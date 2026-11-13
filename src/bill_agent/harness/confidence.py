"""费用行置信度（development.md 第 8 节）。

    confidence = OCR 置信度 × 证据覆盖率 × Π 惩罚系数

- OCR 置信度：记录来源文档（source_ids）里最低的 ocr_confidence；文字层 / Excel / Word 没有该值，按 1.0。
- 证据覆盖率：单据级和本费用行里有值的字段中，有原文依据的比例。模型常用一条整行摘录同时支撑
  名称、单价、币种，却只挂在 amount 一个键下，所以不按键名算：字段有自己的证据，或者值能在
  同一范围的任一摘录里找到（费用行字段看本行摘录，单据级字段看单据级摘录），都算有依据。
  数字按数值比较（5.0 能在 "5.00" 里找到）。列表字段（提单号、箱号）只要有一个值有依据就算：
  Evidence 一个字段只能挂一条摘录，列表里其余的值没有位置放证据。
- 惩罚系数：数据校验的每条 warning × WARNING_PENALTY；抽取 + 审核每多跑一轮 × RETRY_PENALTY
  （被退回过说明模型第一次理解有偏差，即使改对了也降一点）。

低于阈值时在 review_reasons 里写明三项因子，reconcile 据此判 PENDING_REVIEW。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from decimal import Decimal

from bill_agent.extract.extractor import has_support
from bill_agent.extract.numbers import numbers_in
from bill_agent.models import ChargeRecord, ExtractedDocument, RawDocument

WARNING_PENALTY = 0.95
RETRY_PENALTY = 0.95
DEFAULT_THRESHOLD = 0.8

# 不是"抽取出的值"的字段，不计入覆盖率
_DOC_SKIP = {"source_id", "evidence", "charges", "change_instructions", "totals", "line_no"}
_CHARGE_SKIP = {"evidence", "line_no"}


@dataclass
class Confidence:
    value: float
    ocr: float
    coverage: float
    penalty: float
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        s = f"置信度 {self.value:.2f} = OCR {self.ocr:.2f} × 证据覆盖 {self.coverage:.2f} × 惩罚 {self.penalty:.2f}"
        return s + (f"（{'；'.join(self.notes)}）" if self.notes else "")


def _filled(model, skip: set[str]) -> dict[str, object]:
    out = {}
    for name in type(model).model_fields:
        if name in skip:
            continue
        v = getattr(model, name)
        if v is not None and v != [] and v != "":
            out[name] = v
    return out


def _one_supported(v, quotes: list[str]) -> bool:
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        return any(Decimal(str(v)) in numbers_in(q) for q in quotes)
    return any(has_support(str(v), q) for q in quotes)


def _supported(value, quotes: list[str]) -> bool:
    if isinstance(value, list):
        return any(_one_supported(v, quotes) for v in value)
    return _one_supported(value, quotes)


def _missing(model, skip: set[str], prefix: str) -> tuple[int, list[str]]:
    filled = _filled(model, skip)
    quotes = [e.quote for e in model.evidence.values()]
    missing = [f"{prefix}.{k}" for k, v in filled.items()
               if not (k in model.evidence and _supported(v, [model.evidence[k].quote])) and not _supported(v, quotes)]
    return len(filled), missing


def evidence_coverage(doc: ExtractedDocument, line_no: int | None) -> tuple[float, list[str]]:
    total, missing = _missing(doc, _DOC_SKIP, "doc")
    charge = next((c for c in doc.charges if c.line_no == line_no), None)
    if charge is not None:
        n, m = _missing(charge, _CHARGE_SKIP, "charge")
        total, missing = total + n, missing + m
    if not total:
        return 1.0, []
    return (total - len(missing)) / total, sorted(missing)


def ocr_confidence(rec: ChargeRecord, raw_docs: dict[str, RawDocument]) -> float:
    values = [d.ocr_confidence for sid in rec.source_ids if (d := raw_docs.get(sid)) and d.ocr_confidence is not None]
    return min(values) if values else 1.0


def score_record(rec: ChargeRecord, doc: ExtractedDocument, raw_docs: dict[str, RawDocument],
                 warnings: int = 0, rounds: int = 1) -> Confidence:
    ocr = ocr_confidence(rec, raw_docs)
    coverage, missing = evidence_coverage(doc, rec.line_no)
    extra_rounds = max(rounds - 1, 0)
    penalty = WARNING_PENALTY ** warnings * RETRY_PENALTY ** extra_rounds
    notes = []
    if missing:
        notes.append(f"缺少证据: {', '.join(missing)}")
    if warnings:
        notes.append(f"数据校验提示 {warnings} 条")
    if extra_rounds:
        notes.append(f"抽取被退回 {extra_rounds} 次")
    return Confidence(round(ocr * coverage * penalty, 4), ocr, coverage, penalty, notes)
