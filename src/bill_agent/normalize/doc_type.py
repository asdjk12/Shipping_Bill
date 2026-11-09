"""单据类型标准化：按关键词把标题原文归到 Debit Note / Credit Note / Invoice / Other。

按顺序判断，先命中的生效：credit -> Credit Note；invoice -> Invoice（`DEBIT INVOICE` 归 Invoice）；
debit -> Debit Note。都没有（如 CMA 的 `GENERAL NOTICE`）归 Other。原文为空时为 None。
"""

from __future__ import annotations

import re

from bill_agent.models.enums import DocumentType

_RULES = [
    (re.compile(r"(?i)\bcredit\b|贷记|贷项"), DocumentType.CREDIT_NOTE),
    (re.compile(r"(?i)\binvoice\b|发票"), DocumentType.INVOICE),
    (re.compile(r"(?i)\bdebit\b|借记|借项"), DocumentType.DEBIT_NOTE),
]


def normalize_document_type(raw: str | None) -> DocumentType | None:
    if raw is None or not raw.strip():
        return None
    for pattern, doc_type in _RULES:
        if pattern.search(raw):
            return doc_type
    return DocumentType.OTHER
