"""日期标准化：原文 -> date，去掉时间部分。

- `2026-10-24`、`2026/10/24`、`2026年10月24日`：年在前，无歧义。
- `24 OCT 2026`、`Oct 24, 2026`：月份是英文，无歧义。
- `10-24-2026`、`10/03/2025`：年在后，默认月/日/年（用户确认：只有年月日、月日年两种默认格式，
  按 4 位年份的位置区分）。例外：第一段大于 12 时（如 `24/10/2026`）按月/日读不成有效日期，
  只能是日/月，按日/月读。
- 年份必须是 4 位；`10/03/25` 这类 2 位年份分不清年在哪一段，返回 None 并转人工。
"""

from __future__ import annotations

import re
from datetime import date

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_YMD = re.compile(r"(?<!\d)(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})(?!\d)")
_NUMERIC = re.compile(r"(?<!\d)(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{4})(?!\d)")
_DAY_MON = re.compile(r"(?i)(?<!\d)(\d{1,2})[\s\-/.]*([a-z]{3})[a-z]*\.?[\s\-/.,]*(\d{4})(?!\d)")
_MON_DAY = re.compile(r"(?i)\b([a-z]{3})[a-z]*\.?[\s\-/.]*(\d{1,2})(?:st|nd|rd|th)?[\s,\-/.]*(\d{4})(?!\d)")

MIN_YEAR, MAX_YEAR = 1990, 2100


def _make(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d) if MIN_YEAR <= y <= MAX_YEAR else None
    except ValueError:
        return None


def normalize_date(raw: str | None) -> tuple[date | None, str | None]:
    """返回 (日期, 问题)。原文为空时两者都是 None。"""
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
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        # 年在后默认月/日/年；第一段大于 12 时月/日读不通，只能是日/月
        result = _make(y, b, a) if a > 12 >= b else _make(y, a, b)
    else:
        return None, f"日期 {raw!r} 无法解析"
    if result is None:
        return None, f"日期 {raw!r} 不是有效日期，或年份不在 {MIN_YEAR}–{MAX_YEAR} 内"
    return result, None
