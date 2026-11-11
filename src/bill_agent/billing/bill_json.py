"""已有账单 JSON → BillRecord 的字段映射。

还没有拿到题目给的"已有账单 JSON"样例，先支持两种形状：
1. 标准输出格式（6.9）：{"document_info": {...}, "transaction_party": {...}, "charge_details": {...},
   "financial_attributes": {...}, "audit_trail": {...}}，即本程序输出的结果可以直接再导入；
2. 平铺格式：键名就是 BillRecord 的字段名，另外接受 bl_no / container_no（单个值）、charge_item_raw 等别名。

不认识的键丢掉并记 warning，不报错；值不合法（币种不是三位代码、日期格式不对等）抛 ValueError。
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError

from bill_agent.models import BillRecord

_NAMESPACE = uuid.UUID("6f1c2a52-3b7e-4d57-9a43-0c1f6f0b2d11")
_SECTIONS = ("document_info", "transaction_party", "charge_details", "financial_attributes")
_ALIASES = {"charge_item_raw": "original_charge_item", "id": "record_id"}
_SINGULAR = {"bl_no": "bl_nos", "container_no": "container_nos"}
_IGNORED = {"operation"}


def _flatten(raw: dict[str, Any]) -> dict[str, Any]:
    if not any(isinstance(raw.get(s), dict) for s in _SECTIONS):
        return dict(raw)
    flat: dict[str, Any] = {}
    for k, v in raw.items():
        if k in _SECTIONS and isinstance(v, dict):
            flat.update(v)
        elif k == "audit_trail" and isinstance(v, dict):
            if v.get("matched_record_id"):
                flat.setdefault("record_id", v["matched_record_id"])
            if v.get("review_reasons"):
                flat["review_reasons"] = v["review_reasons"]
        else:
            flat[k] = v
    return flat


def bill_from_json(raw: dict[str, Any], source_id: str, index: int) -> tuple[BillRecord, list[str]]:
    warnings: list[str] = []
    data: dict[str, Any] = {}
    for k, v in _flatten(raw).items():
        k = _ALIASES.get(k, k)
        if k in _IGNORED:
            continue
        if k in _SINGULAR:
            if v not in (None, ""):
                data.setdefault(_SINGULAR[k], []).extend(v if isinstance(v, list) else [v])
            continue
        if k not in BillRecord.model_fields:
            warnings.append(f"不认识的字段 {k!r}，已忽略")
            continue
        if k in _SINGULAR.values() and isinstance(v, str):
            v = [v]
        data[k] = v

    if not data.get("original_charge_item"):
        if not data.get("charge_item"):
            raise ValueError("缺少费用名称（charge_item / original_charge_item）")
        data["original_charge_item"] = data["charge_item"]
        warnings.append("没有原始费用名称，用 charge_item 代替")
    data.setdefault("record_id", str(uuid.uuid5(_NAMESPACE, f"{source_id}#{index}")))
    data.setdefault("source_ids", [source_id])
    data.pop("version", None)           # 版本号由账单库维护
    data.pop("updated_at", None)
    try:
        return BillRecord.model_validate(data), warnings
    except ValidationError as e:
        msgs = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
        raise ValueError(msgs) from None
