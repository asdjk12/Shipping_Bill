"""LLM 语义审核：对照原文判断抽取结果每个字段的含义是否正确。

    result = Reviewer(llm).review(group, documents)

只给判断，不改结果。规则：
- 模型要对每个字段给出结论；没有覆盖全部字段时带着缺失列表再审一次，仍不完整则 incomplete=True
- 判为有问题的字段和漏抽项必须附原文依据；依据在原文中找不到的意见记入 dismissed，不当作问题
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel, Field

from bill_agent.context import document_context, verify_evidence
from bill_agent.models import ExtractedDocument, RawDocument

from .compare import CHARGE_FIELDS, DOC_FIELDS, LIST_FIELDS, TOTAL_FIELDS
from .prompts import REVIEW_HUMAN_PROMPT, REVIEW_INCOMPLETE_PROMPT, REVIEW_SYSTEM_PROMPT

log = logging.getLogger(__name__)


# ---------- 模型输出结构 ----------

class FieldVerdict(BaseModel):
    document: int = Field(description="单据编号，对应抽取结果中的 key")
    field: str = Field(description="待审核字段的 key，原样填写")
    ok: bool
    reason: str | None = Field(default=None, description="ok 为 false 时写清错在哪里")
    quote: str | None = Field(default=None, description="ok 为 false 时支持判断的原文，逐字照抄")


class Omission(BaseModel):
    description: str = Field(description="漏抽了什么")
    quote: str = Field(description="漏抽内容的原文，逐字照抄")


class LLMReview(BaseModel):
    verdicts: list[FieldVerdict] = Field(default_factory=list)
    omissions: list[Omission] = Field(default_factory=list)


# ---------- 审核结果 ----------

@dataclass
class ReviewIssue:
    field: str              # 例如 "单据 1 issuer"；漏抽项为 "漏抽"
    reason: str
    quote: str | None = None

    def __str__(self) -> str:
        return f"{self.field}: {self.reason}" + (f"（原文：{self.quote!r}）" if self.quote else "")


@dataclass
class ReviewResult:
    issues: list[ReviewIssue] = field(default_factory=list)
    dismissed: list[ReviewIssue] = field(default_factory=list)   # 依据在原文中找不到的意见
    incomplete: str | None = None                                # 审核没有完成的原因
    attempts: int = 0
    raw_outputs: list[dict] = field(default_factory=list)        # 每次模型的原始输出，用于审计


def flatten(doc: ExtractedDocument) -> dict[str, object]:
    """单据 -> {字段路径: 值}。null 也保留，这样漏填的字段同样会被审核到。"""
    def plain(v):
        return str(v) if isinstance(v, Decimal) else v

    out: dict[str, object] = {f: getattr(doc, f) for f in DOC_FIELDS + LIST_FIELDS}
    for c in doc.charges:
        out |= {f"charges[{c.line_no}].{f}": plain(getattr(c, f)) for f in CHARGE_FIELDS}
    for i, t in enumerate(doc.totals, start=1):
        out |= {f"totals[{i}].{f}": plain(getattr(t, f)) for f in ("currency_raw", *TOTAL_FIELDS)}
    for i, ci in enumerate(doc.change_instructions, start=1):
        out[f"change_instructions[{i}]"] = ci.model_dump(mode="json", exclude={"evidence"})
    return out


class Reviewer:
    def __init__(self, llm=None, max_attempts: int = 2):
        if llm is None:
            from bill_agent.llm import get_chat_model

            llm = get_chat_model()
        self.structured = llm.with_structured_output(LLMReview, include_raw=True)
        self.max_attempts = max_attempts

    def review(self, group: list[RawDocument], documents: list[ExtractedDocument]) -> ReviewResult:
        from langchain_core.messages import HumanMessage, SystemMessage

        context = "\n\n".join(document_context(d, i) for i, d in enumerate(group, start=1))
        fields = {i: flatten(d) for i, d in enumerate(documents, start=1)}
        base = [SystemMessage(REVIEW_SYSTEM_PROMPT), HumanMessage(REVIEW_HUMAN_PROMPT.format(
            context=context, extraction=json.dumps(fields, ensure_ascii=False, indent=2)))]
        expected = {(i, f) for i, doc_fields in fields.items() for f in doc_fields}

        result = ReviewResult()
        messages = base
        for attempt in range(1, self.max_attempts + 1):
            resp = self.structured.invoke(messages)
            out: LLMReview | None = resp["parsed"]
            result.attempts = attempt
            if out is None:
                raw = resp.get("raw")
                result.raw_outputs.append({"unparsed": getattr(raw, "content", str(raw))})
                problem = f"模型没有按要求的结构输出: {resp.get('parsing_error') or '没有返回结构化结果'}"
            else:
                result.raw_outputs.append(out.model_dump())
                missing = expected - {(v.document, v.field) for v in out.verdicts}
                problem = f"缺少这些字段的结论: {sorted(missing)}" if missing else None
            if problem is None:
                result.incomplete = None
                self._collect(out, context, result)
                break
            log.info("审核第 %d 次未完成: %s", attempt, problem)
            result.incomplete = problem
            messages = base + [HumanMessage(REVIEW_INCOMPLETE_PROMPT.format(problem=problem))]
        return result

    @staticmethod
    def _collect(out: LLMReview, context: str, result: ReviewResult) -> None:
        found = [ReviewIssue(f"单据 {v.document} {v.field}", v.reason or "未说明理由", v.quote)
                 for v in out.verdicts if not v.ok]
        found += [ReviewIssue("漏抽", o.description, o.quote) for o in out.omissions]
        for issue in found:
            backed = bool(issue.quote) and verify_evidence([issue.quote], context)[0]
            (result.issues if backed else result.dismissed).append(issue)
