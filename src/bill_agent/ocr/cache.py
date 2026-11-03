"""OCR 结果缓存（按文件内容哈希）。

CPU 上 PaddleOCR 识别一张图约一分钟；同一文件反复提问、反复跑评测时直接读缓存。
缓存键是 source_id（文件 SHA256）+ 后端名称，文件内容不变结果就不变（幂等）。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path

from bill_agent.models import RawDocument, RawTable

from .interface import OCRBackend, OCRResult


class CachedOCR:
    def __init__(self, backend: OCRBackend, cache_dir: str | Path = "data/ocr_cache"):
        self.backend = backend
        self.name = backend.name
        self.dir = Path(cache_dir)

    def _path(self, doc: RawDocument) -> Path:
        return self.dir / f"{doc.source_id}.{re.sub(r'[^A-Za-z0-9._-]', '_', self.name)}.json"

    def recognize(self, doc: RawDocument, data: bytes) -> OCRResult:
        path = self._path(doc)
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            d["tables"] = [RawTable.model_validate(t) if isinstance(t, dict) else t for t in d["tables"]]
            return OCRResult(**d)
        r = self.backend.recognize(doc, data)
        d = asdict(r)
        d["tables"] = [t.model_dump() if isinstance(t, RawTable) else t for t in r.tables]
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return r
