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
   - 摘录不合格（拼接了上下两行、只引用了标签行）时，代码在原文中找值所在的行，标签在这一行或
     上面几行内就用原文片段作为证据（anchored_evidence）
   - 单据级字段没有对应的证据摘录时退回补证据；重试用完仍没有才置为 null 并记入 cleared。
     模型漏写证据不等于文件没有相关语义，所以不能一开始就清空
4. 有问题时重试，最多 max_retries 次。问题都落在具体字段或价格上时，只把这些问题发给模型补正
   （LLMPatch），合并进上一次的结果；有结构性问题时把错误连同上一次的输出发回，整份重新抽取。
   仍未通过的问题记入 errors，由 ReviewGate 转为待人工复核
"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError

from bill_agent.context import document_context, loose, squash
from bill_agent.models import ExtractedDocument, RawDocument

from .numbers import check_numbers, numbers_in
from .prompts import HUMAN_PROMPT, PATCH_PROMPT, RETRY_PROMPT, SYSTEM_PROMPT

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


class LLMFieldFix(BaseModel):
    document: int = Field(description="单据序号：上一次输出中 documents 的第几项，从 1 开始")
    field: str = Field(description="单据级字段名，如 booking_no、issuer、carrier")
    value: str | None = Field(default=None, description="对照原文核实后的值；原文确实没有时为 null")
    values: list[str] = Field(default_factory=list, description="bl_nos、container_nos 这类列表字段的完整取值")
    quote: str | None = Field(default=None, description="包含该值的原文摘录，同一行内连续的文字")


class LLMChargeFix(BaseModel):
    line_no: int = Field(description="费用行序号，与上一次输出相同")
    quantity: str | None = Field(default=None, description="数量原文")
    unit_price: str | None = Field(default=None, description="单价原文")
    amount: str | None = Field(default=None, description="金额原文；贷记或退款加负号")
    evidence: list[LLMEvidence] = Field(default_factory=list)


class LLMPriceFix(BaseModel):
    document: int = Field(description="单据序号：上一次输出中 documents 的第几项，从 1 开始")
    charges: list[LLMChargeFix] = Field(default_factory=list, description="每个费用行复核后的数量、单价、金额")
    totals: list[LLMTotal] | None = Field(default=None, description="复核后的合计；不需要修改时为 null")


class LLMPatch(BaseModel):
    """重试时只修正问题字段，不重新输出整份结果。"""

    fields: list[LLMFieldFix] = Field(default_factory=list, description="单据级字段的修正")
    prices: list[LLMPriceFix] = Field(default_factory=list, description="价格复核结果")


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
    v, q = loose(value), loose(quote)
    return bool(q) and (v in q or q in v)


LABEL_WINDOW = 3   # 多栏排版中标签最多在值上面几行


def _label_parts(value: str, quote: str) -> list[str]:
    """摘录中除值以外的文字（标签）。摘录不含值时整段都是标签。"""
    v, q = loose(value), loose(quote)
    parts = q.split(v) if v in q else [q]
    return [p for p in parts if p]


def anchored_evidence(value: str, quote: str, group: list[RawDocument]) -> dict | None:
    """摘录不合格时由代码回原文找证据。

    多栏排版中标签和值分在上下两行，模型常把两行拼成一句（"BOOKING NUMBER 4055331281"），
    或只引用标签行。值是对的，摘录写法不合格。这里在原文中找包含值的行，并要求摘录里的标签
    出现在这一行或上面 LABEL_WINDOW 行内；满足时用原文从标签行到值所在行的连续片段作为证据。
    找不到标签时返回 None，仍按原规则退回模型，防止把不相干的数字当成字段值。
    """
    v = loose(value)
    labels = _label_parts(value, quote)
    if not v or not labels or any(len(p) < 2 for p in labels):
        return None
    for d in group:
        lines = d.text.splitlines()
        sq = [loose(line) for line in lines]
        for i, s in enumerate(sq):
            if v not in s:
                continue
            lo = max(0, i - LABEL_WINDOW)
            hits = [next((j for j in range(i, lo - 1, -1) if p in sq[j]), None) for p in labels]
            if None in hits:
                continue
            start = min(hits)
            text = "\n".join(line.strip() for line in lines[start:i + 1] if line.strip())
            return {"source_id": d.source_id, "quote": text}
    return None


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

class Problem(str):
    """一条核验问题。本身是字符串（原样反馈给模型、写进报告），同时记下下一轮怎么修：

    kind="field"：单据级字段的值或证据有问题，下一轮只让模型补这个字段
    kind="price"：费用行的数量 / 单价 / 金额、合计或费用行证据有问题，下一轮只让模型复核这张单据的价格
    kind="other"：结构性问题（单据编号越界、费用名找不到等），下一轮整份重新抽取
    """

    kind: str
    doc: int | None
    field: str | None

    def __new__(cls, text: str, kind: str = "other", doc: int | None = None, field: str | None = None):
        obj = super().__new__(cls, text)
        obj.kind, obj.doc, obj.field = kind, doc, field
        return obj


