"""解析层输出：RawDocument。

对应 四.1 多格式文档解析。所有解析器（EML / Excel / Word / OCR）
都产出 RawDocument，下游不关心它来自哪种格式。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .common import StrictModel
from .enums import FileType


class EmailMeta(StrictModel):
    """四.1 EML：解析主题、发件人、收件人。"""

    subject: str | None = None
    sender: str | None = None
    recipients: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    sent_at: datetime | None = None
    message_id: str | None = None


class RawTable(StrictModel):
    """一张表格。四.1 Excel：工作表、表头及费用明细行；Word：表格。"""

    name: str | None = Field(default=None, description="Excel sheet 名或 'table-1' 等")
    header: list[str] | None = Field(default=None, description="识别出的表头行")
    rows: list[list[str | None]] = Field(default_factory=list, description="明细行，单元格保留原文")


class RawDocument(StrictModel):
    source_id: str = Field(description="唯一 ID，建议使用文件内容 SHA256")
    source_path: str = Field(description="来源路径；附件形如 'mail.eml#attachments/DN.pdf'")
    parent_id: str | None = Field(default=None, description="EML 附件指向所属邮件的 source_id")
    file_type: FileType
    text: str = Field(default="", description="全文文本（正文 / OCR 文本）")
    tables: list[RawTable] = Field(default_factory=list)
    email: EmailMeta | None = Field(default=None, description="仅 EML 有值")
    ocr_confidence: float | None = Field(default=None, ge=0, le=1)
    raw_response: dict | None = Field(
        default=None,
        description="OCR 等外部服务的原始响应，用于审计与后续适配",
    )
