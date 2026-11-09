"""费用名称标准化（四.3）。

只改写标准化表中列出的名称：先把大小写、空格、标点统一，再与表中的原始名称和标准名称做精确匹配，
命中则输出标准名称。表中没有的名称原样照抄（如 `H/C`、`OCA17 On Carriage Additional - US Em`），
不做模糊匹配，不交给 LLM 改写，也不因此转人工。
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import DEFAULT_CONFIG_DIR, load_mapping


def charge_key(name: str) -> str:
    """标点换成空格，再合并空格、转小写：`H/D Fee`、`h-d  fee` -> `h d fee`。"""
    return " ".join(re.sub(r"[\W_]+", " ", name).lower().split())


class ChargeNormalizer:
    def __init__(self, config_dir: Path = DEFAULT_CONFIG_DIR):
        self.table: dict[str, str] = {}
        for standard, aliases in load_mapping("charge_mapping.yaml", config_dir).items():
            for name in [standard, *aliases]:
                key = charge_key(name)
                if self.table.get(key, standard) != standard:
                    raise ValueError(f"charge_mapping.yaml 中 {name!r} 同时对应多个标准名称")
                self.table[key] = standard

    def __call__(self, raw: str) -> str:
        """命中表时返回标准名称，否则原样返回原始名称。"""
        return self.table.get(charge_key(raw), raw)
