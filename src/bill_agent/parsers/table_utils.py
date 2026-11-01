"""表格公共处理：单元格格式化、空行空列清理、表头定位、表格转文本。"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal

from bill_agent.models import RawTable

# 表头关键词（四.1 Excel：读取工作表、表头及费用明细行）
HEADER_KEYWORDS = [
    "description", "charge", "charges", "item", "fee", "amount", "amt", "rate", "price",
    "qty", "quantity", "unit", "curr", "currency", "total", "base", "basis", "p/c", "pp/cc",
    "debit", "credit", "roe", "r.o.e", "container", "cntr",
    "费用", "费目", "项目", "名称", "金额", "单价", "数量", "币种", "币别", "单位", "合计", "小计", "汇率",
]
_KW_PATTERN = re.compile("|".join(re.escape(k) for k in sorted(HEADER_KEYWORDS, key=len, reverse=True)))


def format_cell(v: object) -> str | None:
    """单元格值转字符串。数字保留原精度且不带浮点尾巴（40.0 -> '40', 21.9 -> '21.9'）。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v:  # NaN
            return None
        if v.is_integer():
            return str(int(v))
        return format(Decimal(repr(v)).normalize(), "f")
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == time(0) else v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).replace("\r\n", "\n").strip()
    return s or None


def clean_grid(rows: list[list[str | None]]) -> list[list[str | None]]:
    """去掉全空行、去掉右侧全空列。"""
    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if not rows:
        return []
    width = max(
        (max((i + 1 for i, c in enumerate(r) if c not in (None, "")), default=0) for r in rows),
        default=0,
    )
    return [(r + [None] * width)[:width] for r in rows]


def header_score(row: list[str | None]) -> int:
    """一行中命中表头关键词的**不同**单元格值的个数。

    按不同值计数，是因为合并单元格展开后标题行会出现多个相同的值
    （如 5 个 "ACME DEBIT NOTE"），不能让它压过真正的表头。
    """
    # 表头行不含数字单元格；"TOTAL AMOUNT | 1,410.40 | CURRENCY | USD" 这类键值行不是表头
    if any(c and _NUMERIC.match(c.strip()) for c in row):
        return 0
    hits = {c.strip().lower() for c in row if c and len(c) <= 40 and _KW_PATTERN.search(c.lower())}
    return len(hits)


_NUMERIC = re.compile(r"^[-+(]?[\d,]*\.?\d+\)?$")


def find_header(rows: list[list[str | None]], scan: int = 30, min_score: int = 2) -> int | None:
    """在前 scan 行中找关键词命中最多的一行作为表头；命中不足 min_score 返回 None。"""
    best_i, best = None, 0
    for i, r in enumerate(rows[:scan]):
        s = header_score(r)
        if s > best:
            best_i, best = i, s
    return best_i if best >= min_score else None


def build_table(name: str | None, rows: list[list[str | None]]) -> RawTable:
    rows = clean_grid(rows)
    h = find_header(rows)
    if h is None:
        return RawTable(name=name, header=None, rows=rows)
    return RawTable(name=name, header=[c or "" for c in rows[h]], rows=rows[h + 1:])


def grid_to_text(rows: list[list[str | None]]) -> str:
    """表格转纯文本（单元格用 ' | ' 分隔），供 LLM 阅读上下文和证据定位。"""
    return "\n".join(" | ".join(c or "" for c in r).rstrip(" |") for r in clean_grid(rows))
