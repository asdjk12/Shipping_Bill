"""读取 config/ 下的标准化表。默认目录是仓库根目录的 config/，可传入其他目录。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


@lru_cache(maxsize=None)
def load_mapping(name: str, config_dir: Path = DEFAULT_CONFIG_DIR) -> dict[str, list[str]]:
    """读取 `标准值: [别名...]` 形式的 YAML。"""
    data = yaml.safe_load((Path(config_dir) / name).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{name} 应为 `标准值: [别名...]` 的映射")
    return {str(k): [str(v) for v in (vs or [])] for k, vs in data.items()}
