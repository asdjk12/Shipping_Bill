"""编排的节点。

约定：
- 节点是普通函数 `node(state, deps) -> (update, summary)`：update 是要更新的状态字段，summary 是一行摘要。
  节点不引用 LangGraph 的类型（human_review 的 interrupt 除外），build.py 统一包装成图节点，
  并负责计时、token 统计和 trace；以后换自研 harness 的 Runtime 层，节点不用改。
- 外部能力（OCR、LLM skill、账单库）都从 NodeDeps 注入，测试时换成替身。

| 节点          | 复用                                                   | LLM |
|---------------|--------------------------------------------------------|-----|
| parse         | parsers.Router.route_paths                             | 否  |
| ocr           | ocr.run_ocr + CachedOCR(LocalOCR)                      | 否  |
| import_bills  | BillRepository.import_bills                            | 否  |
| extract       | 结构化抽取 skill + ValidationHook（run_skill）         | 是  |
| normalize     | normalize.Normalizer.normalize_document                | 否  |
| score         | harness.confidence.score_record                        | 否  |
| reconcile     | billing.BillProcessor（含 AR/AP）                      | 否  |
| human_review  | interrupt()                                            | 否  |
| output        | models.to_json                                         | 否  |
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from bill_agent.billing import BillProcessor, BillRepository, Matcher, PartyNormalizer
from bill_agent.billing.operation import OperationResult
from bill_agent.extract import GateResult
from bill_agent.extract.serialize import gate_json
from bill_agent.harness.confidence import DEFAULT_THRESHOLD, score_record
from bill_agent.models import ChargeRecord, DocStatus, Operation, OutputRecord, RawDocument, to_json
from bill_agent.normalize import Normalizer
from bill_agent.ocr.interface import OCRBackend, run_ocr
from bill_agent.parsers import RouterConfig, build_default_router
from bill_agent.parsers.base import BillJSON
from bill_agent.rules import EntityMatcher, apply_posting

from .state import (
    AgentState,
    BillJsonInput,
    ExtractionGroup,
    NormalizedDoc,
    ReviewDecision,
    ReviewTicket,
    SkippedInput,
    Stage,
)

NodeResult = tuple[dict, str]

# 人工修改这些字段后要重新判断 AR/AP
_PARTY_FIELDS = {"issuer", "recipient", "payee", "payer", "shipper", "consignee", "carrier"}


@dataclass
class NodeDeps:
    router_config: RouterConfig
    ocr_backend: OCRBackend | None            # None：不做 OCR，扫描件留在 pending_ocr，抽取时跳过
    # 抽取：(输入路径, 解析 + OCR 后的文档) -> 每组的 GateResult。生产实现是
    # run_skill(skill, {"paths": ...}, [ValidationHook()])；用 Callable 而不是 StructuredTool，
    # 节点不碰 LangChain 类型（Tool-Skill / Hook 层的边界）。
    extract_fn: Callable[[list[str], list[RawDocument]], list[GateResult]]
    normalizer: Normalizer
    db_path: str
    entity: EntityMatcher | None              # 记账主体；None 时 posting_type 全部留空
    confidence_threshold: float = DEFAULT_THRESHOLD
    hitl: bool = True                         # False：待人工直接输出 PENDING_REVIEW，不暂停
    max_review_rounds: int = 3                # 人工处理后仍待人工（如匹配冲突），最多再问几轮
    audit_dir: str | None = None              # 审计记录目录：extraction.json、trace.jsonl
    out_path: str | None = None               # 最终 JSON


# ---------------------------------------------------------------- 解析与识别


def parse(state: AgentState, deps: NodeDeps) -> NodeResult:
    """inputs -> raw_docs / bill_jsons / skipped。PDF、图片登记为 pending_ocr。"""
    result = build_default_router(deps.router_config).route_paths(state["inputs"])
    pending = sum(d.status is DocStatus.PENDING_OCR for d in result.documents)
    return {
        "raw_docs": result.documents,
        "bill_jsons": [BillJsonInput(source_path=b.source_path, source_id=b.source_id, records=b.records)
                       for b in result.bill_jsons],
        "skipped": [SkippedInput(source_path=s.source_path, reason=s.reason) for s in result.skipped],
    }, (f"{len(result.documents)} 份文档（待 OCR {pending}），{len(result.bill_jsons)} 个账单 JSON，"
        f"跳过 {len(result.skipped)} 项")


def needs_ocr(state: AgentState) -> bool:
    return any(d.status in (DocStatus.PENDING_OCR, DocStatus.OCR_FAILED) for d in state.get("raw_docs", []))


def ocr(state: AgentState, deps: NodeDeps) -> NodeResult:
    """只处理 pending_ocr / ocr_failed 的文档，其余原样保留，顺序不变。"""
    docs = run_ocr(state["raw_docs"], deps.ocr_backend)
    failed = [d.source_path for d in docs if d.status is DocStatus.OCR_FAILED]
    done = sum(d.status is DocStatus.OCR_DONE for d in docs)
    return {"raw_docs": docs}, f"OCR 完成 {done} 份" + (f"，失败 {len(failed)} 份: {failed}" if failed else "")


def import_bills(state: AgentState, deps: NodeDeps) -> NodeResult:
    """把输入里的已有账单 JSON 导入账单库（按 record_id upsert，幂等）。必须在 reconcile 之前。"""
    bills = state.get("bill_jsons") or []
    if not bills:
        return {}, "没有账单 JSON"
    counts: Counter[str] = Counter()
    with BillRepository(deps.db_path) as repo:
        for b in bills:
            report = repo.import_bills(BillJSON(b.source_path, b.source_id, b.records))
            counts.update(created=len(report.created), updated=len(report.updated),
                          unchanged=len(report.unchanged), errors=len(report.errors))
    return {}, (f"导入 {len(bills)} 个文件：新建 {counts['created']}，更新 {counts['updated']}，"
                f"无变化 {counts['unchanged']}，错误 {counts['errors']}")


# ---------------------------------------------------------------- 抽取与标准化


def extract(state: AgentState, deps: NodeDeps) -> NodeResult:
    """raw_docs -> groups。

    抽取的重试全部在 skill 内部：ReviewGate（代码核验 + LLM 审核）和 ValidationHook（V1–V8）。
    所以图上没有 validate -> extract 的回边。完整轮次记录写到审计目录的 extraction.json。
    """
    docs = state["raw_docs"]
    if not any(d.ready_for_extraction for d in docs):
        return {"groups": []}, "没有可抽取的文档"
    results = deps.extract_fn(state["inputs"], docs)
    if deps.audit_dir:
        _write(Path(deps.audit_dir) / "extraction.json",
               json.dumps([gate_json(g) for g in results], ensure_ascii=False, indent=2))
    groups = [ExtractionGroup(source_paths=g.source_paths, status=g.status.value, documents=g.documents,
                              reasons=[str(r) for r in g.reasons], warnings=g.warnings,
                              rounds=max(len(g.rounds), 1)) for g in results]
    counts = Counter(g.status for g in groups)
    return {"groups": groups}, " / ".join(f"{k} {v}" for k, v in counts.items()) + f"（共 {len(groups)} 组）"


def normalize(state: AgentState, deps: NodeDeps) -> NodeResult:
    """groups -> normalized。needs_review 组的原因带到每条记录的 review_reasons。

    币种 ISO 4217 和日期的校验在 Normalizer 里完成（问题写进 review_reasons），不单独设 validate 节点。
    """
    out = []
    for i, g in enumerate(state.get("groups") or []):
        upstream = g.reasons if g.status != "passed" else []
        for d in g.documents:
            out.append(NormalizedDoc(group=i, doc=d, records=deps.normalizer.normalize_document(d, upstream)))
    n = sum(len(nd.records) for nd in out)
    flagged = sum(bool(r.review_reasons) for nd in out for r in nd.records)
    return {"normalized": out}, f"{len(out)} 张单据，{n} 条费用行（带待人工原因 {flagged} 条）"


def score(state: AgentState, deps: NodeDeps) -> NodeResult:
    """给每条费用行打置信度；低于阈值时在 review_reasons 里写明，reconcile 据此判 PENDING_REVIEW。"""
    raw = {d.source_id: d for d in state["raw_docs"]}
    groups = state["groups"]
    out, low = [], 0
    for nd in state["normalized"]:
        g = groups[nd.group]
        records, values, details = [], [], []
        for rec in nd.records:
            c = score_record(rec, nd.doc, raw, warnings=len(g.warnings), rounds=g.rounds)
            if c.value < deps.confidence_threshold:
                low += 1
                rec = rec.model_copy(update={"review_reasons": [
                    *rec.review_reasons, f"置信度低于 {deps.confidence_threshold}：{c.describe()}"]})
            records.append(rec)
            values.append(c.value)
            details.append(c.describe())
        out.append(nd.model_copy(update={"records": records, "confidence": values, "confidence_detail": details}))
    return {"normalized": out}, f"低于阈值 {deps.confidence_threshold} 的 {low} 条"


# ---------------------------------------------------------------- 入账


def _processor(repo: BillRepository, deps: NodeDeps, entity: EntityMatcher | None) -> BillProcessor:
    return BillProcessor(repo, Matcher(repo, PartyNormalizer.load()), entity, deps.normalizer)


def _paths_by_id(state: AgentState) -> dict[str, str]:
    return {d.source_id: d.source_path for d in state.get("raw_docs", [])}


def _output(res: OperationResult, paths: dict[str, str], confidence: float | None,
            detail: str | None, prefix: list[str] = ()) -> OutputRecord:
    out = res.to_output()
    audit = out.audit_trail.model_copy(update={
        "ai_reasoning": [*prefix, *out.audit_trail.ai_reasoning, *([detail] if detail else [])],
        "source_files": list(dict.fromkeys(paths[s] for s in res.record.source_ids if s in paths)),
        "confidence": confidence,
    })
    return out.model_copy(update={"audit_trail": audit})


def _is_instruction(res: OperationResult) -> bool:
    return res.record.line_no is None and "change_instruction" in res.record.evidence


def reconcile(state: AgentState, deps: NodeDeps) -> NodeResult:
    """AR/AP -> 4 级匹配 -> 操作判定 -> 写库，一个批次一个事务。

    match / ar_ap / decide / persist 不拆成独立节点：BillProcessor 在同一事务里边判定边写库，
    同批次后面的行要能匹配到前面刚 CREATE 的记录（Matcher.claim），拆开就要另做"计划 / 提交"两阶段。
    有 decisions 时是人工处理后再次进入，只处理 tickets 对应的记录（_apply_decisions）。
    """
    if state.get("decisions"):
        return _apply_decisions(state, deps)

    paths = _paths_by_id(state)
    groups = state.get("groups") or []
    outputs: list[OutputRecord] = []
    tickets: list[ReviewTicket] = []
    with BillRepository(deps.db_path) as repo:
        proc = _processor(repo, deps, deps.entity)
        proc.matcher.new_batch()
        with repo.transaction():
            for nd in state.get("normalized") or []:
                by_line = {r.line_no: (c, d) for r, c, d in zip(nd.records, nd.confidence, nd.confidence_detail)}
                default = (min(nd.confidence), None) if nd.confidence else (None, None)
                for res in proc.process_document(nd.doc, nd.records):
                    conf, detail = by_line.get(res.record.line_no, default) if not _is_instruction(res) else default
                    outputs.append(_output(res, paths, conf, detail))
                    if res.operation is Operation.PENDING_REVIEW:
                        tickets.append(ReviewTicket(
                            ticket_id=f"T{len(outputs)}", output_index=len(outputs) - 1,
                            source_paths=groups[nd.group].source_paths if groups else [],
                            record=res.record, reasons=res.review_reasons, confidence=conf,
                            instruction=_is_instruction(res)))
        total = repo.count()
    counts = Counter(o.operation.value for o in outputs)
    summary = " | ".join(f"{k} {v}" for k, v in counts.items()) or "没有费用行"
    return {"outputs": outputs, "tickets": tickets, "decisions": [], "review_round": 0}, \
        f"{summary}；账单库共 {total} 条"


def _apply_decisions(state: AgentState, deps: NodeDeps) -> NodeResult:
    """人工处理：
    - approve：清空 review_reasons，按原记录再走一次匹配和判定（AR/AP 已在首轮判过，不再重判）；
    - edit：先把 changes 写进记录（改了交易方字段时重判 AR/AP），再同 approve；
    - reject：不写库，保留 PENDING_REVIEW，原因追加"人工驳回"。
    再走一次后仍是 PENDING_REVIEW（如匹配冲突）的，生成新一轮 ticket。
    改账指令无法自动执行的 ticket 只能 reject，approve / edit 不生效。
    """
    tickets = {t.ticket_id: t for t in state.get("tickets") or []}
    outputs = list(state["outputs"])
    paths = _paths_by_id(state)
    new_tickets: list[ReviewTicket] = []
    counts: Counter[str] = Counter()

    with BillRepository(deps.db_path) as repo:
        proc = _processor(repo, deps, None)
        proc.matcher.new_batch()
        with repo.transaction():
            for d in state["decisions"]:
                t = tickets.get(d.ticket_id)
                if t is None:
                    counts["unknown"] += 1
                    continue
                note = f"（{d.note}）" if d.note else ""
                old = outputs[t.output_index]
                if d.action == "reject" or t.instruction:
                    msg = (f"人工驳回{note}" if d.action == "reject"
                           else f"人工 {d.action} 不生效：改账指令无法自动定位，只能驳回或在账单库中手工处理{note}")
                    outputs[t.output_index] = _with_review(old, msg)
                    counts["reject" if d.action == "reject" else "ignored"] += 1
                    continue
                try:
                    rec = _decided_record(t.record, d, deps.entity)
                except ValidationError as e:
                    outputs[t.output_index] = _with_review(old, f"人工修改的值不合法，未处理：{e.errors()[0]['msg']}")
                    new_tickets.append(t)
                    counts["invalid"] += 1
                    continue
                prefix = [f"人工{'确认' if d.action == 'approve' else '修改'}{note}"
                          + (f"：{d.changes}" if d.changes else "")]
                [res] = proc.process_document(None, [rec])
                outputs[t.output_index] = _output(res, paths, t.confidence, None, prefix)
                counts[res.operation.value] += 1
                if res.operation is Operation.PENDING_REVIEW:
                    new_tickets.append(t.model_copy(update={"record": res.record, "reasons": res.review_reasons}))

    summary = " | ".join(f"{k} {v}" for k, v in counts.items())
    return {"outputs": outputs, "tickets": new_tickets, "decisions": []}, f"人工处理 {len(state['decisions'])} 条：{summary}"


def _decided_record(rec: ChargeRecord, d: ReviewDecision, entity: EntityMatcher | None) -> ChargeRecord:
    data = {**rec.model_dump(), **d.changes, "review_reasons": []}
    out = ChargeRecord.model_validate(data)
    if entity is not None and _PARTY_FIELDS & set(d.changes) and not {"posting_type", "accounting_entity"} & set(d.changes):
        out = apply_posting([out], entity)[0].model_copy(update={"review_reasons": []})
    return out


def _with_review(out: OutputRecord, reason: str) -> OutputRecord:
    audit = out.audit_trail.model_copy(update={"review_reasons": [*out.audit_trail.review_reasons, reason]})
    return out.model_copy(update={"audit_trail": audit})


def human_review(state: AgentState, deps: NodeDeps) -> NodeResult:
    """interrupt(tickets) 暂停整个图，等人工返回 list[ReviewDecision]。

    恢复：graph.invoke(Command(resume=[...]), config={"configurable": {"thread_id": run_id}})。
    状态由 checkpointer 保存，进程退出后也能恢复。恢复时本节点从头重跑，interrupt 直接返回恢复值。
    """
    from langgraph.types import interrupt

    tickets = state["tickets"]
    answer = interrupt({"run_id": state.get("run_id"), "tickets": [t.model_dump(mode="json") for t in tickets]})
    decisions = [ReviewDecision.model_validate(x) for x in (answer or [])]
    return {"decisions": decisions, "review_round": state.get("review_round", 0) + 1}, \
        f"收到 {len(decisions)} 条人工处理（共 {len(tickets)} 条待人工）"


def output(state: AgentState, deps: NodeDeps) -> NodeResult:
    """最终 schema 校验后写 JSON 文件。"""
    outputs = [OutputRecord.model_validate(o.model_dump()) for o in state.get("outputs") or []]
    if deps.out_path:
        _write(Path(deps.out_path), to_json(outputs))
    counts = Counter(o.operation.value for o in outputs)
    return {"stage": Stage.DONE}, " | ".join(f"{k} {v}" for k, v in counts.items()) + \
        (f"，写入 {deps.out_path}" if deps.out_path else "")


# ---------------------------------------------------------------- 条件边


def after_parse(state: AgentState, deps: NodeDeps) -> str:
    return "ocr" if deps.ocr_backend is not None and needs_ocr(state) else "import_bills"


def after_reconcile(state: AgentState, deps: NodeDeps) -> str:
    if state.get("tickets") and deps.hitl and state.get("review_round", 0) < deps.max_review_rounds:
        return "human_review"
    return "output"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
