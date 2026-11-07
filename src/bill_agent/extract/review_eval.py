"""审核模型的评测：golden 不应被误报，故意改坏的字段应被发现。

每种改坏方式只动语义，值仍然来自单据本身。这里直接调用审核模型，不经过代码核验，
所以 "签发方填成对方" 这类代码也能拦下的情况，同样用来检验审核模型自己的判断。
"""

from __future__ import annotations

from dataclasses import dataclass

from bill_agent.models import ExtractedDocument

from .reviewer import ReviewResult


@dataclass
class Mutation:
    name: str
    document: ExtractedDocument
    fields: tuple[str, ...]       # 命中其中任一字段算发现；"漏抽" 表示漏抽项


def _mutated(doc: ExtractedDocument, change) -> ExtractedDocument:
    data = doc.model_dump()
    change(data)
    return ExtractedDocument.model_validate(data)


def mutations(doc: ExtractedDocument) -> list[Mutation]:
    out: list[Mutation] = []

    other_party = next((p for p in (doc.recipient, doc.payer) if p and p != doc.issuer), None)
    if other_party:
        out.append(Mutation("签发方填成对方", _mutated(doc, lambda d: d.update(issuer=other_party)), ("issuer",)))

    if doc.reference_no and doc.bl_nos:
        def swap_ids(d):
            d["reference_no"], d["bl_nos"][0] = d["bl_nos"][0], d["reference_no"]
        out.append(Mutation("单据号与提单号互换", _mutated(doc, swap_ids), ("reference_no", "bl_nos")))

    if doc.shipper and doc.consignee:
        def swap_cargo(d):
            d["shipper"], d["consignee"] = d["consignee"], d["shipper"]
        out.append(Mutation("寄件人与收件人互换", _mutated(doc, swap_cargo), ("shipper", "consignee")))

    if doc.payee:
        out.append(Mutation("收款方漏填", _mutated(doc, lambda d: d.update(payee=None)), ("payee",)))

    target = next((c for c in doc.charges if None not in (c.quantity, c.unit_price)
                   and c.quantity != c.unit_price), None)
    if target:
        def swap_qty(d):
            c = next(x for x in d["charges"] if x["line_no"] == target.line_no)
            c["quantity"], c["unit_price"] = c["unit_price"], c["quantity"]
        fields = (f"charges[{target.line_no}].quantity", f"charges[{target.line_no}].unit_price")
        out.append(Mutation("数量与单价互换", _mutated(doc, swap_qty), fields))

    if doc.charges:
        out.append(Mutation("漏抽一行费用", _mutated(doc, lambda d: d["charges"].pop()), ("漏抽",)))
    return out


def detected(review: ReviewResult, mutation: Mutation) -> bool:
    return any(i.field == f or i.field.endswith(f" {f}") for i in review.issues for f in mutation.fields)
