"""PDF / 图片：解析阶段只登记，不识别。

产出 status=PENDING_OCR 的 RawDocument：原文件存档到 file_ref，text / tables 为空。
OCR 服务（视觉模型）稍后读取 file_ref，通过 RawDocument.complete_ocr() 回填结果。
"""

from __future__ import annotations

from bill_agent.models import DocStatus, FileType, RawDocument

from .base import InputFile, ParseResult
from .blob_store import BlobStore
from .detect import DetectedType, detect, suffix_for


class PdfImageParser:
    def __init__(self, blob_store: BlobStore):
        self.blobs = blob_store

    def parse(self, f: InputFile, router) -> ParseResult:
        kind = detect(f.data, f.name, f.content_type)
        file_ref = self.blobs.put(f.data, f.source_id, suffix_for(f.data, kind))
        doc = RawDocument(
            source_id=f.source_id,
            source_path=f.source_path,
            parent_id=f.parent_id,
            file_type=FileType.IMAGE if kind is DetectedType.IMAGE else FileType.PDF,
            status=DocStatus.PENDING_OCR,
            file_ref=file_ref,
        )
        return ParseResult(documents=[doc])