PATCHABLE = frozenset(("field", "price"))
PRICE_FIELDS = ("quantity", "unit_price", "amount", "subtotal", "tax_amount", "total_amount")
LIST_FIELDS = ("bl_nos", "container_nos")
EVIDENCE_ONLY = ("totals",)   # 只能补证据、值由 prices 修正的单据级证据字段
PATCH_FIELDS = EVIDENCE_REQUIRED + LIST_FIELDS + EVIDENCE_ONLY

# 原文有这些标签、对应字段却为空时，提示模型核实（漏抽检查）
OMISSION_LABELS = {
    "booking_no": re.compile(r"(?i)\bbooking\s*(?:no\b|number|#|ref)"),
}


@dataclass
class ExtractionResult:
    source_paths: list[str]
    documents: list[ExtractedDocument] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)      # 重试后仍未解决的问题
    attempts: int = 0
    attempt_errors: list[list[str]] = field(default_factory=list)   # 每次尝试的校验问题
    attempt_modes: list[str] = field(default_factory=list)          # 每次尝试是整份抽取（full）还是只补问题（patch）
    cleared: list[str] = field(default_factory=list)      # 重试用完仍没有证据而被置空的字段
    raw_outputs: list[dict] = field(default_factory=list)  # 每次的完整输出（补问题的轮次是合并后的结果），用于审计
    patches: list[dict] = field(default_factory=list)      # 补问题轮次模型给出的修正内容


