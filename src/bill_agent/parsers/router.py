"""Parser Router：识别类型 → 分发给对应解析器 → 汇总 ParseResult。

- 所有格式最终都产出 RawDocument（已有账单 JSON 除外，它进入 bill_jsons）
- EML 附件通过 router.route(...) 递归回到这里
- 同一文件内容（sha256 相同）只解析一次，例如邮件正文和转发邮件里重复的附件
- 单个文件失败不中断整批，记入 skipped 并写明原因
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .base import InputFile, Parser, ParseError, ParseResult, SkippedItem
from .blob_store import BlobStore
from .detect import DetectedType, detect, mime_for

log = logging.getLogger(__name__)


@dataclass
class RouterConfig:
    max_depth: int = 5                     # EML 嵌套层数上限，防止异常邮件无限递归
    max_file_bytes: int = 50 * 1024 * 1024
    skip_inline_images_below: int = 20 * 1024  # 邮件正文内嵌的小图片（logo、签名）不送 OCR
    blob_dir: str = "data/blobs"               # PDF / 图片原文件存档目录


class Router:
    def __init__(self, parsers: dict[DetectedType, Parser], config: RouterConfig | None = None):
        self.parsers = parsers
        self.config = config or RouterConfig()
        self._seen: dict[str, str] = {}   # source_id -> 首次出现的 source_path

    def reset(self) -> None:
        self._seen.clear()

    # ---------- 入口 ----------

    def route_paths(self, paths: list[str | Path]) -> ParseResult:
        """处理磁盘上的文件或目录（目录递归展开，按文件名排序保证结果可复现）。

        source_path 使用相对路径并统一用 "/" 分隔，不依赖本机目录和操作系统：
        - 传入目录：相对于该目录，如 "sub/DN.pdf"
        - 传入单个文件：只用文件名，如 "DN.pdf"
        - 邮件附件在此基础上追加 "#attachments/<文件名>"
        """
        result = ParseResult()
        for p in paths:
            p = Path(p)
            if p.is_dir():
                files = [(x, x.relative_to(p).as_posix()) for x in sorted(p.rglob("*")) if x.is_file()]
            else:
                files = [(p, p.name)]
            for fp, rel in files:
                if fp.name.startswith("."):
                    continue
                try:
                    data = fp.read_bytes()
                except OSError as e:
                    result.skipped.append(SkippedItem(rel, f"无法读取文件: {e}"))
                    continue
                result.extend(self.route(InputFile(data=data, name=fp.name, source_path=rel)))
        return result

    def route(self, f: InputFile) -> ParseResult:
        if f.depth > self.config.max_depth:
            return ParseResult(skipped=[SkippedItem(f.source_path, f"嵌套层数超过 {self.config.max_depth}")])
        if not f.data:
            return ParseResult(skipped=[SkippedItem(f.source_path, "空文件")])
        if len(f.data) > self.config.max_file_bytes:
            return ParseResult(skipped=[SkippedItem(f.source_path, f"文件过大: {len(f.data)} bytes")])

        sid = f.source_id
        if sid in self._seen:
            return ParseResult(skipped=[SkippedItem(f.source_path, f"与 {self._seen[sid]} 内容相同，已去重")])
        self._seen[sid] = f.source_path

        t = detect(f.data, f.name, f.content_type)
        parser = self.parsers.get(t)
        if parser is None:
            return ParseResult(skipped=[SkippedItem(f.source_path, f"不支持的文件类型: {t.value}")])
        try:
            result = parser.parse(f, self)
        except ParseError as e:
            return ParseResult(skipped=[SkippedItem(f.source_path, str(e))])
        except Exception as e:  # 第三方库对损坏文件可能抛任意异常
            log.exception("解析失败: %s", f.source_path)
            return ParseResult(skipped=[SkippedItem(f.source_path, f"解析失败: {type(e).__name__}: {e}")])
        # 补齐本文件对应文档的文件元信息（附件产出的子文档已在递归中补齐）
        meta = {"file_name": f.name, "mime_type": mime_for(f.data, t), "file_size": len(f.data)}
        result.documents = [
            d.model_copy(update=meta) if d.source_id == sid and d.file_name is None else d
            for d in result.documents
        ]
        log.info("parsed %s as %s -> %d docs", f.source_path, t.value, len(result.documents))
        return result


def build_default_router(config: RouterConfig | None = None) -> Router:
    """组装默认 Router。PDF / 图片登记为 PENDING_OCR，原文件存入 config.blob_dir。"""
    from .eml import EmlParser
    from .excel import ExcelParser
    from .json_bill import BillJsonParser
    from .pdf_image import PdfImageParser
    from .word import WordParser

    config = config or RouterConfig()
    excel, word = ExcelParser(), WordParser()
    pdf_image = PdfImageParser(BlobStore(config.blob_dir))
    parsers: dict[DetectedType, Parser] = {
        DetectedType.EML: EmlParser(),
        DetectedType.XLSX: excel,
        DetectedType.XLS: excel,
        DetectedType.DOCX: word,
        DetectedType.DOC: word,
        DetectedType.PDF: pdf_image,
        DetectedType.IMAGE: pdf_image,
        DetectedType.BILL_JSON: BillJsonParser(),
    }
    return Router(parsers, config)
