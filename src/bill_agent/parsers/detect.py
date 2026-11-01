"""文件类型识别：优先看文件头（magic bytes），扩展名只做兜底。

邮件附件经常出现扩展名错误或缺失（如 "invoice" 没有后缀、"DN.pdf" 实际是图片），
所以不能只信扩展名。
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from enum import Enum
from pathlib import PurePath


class DetectedType(str, Enum):
    EML = "eml"
    PDF = "pdf"
    IMAGE = "image"
    XLSX = "xlsx"
    XLS = "xls"
    DOCX = "docx"
    DOC = "doc"
    BILL_JSON = "bill_json"
    UNKNOWN = "unknown"


_EXT_MAP = {
    ".eml": DetectedType.EML,
    ".pdf": DetectedType.PDF,
    ".jpg": DetectedType.IMAGE, ".jpeg": DetectedType.IMAGE, ".png": DetectedType.IMAGE,
    ".tif": DetectedType.IMAGE, ".tiff": DetectedType.IMAGE, ".bmp": DetectedType.IMAGE,
    ".gif": DetectedType.IMAGE, ".webp": DetectedType.IMAGE,
    ".xlsx": DetectedType.XLSX, ".xlsm": DetectedType.XLSX,
    ".xls": DetectedType.XLS,
    ".docx": DetectedType.DOCX, ".docm": DetectedType.DOCX,
    ".doc": DetectedType.DOC,
    ".json": DetectedType.BILL_JSON,
}

_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_EML_HEADER = re.compile(
    rb"^(Return-Path|Received|From|To|Subject|Date|Message-ID|MIME-Version|Delivered-To|X-[\w-]+):",
    re.IGNORECASE | re.MULTILINE,
)


def _ext(name: str) -> str:
    return PurePath(name).suffix.lower()


def _detect_zip(data: bytes) -> DetectedType:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
    except zipfile.BadZipFile:
        return DetectedType.UNKNOWN
    if any(n.startswith("xl/") for n in names):
        return DetectedType.XLSX
    if any(n.startswith("word/") for n in names):
        return DetectedType.DOCX
    return DetectedType.UNKNOWN


def _detect_ole2(name: str, content_type: str | None) -> DetectedType:
    """.xls 和 .doc 都是 OLE2 容器，用扩展名 / MIME 区分；都没有时读取流名。"""
    ext = _ext(name)
    if ext in (".xls", ".doc"):
        return _EXT_MAP[ext]
    ct = (content_type or "").lower()
    if "excel" in ct or "spreadsheet" in ct:
        return DetectedType.XLS
    if "word" in ct or "msword" in ct:
        return DetectedType.DOC
    return DetectedType.UNKNOWN  # 交给 _sniff_ole2_streams


def _sniff_ole2_streams(data: bytes) -> DetectedType:
    # OLE2 目录项名以 UTF-16LE 存储
    if "Workbook".encode("utf-16-le") in data or "Book".encode("utf-16-le") in data:
        return DetectedType.XLS
    if "WordDocument".encode("utf-16-le") in data:
        return DetectedType.DOC
    return DetectedType.UNKNOWN


def _looks_like_json(data: bytes) -> bool:
    head = data.lstrip()[:1]
    if head not in (b"{", b"["):
        return False
    try:
        json.loads(data.decode("utf-8-sig"))
        return True
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False


def detect(data: bytes, name: str = "", content_type: str | None = None) -> DetectedType:
    if data.startswith(b"%PDF"):
        return DetectedType.PDF
    if (
        data.startswith(b"\x89PNG")
        or data.startswith(b"\xff\xd8\xff")
        or data.startswith((b"II*\x00", b"MM\x00*"))
        or data.startswith((b"GIF87a", b"GIF89a"))
        or data.startswith(b"BM")
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    ):
        return DetectedType.IMAGE
    if data.startswith(b"PK\x03\x04"):
        t = _detect_zip(data)
        if t is not DetectedType.UNKNOWN:
            return t
    if data.startswith(_OLE2):
        t = _detect_ole2(name, content_type)
        return t if t is not DetectedType.UNKNOWN else _sniff_ole2_streams(data)
    if _looks_like_json(data):
        return DetectedType.BILL_JSON
    # EML 是纯文本，只能靠头部特征 + 扩展名 / MIME
    if _ext(name) == ".eml" or (content_type or "").lower() == "message/rfc822":
        return DetectedType.EML
    if len(_EML_HEADER.findall(data[:4096])) >= 2:
        return DetectedType.EML
    return _EXT_MAP.get(_ext(name), DetectedType.UNKNOWN)


_IMAGE_SIGNATURES = [
    (b"\x89PNG", ".png", "image/png"),
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"II*\x00", ".tiff", "image/tiff"),
    (b"MM\x00*", ".tiff", "image/tiff"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
    (b"BM", ".bmp", "image/bmp"),
]

_MIME = {
    DetectedType.EML: "message/rfc822",
    DetectedType.PDF: "application/pdf",
    DetectedType.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    DetectedType.XLS: "application/vnd.ms-excel",
    DetectedType.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    DetectedType.DOC: "application/msword",
    DetectedType.BILL_JSON: "application/json",
}


def _image_sig(data: bytes) -> tuple[str, str]:
    for sig, ext, mime in _IMAGE_SIGNATURES:
        if data.startswith(sig):
            return ext, mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return "", "application/octet-stream"


def suffix_for(data: bytes, kind: DetectedType) -> str:
    """按真实内容给出扩展名（附件扩展名可能是错的）。"""
    if kind is DetectedType.IMAGE:
        return _image_sig(data)[0]
    return {DetectedType.PDF: ".pdf"}.get(kind, "")


def mime_for(data: bytes, kind: DetectedType) -> str | None:
    if kind is DetectedType.IMAGE:
        return _image_sig(data)[1]
    return _MIME.get(kind)
