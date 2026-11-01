"""解析层公共结构。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from bill_agent.models import RawDocument

if TYPE_CHECKING:
    from .router import Router


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class InputFile:
    """进入 Router 的一个文件（磁盘文件或邮件附件）。"""

    data: bytes
    name: str                    # 文件名（用于扩展名兜底判断）
    source_path: str             # 溯源路径，附件形如 "mail.eml#attachments/DN.pdf"
    parent_id: str | None = None
    depth: int = 0               # 嵌套层数（EML 附件 +1）
    content_type: str | None = None  # 邮件附件声明的 MIME 类型

    @property
    def source_id(self) -> str:
        return sha256(self.data)


@dataclass
class SkippedItem:
    source_path: str
    reason: str


@dataclass
class BillJSON:
    """已存在的账单 JSON（四.5 用于匹配和更新），原样保留，由账单库模块导入。"""

    source_path: str
    source_id: str
    records: list[dict[str, Any]]


@dataclass
class ParseResult:
    documents: list[RawDocument] = field(default_factory=list)
    bill_jsons: list[BillJSON] = field(default_factory=list)
    skipped: list[SkippedItem] = field(default_factory=list)

    def extend(self, other: "ParseResult") -> None:
        self.documents.extend(other.documents)
        self.bill_jsons.extend(other.bill_jsons)
        self.skipped.extend(other.skipped)


class Parser(Protocol):
    def parse(self, f: InputFile, router: "Router") -> ParseResult: ...


class ParseError(Exception):
    """文件损坏、格式不支持等。Router 捕获后记入 skipped，不中断整批处理。"""
