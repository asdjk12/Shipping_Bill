"""日期标准化：原文 -> date，去掉时间部分。

- `2026-10-24`、`2026/10/24`、`2026年10月24日`：年在前，无歧义。
- `24 OCT 2026`、`Oct 24, 2026`：月份是英文，无歧义。
- `10-24-2026`、`10/03/2025`：数字月日。第一段大于 12 按日/月，第二段大于 12 按月/日；
  两段都不超过 12 时，看同一份原文里其他日期能否确定顺序（context），仍不能确定就返回 None 并转人工，
  不按签发方所在国家推测。
"""

from __future__ import annotations

import re
from datetime import date

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_YMD = re.compile(r"(?<!\d)(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})(?!\d)")
_NUMERIC = re.compile(r"(?<!\d)(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{4}|\d{2})(?!\d)")
_DAY_MON = re.compile(r"(?i)(?<!\d)(\d{1,2})[\s\-/.]*([a-z]{3})[a-z]*\.?[\s\-/.,]*(\d{4})(?!\d)")
_MON_DAY = re.compile(r"(?i)\b([a-z]{3})[a-z]*\.?[\s\-/.]*(\d{1,2})(?:st|nd|rd|th)?[\s,\-/.]*(\d{4})(?!\d)")

MIN_YEAR, MAX_YEAR = 1990, 2100


def _make(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d) if MIN_YEAR <= y <= MAX_YEAR else None
    except ValueError:
        return None


def numeric_order(text: str) -> str | None:
    """从一段文字里的数字日期推断月日顺序：'MD' 或 'DM'；判断不出或互相矛盾时为 None。"""
    orders = set()
    for a, b, _ in _NUMERIC.findall(text):
        a, b = int(a), int(b)
        if a > 12 >= b >= 1:
            orders.add("DM")
        elif b > 12 >= a >= 1:
            orders.add("MD")
    return orders.pop() if len(orders) == 1 else None


def normalize_date(raw: str | None, context: str | None = None) -> tuple[date | None, str | None]:
    """返回 (日期, 问题)。原文为空时两者都是 None。context 是同一份单据的原文，用来判断月日顺序。"""
    if raw is None or not raw.strip():
        return None, None
    s = raw.strip()
    if m := _YMD.search(s):
        result = _make(int(m[1]), int(m[2]), int(m[3]))
    elif (m := _DAY_MON.search(s)) and m[2].lower() in _MONTHS:
        result = _make(int(m[3]), _MONTHS[m[2].lower()], int(m[1]))
    elif (m := _MON_DAY.search(s)) and m[1].lower() in _MONTHS:
        result = _make(int(m[3]), _MONTHS[m[1].lower()], int(m[2]))
    elif m := _NUMERIC.search(s):
        a, b = int(m[1]), int(m[2])
        y = int(m[3]) + 2000 if len(m[3]) == 2 else int(m[3])
        if a > 12 or b > 12:
            order = "DM" if a > 12 else "MD"
        else:
            order = numeric_order(context) if context else None
        if order is None:
            return None, f"日期 {raw!r} 无法确定是月/日还是日/月"
        result = _make(y, a, b) if order == "MD" else _make(y, b, a)
    else:
        return None, f"日期 {raw!r} 无法解析"
    if result is None:
        return None, f"日期 {raw!r} 不是有效日期，或年份不在 {MIN_YEAR}–{MAX_YEAR} 内"
    return result, None