def convert(out: LLMExtraction, group: list[RawDocument], clear_unsupported: bool = True,
            cleared: list[str] | None = None,
            check_omissions: bool = True) -> tuple[list[ExtractedDocument], list[Problem]]:
    """模型输出 -> ExtractedDocument，同时核验照抄字段。返回 (通过模型校验的单据, 问题列表)。

    clear_unsupported：没有证据的单据级字段直接置空（记入 cleared）；否则作为问题退回模型。
    check_omissions：原文有订舱号等标签而字段为空时作为问题退回。补问题的轮次不再检查，
    模型核实后仍填 null 就接受。
    """
    texts = {d.source_id: squash(d.text) for d in group}
    all_text = "".join(texts.values())
    all_loose = loose("".join(d.text for d in group))
    docs, errors = [], []

    def where(quote: str) -> str | None:
        q = squash(quote)
        return next((sid for sid, t in texts.items() if q and q in t), None)

    for i, d in enumerate(out.documents, start=1):
        tag = f"单据 {i}"

        def problem(text: str, kind: str = "other", name: str | None = None) -> None:
            errors.append(Problem(f"{tag}: {text}", kind, i, name))

        if not 1 <= d.source_doc <= len(group):
            problem(f"source_doc={d.source_doc} 超出文档编号范围 1~{len(group)}")
            continue
        src = group[d.source_doc - 1]
        src_text = texts[src.source_id]
        src_numbers = numbers_in(src.text)

        def check_number(label: str, name: str, value: str | None) -> None:
            # 不含数字的值（如 "N/A"）不在这里判断，由模型校验拒绝
            if value and not numbers_in(value) <= src_numbers:
                problem(f"{label}{name}={value!r} 在文档 {d.source_doc} 中找不到", "price", name)

        def evidence(items: list[LLMEvidence], label: str, values: dict, kind_of) -> dict:
            """values：字段名 -> 字段值，用于摘录不合格时回原文找证据（anchored_evidence）。"""
            ev = {}
            for e in items:
                sid = where(e.quote)
                value = values.get(e.field)
                # 摘录在原文中找不到，或单据级字段的摘录不含值：先按"标签 + 值所在行"回原文找
                if isinstance(value, str) and value and e.field not in ev and (
                        sid is None or (e.field in EVIDENCE_REQUIRED and not has_support(value, e.quote))):
                    found = anchored_evidence(value, e.quote, group)
                    if found:
                        log.info("%s%s 的摘录 %r 不合格，改用原文 %r", label, e.field, e.quote, found["quote"])
                        ev[e.field] = found
                        continue
                if sid is None:
                    problem(f"{label}字段 {e.field} 的证据在原文中找不到: {e.quote!r}"
                            "（只引用同一行内连续的原文；标签和值不在同一行时只引用值）", kind_of(e.field), e.field)
                    continue
                ev.setdefault(e.field, {"source_id": sid, "quote": e.quote})
            return ev

        # 照抄字段必须出现在原文中（忽略空白、大小写和标点："CO.LLC" 与 "Co., LLC" 视为相同）
        verbatim = {name: (name, getattr(d, name)) for name in EVIDENCE_REQUIRED}
        verbatim |= {f"{name}[{k}]": (name, v) for name in LIST_FIELDS for k, v in enumerate(getattr(d, name))}
        for label, (name, v) in verbatim.items():
            if v and loose(v) not in all_loose:
                problem(f"{label}={v!r} 在原文中找不到", "field", name)

        charges = []
        for c in d.charges:
            ctag = f"费用行 {c.line_no}: "
            if squash(c.charge_name_raw) not in src_text:
                problem(f"{ctag}费用名 {c.charge_name_raw!r} 在文档 {d.source_doc} 中找不到")
            for name in ("currency_raw", "payment_term_raw"):
                v = getattr(c, name)
                if v and squash(v) not in all_text:
                    problem(f"{ctag}{name}={v!r} 在原文中找不到")
            if c.payment_term_raw and (msg := payment_term_problem(c.payment_term_raw, src.text)):
                problem(f"{ctag}{msg}")
            for name in ("quantity", "unit_price", "amount"):
                check_number(ctag, name, getattr(c, name))
            charges.append(c.model_dump(exclude={"evidence"})
                           | {"evidence": evidence(c.evidence, ctag, c.model_dump(), lambda f: "price")})

        for k, t in enumerate(d.totals, start=1):
            for name in ("subtotal", "tax_amount", "total_amount"):
                check_number(f"合计 {k}: ", name, getattr(t, name))
            if t.currency_raw and squash(t.currency_raw) not in all_text:
                problem(f"合计 {k}: currency_raw={t.currency_raw!r} 在原文中找不到")

        changes = []
        for ci in d.change_instructions:
            sid = where(ci.quote)
            if sid is None:
                problem(f"改账要求的原文找不到: {ci.quote!r}")
                continue
            changes.append(ci.model_dump(exclude={"quote"}) | {"evidence": {"source_id": sid, "quote": ci.quote}})

        data = d.model_dump(exclude={"source_doc", "charges", "change_instructions", "evidence"})
        # 同一号码的不同写法（"CMDUCHN4053601" / "CMDU CHN4053601"）只保留第一次出现的
        for name in LIST_FIELDS:
            seen, unique = set(), []
            for v in data[name]:
                if squash(v) not in seen:
                    seen.add(squash(v))
                    unique.append(v)
            data[name] = unique
        doc_evidence = evidence(d.evidence, "", data, lambda f: "field")
        # 同一张单据里别的字段的证据已经包含这个值时直接复用（如 carrier 与 issuer 是同一家公司）
        pool = list(doc_evidence.values()) + [e for c in charges for e in c["evidence"].values()]
        for name in EVIDENCE_REQUIRED:
            value = data[name]
            if not value or has_support(value, doc_evidence.get(name, {}).get("quote", "")):
                continue
            reuse = next((e for e in pool if has_support(value, e["quote"])), None)
            if reuse:
                doc_evidence[name] = dict(reuse)
                continue
            if not clear_unsupported:
                problem(f"{name}={value!r} 没有包含该值的证据摘录。请补上原文摘录，文件确实没有相关语义时填 null",
                        "field", name)
                continue
            log.info("%s: %s=%r 重试后仍没有证据摘录，置为 null", tag, name, value)
            if cleared is not None:
                cleared.append(f"{tag} {name}={value!r}")
            data[name] = None
        if check_omissions:
            for name, label in OMISSION_LABELS.items():
                hit = next((m for g in group if (m := label.search(g.text))), None)
                if not data[name] and hit:
                    problem(f"{name} 为空，但原文有 {hit.group(0)!r} 标签。请对照原文核实：标签对应的值属于这票货时"
                            "填写并给出摘录，确实没有或不属于本单时保持 null", "field", name)
        data |= {"source_id": src.source_id, "charges": charges, "change_instructions": changes,
                 "evidence": doc_evidence}
        try:
            doc = ExtractedDocument.model_validate(data)
        except ValidationError as e:
            for x in e.errors():
                loc = [str(p) for p in x["loc"]]
                kind = "price" if loc[0] in ("charges", "totals") and loc[-1] in PRICE_FIELDS else "other"
                problem(f"{'.'.join(loc)}: {x['msg']}", kind, loc[-1])
            continue
        docs.append(doc)
        for msg in check_numbers(doc):
            problem(msg, "price")
        if doc.issuer and doc.recipient and loose(doc.issuer) == loose(doc.recipient):
            problem(f"签发方和接收方都是 {doc.issuer!r}，单据不会开给自己，请重新判断这两个字段")
    return docs, errors


# ---------- 只补问题字段 ----------

