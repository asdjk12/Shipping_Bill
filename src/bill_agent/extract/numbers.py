"""代码层的数字检查：只做查找和算术，不调用 LLM。

- numbers_in：取出文本中完整的数字，比对时 "50" 不会误命中 "150"
- check_numbers：抽取结果内部的算术一致性，不看原文
"""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal

from bill_agent.context import squash
from bill_agent.models import ExtractedDocument

TOLERANCE = Decimal("0.01")

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> set[Decimal]:
    return {Decimal(m.replace(",", "")) for m in _NUMBER.findall(text)}


def _differs(a: Decimal, b: Decimal) -> bool:
    # 比较绝对值：贷记行是负数，而单据上的合计通常写成正数
    return abs(abs(a) - abs(b)) > TOLERANCE


def check_numbers(doc: ExtractedDocument) -> list[str]:
    """数量 × 单价 = 金额；费用行加总 = 小计；小计 + 税额 = 总计。缺项或币种对不上时跳过。"""
    errors = []
    sums: dict[str, Decimal] = defaultdict(Decimal)
    for c in doc.charges:
        if None not in (c.quantity, c.unit_price, c.amount) and _differs(c.quantity * c.unit_price, c.amount):
            errors.append(f"费用行 {c.line_no}: 数量 {c.quantity} × 单价 {c.unit_price} "
                          f"= {c.quantity * c.unit_price}，与金额 {c.amount} 不一致")
        if c.amount is not None:
            sums[squash(c.currency_raw or doc.currency_raw or "")] += c.amount

    # 单币种时不要求写法相同："$" 的费用行可以和 "USD" 的合计比较
    only_one = len(sums) == 1 and len(doc.totals) == 1
    for t in doc.totals:
        key = next(iter(sums)) if only_one else squash(t.currency_raw or doc.currency_raw or "")
        if key not in sums:
            continue
        total = sums[key]
        label = t.currency_raw or "无币种"
        if t.subtotal is not None and _differs(total, t.subtotal):
            errors.append(f"合计[{label}]: 费用行加总 {total} 与小计 {t.subtotal} 不一致")
        base = abs(total if t.subtotal is None else t.subtotal)
        if t.total_amount is not None and _differs(base + (t.tax_amount or 0), t.total_amount):
            errors.append(f"合计[{label}]: 加总 {base} + 税额 {t.tax_amount or 0} 与总计 {t.total_amount} 不一致")
    return errors
