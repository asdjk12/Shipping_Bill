"""抽取结果与 golden 标注逐字段对比。

- 文本字段忽略空白和大小写；公司名再忽略标点（"CO.LLC" 与 "Co., LLC" 相同）；列表字段（提单号、箱号）按集合比较
- 费用行按 line_no 对齐，合计按币种对齐；金额按数值比较
- evidence、source_id 不参与比较（证据写法可以不同，已由抽取核验保证能在原文中找到）
"""

from __future__ import annotations

from dataclasses import dataclass

from bill_agent.context import loose, squash
from bill_agent.models import ExtractedDocument

DOC_FIELDS = ["document_type_raw", "reference_no", "document_date_raw", "issuer", "recipient",
              "payee", "payer", "shipper", "consignee", "carrier", "booking_no", "currency_raw"]
PARTY_FIELDS = {"issuer", "recipient", "payee", "payer", "shipper", "consignee", "carrier"}
LIST_FIELDS = ["bl_nos", "container_nos"]
CHARGE_FIELDS = ["charge_name_raw", "quantity", "unit_price", "amount", "currency_raw", "payment_term_raw"]
TOTAL_FIELDS = ["subtotal", "tax_amount", "total_amount"]


@dataclass
class Diff:
    field: str
    expected: object
    actual: object


def _eq(a, b) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return squash(a) == squash(b)
    return a == b


def compare(expected: ExtractedDocument, actual: ExtractedDocument) -> tuple[int, list[Diff]]:
    """返回 (比较的字段数, 不一致的字段)。"""
    diffs: list[Diff] = []
    n = 0

    def check(name, e, a):
        nonlocal n
        n += 1
        if not _eq(e, a):
            diffs.append(Diff(name, e, a))

    for f in DOC_FIELDS:
        e, a = getattr(expected, f), getattr(actual, f)
        if f in PARTY_FIELDS and e and a and loose(e) == loose(a):
            a = e
        check(f, e, a)
    for f in LIST_FIELDS:
        check(f, sorted(map(squash, getattr(expected, f))), sorted(map(squash, getattr(actual, f))))

    exp_c = {c.line_no: c for c in expected.charges}
    act_c = {c.line_no: c for c in actual.charges}
    check("charges.count", len(exp_c), len(act_c))
    for no in sorted(exp_c.keys() | act_c.keys()):
        e, a = exp_c.get(no), act_c.get(no)
        if e is None or a is None:
            check(f"charges[{no}]", "存在" if e else None, "存在" if a else None)
            continue
        for f in CHARGE_FIELDS:
            check(f"charges[{no}].{f}", getattr(e, f), getattr(a, f))

    key = lambda t: squash(t.currency_raw or "")
    exp_t = {key(t): t for t in expected.totals}
    act_t = {key(t): t for t in actual.totals}
    for cur in sorted(exp_t.keys() | act_t.keys()):
        e, a = exp_t.get(cur), act_t.get(cur)
        if e is None or a is None:
            check(f"totals[{cur}]", "存在" if e else None, "存在" if a else None)
            continue
        for f in TOTAL_FIELDS:
            check(f"totals[{cur}].{f}", getattr(e, f), getattr(a, f))
    return n, diffs
