"""通用类型与工具：金额解析、证据。"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer

_AMOUNT_STRIP = re.compile(r"[,\s ]")


def parse_amount(value: Any) -> Decimal | None:
    """把 LLM / 表格中的金额转成 Decimal。

    只做无歧义的清洗：去掉千分位逗号和空格，把 "(40.00)" 视为负数。
    含字母（如 OCR 把 0 识别成 O 的 "4O.00"）时直接报错，
    交给后续校验环节处理，而不是在这里猜。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("金额不能是布尔值")
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        s = _AMOUNT_STRIP.sub("", value)
        negative = s.startswith("(") and s.endswith(")")
        if negative:
            s = s[1:-1]
        try:
            d = Decimal(s)
        except InvalidOperation:
            raise ValueError(f"无法解析为金额: {value!r}") from None
        if not d.is_finite():
            raise ValueError(f"金额不是有限数字: {value!r}")
        return -d if negative else d
    raise ValueError(f"不支持的金额类型: {type(value).__name__}")


# 内部用 Decimal 保证精度；输出 JSON 时转为数字（七.3 金额必须使用数字类型）
Amount = Annotated[
    Decimal,
    BeforeValidator(parse_amount),
    PlainSerializer(lambda d: float(d), return_type=float, when_used="json"),
]


class StrictModel(BaseModel):
    """所有模型的基类：禁止未声明字段，防止 LLM 输出多余键混进来。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Evidence(StrictModel):
    """字段值在原文中的出处（二.9 记录原始文本和处理依据；七.1 不得虚构数据）。"""

    source_id: str = Field(description="RawDocument.source_id")
    quote: str = Field(min_length=1, description="原文中的原样片段")