def apply_patch(previous: dict, patch: LLMPatch) -> dict:
    """把模型给出的修正合并进上一次的完整输出。只改问题涉及的字段，其余保持不变。"""
    out = copy.deepcopy(previous)
    docs = out["documents"]
    for f in patch.fields:
        if not 1 <= f.document <= len(docs) or f.field not in PATCH_FIELDS:
            continue
        d = docs[f.document - 1]
        if f.field not in EVIDENCE_ONLY:
            d[f.field] = list(f.values) if f.field in LIST_FIELDS else f.value
        d["evidence"] = [e for e in d.get("evidence", []) if e["field"] != f.field]
        if f.quote:
            d["evidence"].append({"field": f.field, "quote": f.quote})
    for p in patch.prices:
        if not 1 <= p.document <= len(docs):
            continue
        d = docs[p.document - 1]
        by_line = {c["line_no"]: c for c in d.get("charges", [])}
        for c in p.charges:
            if (old := by_line.get(c.line_no)) is None:
                continue
            old.update(quantity=c.quantity, unit_price=c.unit_price, amount=c.amount)
            if c.evidence:
                old["evidence"] = [e.model_dump() for e in c.evidence]
        if p.totals is not None:
            d["totals"] = [t.model_dump() for t in p.totals]
    return out


class Extractor:
    def __init__(self, llm=None, max_retries: int = 3):
        if llm is None:
            from bill_agent.llm import get_chat_model

            llm = get_chat_model()
        # include_raw：模型没有按结构输出时拿到解析错误，作为反馈重试，而不是直接崩溃
        self.structured = llm.with_structured_output(LLMExtraction, include_raw=True)
        self.patcher = llm.with_structured_output(LLMPatch, include_raw=True)
        self.max_retries = max_retries

    def extract_group(self, group: list[RawDocument], feedback: list[str] | None = None,
                      previous: dict | None = None) -> ExtractionResult:
        """feedback + previous：审核退回时的意见和被退回的输出，整份重新抽取时一直带给模型。

        第一次整份抽取。之后的问题如果都落在具体字段或价格上（Problem.kind 为 field / price），
        只把这些问题发给模型，让它给出修正并合并进上一次的结果；有结构性问题时整份重新抽取。
        """
        from langchain_core.messages import HumanMessage, SystemMessage

        context = "\n\n".join(document_context(d, i) for i, d in enumerate(group, start=1))
        base = [SystemMessage(SYSTEM_PROMPT), HumanMessage(HUMAN_PROMPT.format(context=context))]
        feedback = feedback or []

        def with_feedback(template: str, problems: list[str], output: dict | None) -> list:
            return base + [HumanMessage(template.format(
                errors="\n".join(f"- {e}" for e in problems),
                previous=json.dumps(output, ensure_ascii=False),
            ))]

        messages = with_feedback(RETRY_PROMPT, feedback, previous) if feedback else base
        result = ExtractionResult(source_paths=[d.source_path for d in group])
        last_full: dict | None = None      # 最近一次能解析的完整输出，补问题时在它上面合并
        patch_next = False

        for attempt in range(1, self.max_retries + 2):
            final = attempt > self.max_retries
            result.attempts = attempt
            result.attempt_modes.append("patch" if patch_next else "full")
            out: LLMExtraction | None = None
            if patch_next:
                resp = self.patcher.invoke(messages)
                patch: LLMPatch | None = resp["parsed"]
                if patch is not None:
                    result.patches.append(patch.model_dump())
                    out = LLMExtraction.model_validate(apply_patch(last_full, patch))
            else:
                resp = self.structured.invoke(messages)
                out = resp["parsed"]

            if out is None:
                raw = resp.get("raw")
                result.raw_outputs.append({"unparsed": getattr(raw, "content", str(raw))})
                result.documents = []
                result.errors = [Problem(f"模型没有按要求的结构输出: {resp.get('parsing_error') or '没有返回结构化结果'}")]
            else:
                last_full = out.model_dump()
                result.raw_outputs.append(last_full)
                result.cleared = []
                result.documents, result.errors = convert(
                    out, group, clear_unsupported=final, cleared=result.cleared, check_omissions=not patch_next)
            result.attempt_errors.append(result.errors)
            if not result.errors:
                break
            log.info("抽取第 %d 次未通过校验: %s", attempt, result.errors)
            patch_next = last_full is not None and all(
                getattr(e, "kind", "other") in PATCHABLE for e in result.errors)
            if patch_next:
                messages = with_feedback(PATCH_PROMPT, result.errors, last_full)
            else:
                messages = with_feedback(RETRY_PROMPT, feedback + result.errors, last_full)
        return result

    def extract(self, docs: list[RawDocument]) -> list[ExtractionResult]:
        return [self.extract_group(g) for g in group_documents(docs)]
