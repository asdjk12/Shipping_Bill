"""OCR 接入接口（只定义契约，不包含实现）。

视觉模型 OCR 服务实现 OCRBackend.recognize 即可：

    class VisionOCR:
        name = "qwen-vl-max"                       # 记入 RawDocument.ocr_engine
        def recognize(self, doc: RawDocument, data: bytes) -> OCRResult:
            ...  # doc.mime_type / doc.file_name 可用于构造模型输入
            return OCRResult(text=..., tables=[[["DESCRIPTION", "AMOUNT"], ["THC", "120"]]], confidence=0.9)

    docs = run_ocr(parse_result.documents, VisionOCR())

run_ocr 负责：只处理 PENDING_OCR / OCR_FAILED 的文档；从 file_ref 读原文件；
把表格交给与 Excel/Word 相同的表头定位逻辑；成功 -> OCR_DONE，异常 -> OCR_FAILED。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from bill_agent.models import DocStatus, RawDocument, RawTable
from bill_agent.parsers.blob_store import BlobStore
from bill_agent.parsers.table_utils import build_table

log = logging.getLogger(__name__)


@dataclass
class OCRResult:
    text: str
    # 二维表格（行 × 单元格），第一行是否表头不要求，由 build_table 统一定位；
    # 也可以直接给 RawTable
    tables: list[list[list[str | None]] | RawTable] = field(default_factory=list)
    confidence: float | None = None     # 0~1，没有就留空
    raw_response: dict | None = None    # 模型原始输出，用于审计
    warnings: list[str] = field(default_factory=list)


class OCRBackend(Protocol):
    name: str

    def recognize(self, doc: RawDocument, data: bytes) -> OCRResult: ...


def run_ocr(docs: list[RawDocument], backend: OCRBackend) -> list[RawDocument]:
    """对待 OCR 的文档执行识别，其他文档原样返回；顺序不变。"""
    out: list[RawDocument] = []
    for doc in docs:
        if doc.status not in (DocStatus.PENDING_OCR, DocStatus.OCR_FAILED):
            out.append(doc)
            continue
        try:
            r = backend.recognize(doc, BlobStore.get(doc.file_ref))
            tables = [
                t if isinstance(t, RawTable) else build_table(f"ocr-table-{i}", t)
                for i, t in enumerate(r.tables, start=1)
            ]
            out.append(doc.complete_ocr(
                text=r.text,
                engine=backend.name,
                tables=[t for t in tables if t.rows or t.header],
                confidence=r.confidence,
                raw_response=r.raw_response,
                warnings=r.warnings,
            ))
        except Exception as e:
            log.exception("OCR 失败: %s", doc.source_path)
            out.append(doc.fail_ocr(f"{type(e).__name__}: {e}", engine=getattr(backend, "name", None)))
    return out
