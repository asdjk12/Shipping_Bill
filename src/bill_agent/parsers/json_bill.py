"""已存在的账单 JSON（三、输入数据：用于账单匹配和更新）。

这里只负责读入并做最基本的形状检查，原样交给账单库模块导入；
字段映射要等拿到样例 JSON 后在 billing/repository.py 里实现。
支持三种形状：[ {...}, ... ]、{"bills"|"records"|"data": [...]}、单个 {...}。
"""

from __future__ import annotations

import json

from .base import BillJSON, InputFile, ParseError, ParseResult

_LIST_KEYS = ("bills", "records", "data", "items")


class BillJsonParser:
    def parse(self, f: InputFile, router) -> ParseResult:
        try:
            payload = json.loads(f.data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ParseError(f"JSON 格式错误: {e}") from e

        if isinstance(payload, dict):
            for k in _LIST_KEYS:
                if isinstance(payload.get(k), list):
                    payload = payload[k]
                    break
            else:
                payload = [payload]
        if not isinstance(payload, list) or not all(isinstance(x, dict) for x in payload):
            raise ParseError("账单 JSON 应为对象数组")
        return ParseResult(bill_jsons=[BillJSON(f.source_path, f.source_id, payload)])
