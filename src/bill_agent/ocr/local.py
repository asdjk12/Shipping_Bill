"""本地 OCR 后端：PDF 文字层优先，扫描页和图片用 PaddleOCR。

    docs = run_ocr(parse_result.documents, LocalOCR(device="cpu"))

按页处理：
- PDF：有可用文字层的页直接读取（engine "pdf-text-layer"，置信度 1.0）；
  其余页渲染成图片交给 PaddleOCR
- 图片：每一帧（多页 TIFF）交给 PaddleOCR
- 文档置信度取各页最小值：任何一页识别不可靠，整份文档都需要谨慎对待
- OCRResult.engine 记录实际用到的引擎，如 "pdf-text-layer+paddleocr/PP-StructureV3"
"""

from __future__ import annotations

import io
from typing import Protocol

from bill_agent.models import FileType, RawDocument, RawTable
from bill_agent.parsers.table_utils import build_table

from .interface import OCRResult
from .paddle import PageOCR, PaddleStructure
from .pdf_text import read_pdf_pages, render_pages

TEXT_LAYER = "pdf-text-layer"


class ImageOCR(Protocol):
    name: str

    def recognize_image(self, image) -> PageOCR: ...


def load_images(data: bytes) -> list:
    """图片转 RGB PIL 图片列表；多页 TIFF 每帧一张。"""
    from PIL import Image, ImageSequence

    with Image.open(io.BytesIO(data)) as im:
        return [frame.convert("RGB") for frame in ImageSequence.Iterator(im)]


class LocalOCR:
    name = "local-ocr"

    def __init__(self, image_ocr: ImageOCR | None = None, *, device: str = "cpu", dpi: int = 200):
        self.image_ocr = image_ocr or PaddleStructure(device=device)
        self.dpi = dpi

    def recognize(self, doc: RawDocument, data: bytes) -> OCRResult:
        pages: list[tuple[int, str, PageOCR]] = []   # (页码, 来源引擎, 结果)
        warnings: list[str] = []

        if doc.file_type is FileType.PDF:
            pdf_pages = read_pdf_pages(data)
            if not pdf_pages:
                raise ValueError("PDF 没有页面")
            scanned = [p.number for p in pdf_pages if not p.usable]
            images = render_pages(data, scanned, self.dpi) if scanned else {}
            for p in pdf_pages:
                if p.usable:
                    pages.append((p.number, TEXT_LAYER, PageOCR(text=p.text, confidence=1.0)))
                else:
                    warnings.append(f"第 {p.number} 页{p.reason}，改用 {self.image_ocr.name} 识别")
                    pages.append((p.number, self.image_ocr.name, self.image_ocr.recognize_image(images[p.number])))
        else:
            for i, image in enumerate(load_images(data), start=1):
                pages.append((i, self.image_ocr.name, self.image_ocr.recognize_image(image)))

        multi = len(pages) > 1
        texts: list[str] = []
        tables: list[RawTable] = []
        for n, _, page in pages:
            texts.append(f"--- page {n} ---\n{page.text}" if multi else page.text)
            tables += [build_table(f"page-{n}-table-{k}", g) for k, g in enumerate(page.tables, start=1)]
            prefix = f"第 {n} 页" if multi else ""
            warnings += [f"{prefix}低置信度文本 {t!r}（{s:.2f}）" for t, s in page.low_conf_lines]

        confs = [p.confidence for _, _, p in pages if p.confidence is not None]
        engines = list(dict.fromkeys(e for _, e, _ in pages))
        return OCRResult(
            text="\n\n".join(texts),
            tables=tables,
            confidence=min(confs) if confs else None,
            raw_response={"pages": [{"page": n, "engine": e, **(p.raw or {})} for n, e, p in pages]},
            warnings=warnings,
            engine="+".join(engines),
        )
