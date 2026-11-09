"""付款方式标准化（四.2 PP 预付 / CC 到付）。

抽取核验（extract.payment_term_problem）用同一张表判断值是不是付款方式，两处口径一致。
单字母 P / C 已确认分别为 PP / CC。
"""

from __future__ import annotations

from bill_agent.context import squash
from bill_agent.models.enums import PaymentTerm

PAYMENT_TERM_MAP: dict[str, PaymentTerm] = {
    **dict.fromkeys(["p", "pp", "prepaid", "freightprepaid", "预付", "寄付"], PaymentTerm.PP),
    **dict.fromkeys(["c", "cc", "collect", "freightcollect", "到付"], PaymentTerm.CC),
}
PAYMENT_TERMS = frozenset(PAYMENT_TERM_MAP)


def normalize_payment_term(raw: str | None) -> PaymentTerm | None:
    """无法识别时返回 None，不转人工（抽取核验已经拦过不合格的写法）。"""
    return PAYMENT_TERM_MAP.get(squash(raw)) if raw else None
