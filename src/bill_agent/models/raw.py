"""解析层输出：RawDocument。

对应 四.1 多格式文档解析。所有输入（EML / Excel / Word / PDF / 图片）都先变成 RawDocument，
下游只处理这一种结构。

状态（DocStatus）：
- EML / Excel / Word 由解析器直接产出文本和表格 -> PARSED
- PDF / 图片 只做登记：原文件存档到 file_ref，text 为空 -> PENDING_OCR
  OCR 服务读取 file_ref，识别后调用 complete_ocr() -> OCR_DONE；失败调用 fail_ocr() -> OCR_FAILED
- 只有 PARSED / OCR_DONE 的文档进入 LLM 抽取（ready_for_extraction）

状态与字段的一致性由 _check_state 强制校验，OCR 服务写回的结果也要经过它。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from .common import StrictModel
from .enums import DocStatus, FileType

OCR_FILE_TYPES = {FileType.PDF, FileType.IMAGE}


class EmailMeta(StrictModel):
    """四.1 EML：解析主题、发件人、收件人。"""

    subject: str | None = None
    sender: str | None = None
    recipients: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    sent_at: datetime | None = None
    message_id: str | None = None


class RawTable(StrictModel):
    """一张表格。四.1 Excel：工作表、表头及费用明细行；Word：表格；OCR：识别出的表格。"""

    name: str | None = Field(default=None, description="Excel sheet 名、'table-1'、'page-1-table-1' 等")
    header: list[str] | None = Field(default=None, description="识别出的表头行")
    rows: list[list[str | None]] = Field(default_factory=list, description="明细行，单元格保留原文")


class RawDocument(StrictModel):
    # --- 身份与来源（二.9 记录文件来源）
    source_id: str = Field(description="文件内容 SHA256，同一内容只处理一次")
    source_path: str = Field(description="来源路径；附件形如 'mail.eml#attachments/DN.pdf'")
    parent_id: str | None = Field(default=None, description="EML 附件指向所属邮件的 source_id")
    file_type: FileType
    file_name: str | None = None
    mime_type: str | None = Field(default=None, description="按文件头识别出的类型，如 application/pdf")
    file_size: int | None = Field(default=None, ge=0)

    # --- 处理状态
    status: DocStatus = DocStatus.PARSED
    file_ref: str | None = Field(
        default=None,
        description="原文件存档路径。PENDING_OCR 时必填，OCR 服务从这里读取文件",
    )

    # --- 内容
    text: str = Field(default="", description="全文文本（解析结果 / OCR 结果）")
    tables: list[RawTable] = Field(default_factory=list)
    email: EmailMeta | None = Field(default=None, description="仅 EML 有值")

    # --- OCR 结果信息（由 OCR 服务填写）
    ocr_engine: str | None = Field(default=None, description="OCR 引擎 / 视觉模型名称")
    ocr_confidence: float | None = Field(default=None, ge=0, le=1)
    ocr_completed_at: datetime | None = None
    ocr_error: str | None = None
    raw_response: dict | None = Field(default=None, description="OCR 服务原始响应，用于审计")

    warnings: list[str] = Field(default_factory=list, description="解析过程中的非致命问题")

    # ---------- 状态校验 ----------

    @model_validator(mode="after")
    def _check_state(self) -> "RawDocument":
        s, needs_ocr = self.status, self.file_type in OCR_FILE_TYPES
        if needs_ocr and s is DocStatus.PARSED:
            raise ValueError("PDF / 图片不能是 PARSED，应为 PENDING_OCR / OCR_DONE / OCR_FAILED")
        if not needs_ocr and s is not DocStatus.PARSED:
            raise ValueError(f"{self.file_type.value} 不需要 OCR，状态只能是 PARSED")
        if s is DocStatus.PENDING_OCR:
            if not self.file_ref:
                raise ValueError("PENDING_OCR 必须有 file_ref，否则 OCR 服务拿不到文件")
            if self.text or self.tables:
                raise ValueError("PENDING_OCR 时 text / tables 必须为空")
        if s is DocStatus.OCR_DONE:
            if not self.ocr_engine:
                raise ValueError("OCR_DONE 必须记录 ocr_engine")
            if not self.text.strip() and not self.tables:
                raise ValueError("OCR_DONE 时 text 和 tables 不能都为空")
        if s is DocStatus.OCR_FAILED and not self.ocr_error:
            raise ValueError("OCR_FAILED 必须记录 ocr_error")
        return self

    # ---------- 状态流转（OCR 服务使用） ----------

    @property
    def ready_for_extraction(self) -> bool:
        return self.status in (DocStatus.PARSED, DocStatus.OCR_DONE)

    def complete_ocr(
        self,
        *,
        text: str,
        engine: str,
        tables: list[RawTable] | None = None,
        confidence: float | None = None,
        raw_response: dict | None = None,
        warnings: list[str] | None = None,
    ) -> "RawDocument":
        """OCR 成功：返回新的 OCR_DONE 文档（原对象不变，结果经过完整校验）。"""
        if self.status not in (DocStatus.PENDING_OCR, DocStatus.OCR_FAILED):
            raise ValueError(f"当前状态 {self.status.value} 不能写入 OCR 结果")
        data = self.model_dump()
        data.update(
            status=DocStatus.OCR_DONE,
            text=text,
            tables=[t.model_dump() if isinstance(t, RawTable) else t for t in (tables or [])],
            ocr_engine=engine,
            ocr_confidence=confidence,
            ocr_completed_at=datetime.now(),
            ocr_error=None,
            raw_response=raw_response,
            warnings=[*self.warnings, *(warnings or [])],
        )
        return RawDocument.model_validate(data)

    def fail_ocr(self, error: str, *, engine: str | None = None, raw_response: dict | None = None) -> "RawDocument":
        """OCR 失败：返回 OCR_FAILED 文档，保留 file_ref 以便重试。"""
        if self.status not in (DocStatus.PENDING_OCR, DocStatus.OCR_FAILED):
            raise ValueError(f"当前状态 {self.status.value} 不能标记 OCR 失败")
        data = self.model_dump()
        data.update(status=DocStatus.OCR_FAILED, ocr_error=error, ocr_engine=engine,
                    raw_response=raw_response, text="", tables=[])
        return RawDocument.model_validate(data)
