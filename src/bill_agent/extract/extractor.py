"""LLM 结构化抽取：RawDocument（解析 / OCR 结果）-> ExtractedDocument。

    results = Extractor().extract(parse_result.documents)

流程（每组文件一次 LLM 调用）：
1. 分组：一个上传文件及其所有附件为一组（邮件正文 + 附件一起给模型，正文可能补充附件的信息）
2. 模型按 LLMExtraction 输出。这个结构专为模型设计：金额照抄原文字符串，证据用列表，
   不让模型填 source_id
3. 代码转换为 ExtractedDocument 并核验：
   - 模型校验（金额能否解析，"4O.00" 这类 OCR 混淆会在这里报错；证据字段名是否存在；行号是否唯一）
   - 证据和所有照抄字段必须能在原文中找到（文本忽略空白和大小写，数字按完整数值比较）
   - 数量 × 单价、费用行加总、小计 + 税额必须与单据上的数字一致（numbers.check_numbers）
   - 付款方式必须是已知写法，单字母有反证时不采信（payment_term_problem）
   - 单据级字段没有对应的证据摘录时退回补证据；重试用完仍没有才置为 null 并记入 cleared。
     模型漏写证据不等于文件没有相关语义，所以不能一开始就清空
4. 有问题时把错误连同上一次的输出发回模型重试，最多 max_retries 次；
   仍未通过的问题记入 errors，由 ReviewGate 转为待人工复核
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError

from bill_agent.context import document_context, squash
from bill_agent.models import ExtractedDocument, RawDocument

from .numbers import check_numbers, numbers_in
from .prompts import HUMAN_PROMPT, RETRY_PROMPT, SYSTEM_PROMPT

log = logging.getLogger(__name__)


# ---------- 模型输出结构 ----------

class LLMEvidence(BaseModel):
    field: str = Field(description="字段名。单据级只能用单据字段（如 reference_no、payee、totals），"
                                   "费用行只能用费用行字段（如 amount、currency_raw）")
    quote: str = Field(description="原文中同一行内连续的文字，逐字照抄")


class LLMCharge(BaseModel):
    line_no: int = Field(description="费用行序号，从 1 开始")
    charge_name_raw: str = Field(description="费用名称原文")
    quantity: str | None = Field(default=None, description="数量原文")
    unit_price: str | None = Field(default=None, description="单价原文")
    amount: str | None = Field(default=None, description="金额原文，如 '1,310.40'；贷记或退款加负号")
    currency_raw: str | None = Field(default=None, description="该行币种原文；行上没有则 null")
    payment_term_raw: str | None = Field(default=None, description="付款方式原文，如 PP / CC / C")
    evidence: list[LLMEvidence] = Field(default_factory=list)


class LLMTotal(BaseModel):
    currency_raw: str | None = None
    subtotal: str | None = None
    tax_amount: str | None = None
    total_amount: str | None = None


class LLMChangeInstruction(BaseModel):
    target_reference_no: str | None = None
    target_charge_name_raw: str | None = None
    field: str = Field(description="要修改的字段，如 amount / currency / payment_term")
    new_value_raw: str
    quote: str = Field(description="改账要求的原文")


class LLMDocument(BaseModel):
    source_doc: int = Field(description="单据所在文件的编号（上下文中 '文档 N' 的 N）")
    document_type_raw: str | None = Field(default=None, description="单据标题原文，如 'DEBIT NOTE'")
    reference_no: str | None = Field(default=None, description="单据编号")
    document_date_raw: str | None = Field(default=None, description="单据日期原文")
    issuer: str | None = Field(default=None, description="签发方")
    recipient: str | None = Field(default=None, description="接收方")
    payee: str | None = Field(default=None, description="原文明确写出的收款方")
    payer: str | None = Field(default=None, description="原文明确写出的付款方")
    shipper: str | None = Field(default=None, description="寄件人")
    consignee: str | None = Field(default=None, description="收件人")
    carrier: str | None = Field(default=None, description="送货人：收取本单费用的航运公司")
    bl_nos: list[str] = Field(default_factory=list, description="所有提单号")
    booking_no: str | None = Field(default=None, description="订舱号")
    container_nos: list[str] = Field(default_factory=list, description="所有箱号")
    currency_raw: str | None = Field(default=None, description="单据级币种")
    charges: list[LLMCharge] = Field(default_factory=list)
    totals: list[LLMTotal] = Field(default_factory=list)
    change_instructions: list[LLMChangeInstruction] = Field(default_factory=list)
    evidence: list[LLMEvidence] = Field(default_factory=list, description="单据级字段的证据")


class LLMExtraction(BaseModel):
    documents: list[LLMDocument] = Field(default_factory=list, description="每张收费单据一项；没有收费单据则为空")


# ---------- 分组与上下文 ----------

def group_documents(docs: list[RawDocument]) -> list[list[RawDocument]]:
    """按上传文件分组：附件归到最顶层的邮件下；保持原顺序。"""
    by_id = {d.source_id: d for d in docs}

    def root(d: RawDocument) -> str:
        seen = set()
        while d.parent_id and d.parent_id in by_id and d.source_id not in seen:
            seen.add(d.source_id)
            d = by_id[d.parent_id]
        return d.source_id

    groups: dict[str, list[RawDocument]] = {}
    for d in docs:
        if d.ready_for_extraction and d.text.strip():
            groups.setdefault(root(d), []).append(d)
    return list(groups.values())


# ---------- 没有相关语义就留空 ----------

# 提单号、箱号是列表，费用行的值逐项回原文查找，都不靠这一条
EVIDENCE_REQUIRED = ("document_type_raw", "reference_no", "document_date_raw", "issuer", "recipient",
                     "payee", "payer", "shipper", "consignee", "carrier", "booking_no", "currency_raw")


def has_support(value: str, quote: str) -> bool:
    """摘录包含值，或者值跨行时摘录是值的一部分。"""
    v, q = squash(value), squash(quote)
    return bool(q) and (v in q or q in v)


# ---------- 付款方式 ----------

PAYMENT_TERMS = {"p", "c", "pp", "cc", "prepaid", "collect", "freightprepaid", "freightcollect",
                 "预付", "到付", "寄付"}
_PAYMENT_COLUMN = re.compile(r"(?i)\bP\s*/\s*C\b|\bPP\s*/\s*CC\b|prepaid|collect|pay(ment)?\s*terms?|预付|到付|寄付")
_BASIS_COLUMN = re.compile(r"(?i)\b(base|basis)\b")


def payment_term_problem(value: str, source_text: str) -> str | None:
    """只拦有明确反证的值，其余交给模型的语义判断，不要求原文有固定的列名。"""
    term = squash(value)
    if term not in PAYMENT_TERMS:
        return f"payment_term_raw={value!r} 不是付款方式，付款方式只能取 PP / CC / Prepaid / Collect 这类写法，否则填 null"
    if len(term) == 1 and _BASIS_COLUMN.search(source_text) and not _PAYMENT_COLUMN.search(source_text):
        return (f"payment_term_raw={value!r} 取自计费基准列：单据有 Base / Basis 列，没有付款方式列，"
                "这里应填 null")
    return None


# ---------- 转换与核验 ----------

@dataclass
class ExtractionResult:
    source_paths: list[str]
    documents: list[ExtractedDocument] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)      # 重试后仍未解决的问题
    attempts: int = 0
    attempt_errors: list[list[str]] = field(default_factory=list)   # 每次尝试的校验问题
    cleared: list[str] = field(default_factory=list)      # 重试用完仍没有证据而被置空的字段
    raw_outputs: list[dict] = field(default_factory=list)  # 每次模型的原始输出，用于审计


def convert(out: LLMExtraction, group: list[RawDocument], clear_unsupported: bool = True,
            cleared: list[str] | None = None) -> tuple[list[ExtractedDocument], list[str]]:
    """模型输出 -> ExtractedDocument，同时核验照抄字段。返回 (通过模型校验的单据, 问题列表)。

    clear_unsupported：没有证据的单据级字段直接置空（记入 cleared）；否则作为问题退回模型。
    """
    texts = {d.source_id: squash(d.text) for d in group}
    all_text = "".join(texts.values())
    docs, errors = [], []

    def where(quote: str) -> str | None:
        q = squash(quote)
        return next((sid for sid, t in texts.items() if q and q in t), None)

    for i, d in enumerate(out.documents, start=1):
        tag = f"单据 {i}"
        if not 1 <= d.source_doc <= len(group):
            errors.append(f"{tag}: source_doc={d.source_doc} 超出文档编号范围 1~{len(group)}")
            continue
        src = group[d.source_doc - 1]
        src_text = texts[src.source_id]
        src_numbers = numbers_in(src.text)

        def check_number(label: str, name: str, value: str | None) -> None:
            # 不含数字的值（如 "N/A"）不在这里判断，由模型校验拒绝
            if value and not numbers_in(value) <= src_numbers:
                errors.append(f"{label}: {name}={value!r} 在文档 {d.source_doc} 中找不到")

        def evidence(items: list[LLMEvidence], prefix: str) -> dict:
            ev = {}
            for e in items:
                sid = where(e.quote)
                if sid is None:
                    errors.append(f"{prefix}: 字段 {e.field} 的证据在原文中找不到: {e.quote!r}"
                                  "（只引用同一行内连续的原文；标签和值不在同一行时只引用值）")
                    continue
                ev.setdefault(e.field, {"source_id": sid, "quote": e.quote})
            return ev

        # 照抄字段必须出现在原文中
        verbatim = {name: getattr(d, name) for name in (
            "document_type_raw", "reference_no", "document_date_raw", "issuer", "recipient",
            "payee", "payer", "shipper", "consignee", "carrier", "booking_no", "currency_raw")}
        verbatim |= {f"bl_nos[{k}]": v for k, v in enumerate(d.bl_nos)}
        verbatim |= {f"container_nos[{k}]": v for k, v in enumerate(d.container_nos)}
        for name, v in verbatim.items():
            if v and squash(v) not in all_text:
                errors.append(f"{tag}: {name}={v!r} 在原文中找不到")

        charges = []
        for c in d.charges:
            ctag = f"{tag} 费用行 {c.line_no}"
            if squash(c.charge_name_raw) not in src_text:
                errors.append(f"{ctag}: 费用名 {c.charge_name_raw!r} 在文档 {d.source_doc} 中找不到")
            for name in ("currency_raw", "payment_term_raw"):
                v = getattr(c, name)
                if v and squash(v) not in all_text:
                    errors.append(f"{ctag}: {name}={v!r} 在原文中找不到")
            if c.payment_term_raw and (problem := payment_term_problem(c.payment_term_raw, src.text)):
                errors.append(f"{ctag}: {problem}")
            for name in ("quantity", "unit_price", "amount"):
                check_number(ctag, name, getattr(c, name))
            charges.append(c.model_dump(exclude={"evidence"}) | {"evidence": evidence(c.evidence, ctag)})

        for k, t in enumerate(d.totals, start=1):
            for name in ("subtotal", "tax_amount", "total_amount"):
                check_number(f"{tag} 合计 {k}", name, getattr(t, name))
            if t.currency_raw and squash(t.currency_raw) not in all_text:
                errors.append(f"{tag} 合计 {k}: currency_raw={t.currency_raw!r} 在原文中找不到")

        changes = []
        for ci in d.change_instructions:
            sid = where(ci.quote)
            if sid is None:
                errors.append(f"{tag}: 改账要求的原文找不到: {ci.quote!r}")
                continue
            changes.append(ci.model_dump(exclude={"quote"}) | {"evidence": {"source_id": sid, "quote": ci.quote}})

        data = d.model_dump(exclude={"source_doc", "charges", "change_instructions", "evidence"})
        # 同一号码的不同写法（"CMDUCHN4053601" / "CMDU CHN4053601"）只保留第一次出现的
        for name in ("bl_nos", "container_nos"):
            seen, unique = set(), []
            for v in data[name]:
                if squash(v) not in seen:
                    seen.add(squash(v))
                    unique.append(v)
            data[name] = unique
        doc_evidence = evidence(d.evidence, tag)
        for name in EVIDENCE_REQUIRED:
            if data[name] and not has_support(data[name], doc_evidence.get(name, {}).get("quote", "")):
                if not clear_unsupported:
                    errors.append(f"{tag}: {name}={data[name]!r} 没有包含该值的证据摘录。请补上原文摘录，"
                                  "文件确实没有相关语义时填 null")
                    continue
                log.info("%s: %s=%r 重试后仍没有证据摘录，置为 null", tag, name, data[name])
                if cleared is not None:
                    cleared.append(f"{tag} {name}={data[name]!r}")
                data[name] = None
        data |= {"source_id": src.source_id, "charges": charges, "change_instructions": changes,
                 "evidence": doc_evidence}
        try:
            doc = ExtractedDocument.model_validate(data)
        except ValidationError as e:
            errors += [f"{tag}: {'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]
            continue
        docs.append(doc)
        errors += [f"{tag}: {e}" for e in check_numbers(doc)]
        if doc.issuer and doc.recipient and squash(doc.issuer) == squash(doc.recipient):
            errors.append(f"{tag}: 签发方和接收方都是 {doc.issuer!r}，单据不会开给自己，请重新判断这两个字段")
    return docs, errors


class Extractor:
    def __init__(self, llm=None, max_retries: int = 2):
        if llm is None:
            from bill_agent.llm import get_chat_model

            llm = get_chat_model()
        # include_raw：模型没有按结构输出时拿到解析错误，作为反馈重试，而不是直接崩溃
        self.structured = llm.with_structured_output(LLMExtraction, include_raw=True)
        self.max_retries = max_retries

    def extract_group(self, group: list[RawDocument], feedback: list[str] | None = None,
                      previous: dict | None = None) -> ExtractionResult:
        """feedback + previous：审核退回时的意见和被退回的输出，整个重试过程中一直带给模型。"""
        from langchain_core.messages import HumanMessage, SystemMessage

        context = "\n\n".join(document_context(d, i) for i, d in enumerate(group, start=1))
        base = [SystemMessage(SYSTEM_PROMPT), HumanMessage(HUMAN_PROMPT.format(context=context))]
        feedback = feedback or []

        def with_feedback(problems: list[str], output: dict | None) -> list:
            return base + [HumanMessage(RETRY_PROMPT.format(
                errors="\n".join(f"- {e}" for e in problems),
                previous=json.dumps(output, ensure_ascii=False),
            ))]

        messages = with_feedback(feedback, previous) if feedback else base
        result = ExtractionResult(source_paths=[d.source_path for d in group])

        for attempt in range(1, self.max_retries + 2):
            resp = self.structured.invoke(messages)
            out: LLMExtraction | None = resp["parsed"]
            result.attempts = attempt
            if out is None:
                raw = resp.get("raw")
                result.raw_outputs.append({"unparsed": getattr(raw, "content", str(raw))})
                result.documents = []
                result.errors = [f"模型没有按要求的结构输出: {resp.get('parsing_error') or '没有返回结构化结果'}"]
            else:
                result.raw_outputs.append(out.model_dump())
                result.cleared = []
                result.documents, result.errors = convert(
                    out, group, clear_unsupported=attempt > self.max_retries, cleared=result.cleared)
            result.attempt_errors.append(result.errors)
            if not result.errors:
                break
            log.info("抽取第 %d 次未通过校验: %s", attempt, result.errors)
            messages = with_feedback(feedback + result.errors, result.raw_outputs[-1])
        return result

    def extract(self, docs: list[RawDocument]) -> list[ExtractionResult]:
        return [self.extract_group(g) for g in group_documents(docs)]
