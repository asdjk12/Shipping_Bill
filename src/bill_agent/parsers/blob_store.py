"""原文件存档（按内容寻址）。

PDF / 图片在解析阶段不识别，只把原文件存进来，RawDocument.file_ref 指向存档路径，
OCR 服务之后从这里读取。邮件附件没有磁盘路径，所以统一存档，而不是引用原始位置。
"""

from __future__ import annotations

from pathlib import Path


class BlobStore:
    def __init__(self, root: str | Path = "data/blobs"):
        self.root = Path(root)

    def put(self, data: bytes, sha256: str, suffix: str = "") -> str:
        """写入并返回路径；同一内容只存一份。"""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{sha256}{suffix}"
        if not path.exists():
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return str(path)

    @staticmethod
    def get(file_ref: str) -> bytes:
        return Path(file_ref).read_bytes()
