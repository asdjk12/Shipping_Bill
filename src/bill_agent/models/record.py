"""标准化后的费用行：ChargeRecord。

一个 ExtractedDocument 中有 N 个费用 -> N 个 ChargeRecord（四.4）。
它是匹配、AR/AP、操作判定的输入，也是账单库中存储的单位。
单据级字段（单据号、双方、提单号等）被展开到每一行，方便按行匹配。

注意：样例 DN 中同一单据有两行 "HANDLING CHARGE"（65.00 / 35.00），
所以 "单据编号 + 费用名称"（匹配规则 1）并不唯一，需配合 line_no / 金额消歧。
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from pydantic import Field

from .common import Amount, Evidence, StrictModel
from .enums import DCDirection, DocumentType, PaymentTerm, PostingType

CURRENCY_PATTERN = r"^[A-Z]{3}$"  # 七.4 币种统一使用三位代码；ISO 4217 白名单在 validate 模块


class ChargeRecord(StrictModel):
    # --- 单据信息（四.2）
    document_type: DocumentType | None = None
    document_type_raw: str | None = None
    reference_no: str | None = None
    document_date: date | None = None

    # --- 交易双方与收费方向（四.2、四.6）
    issuer: str | None = None
    recipient: str | None = None
    payee: str | None = None
    payer: str | None = None

    # --- 匹配键（四.5）
    master_bl_no: str | None = None
    house_bl_no: str | None = None
    booking_no: str | None = None
    container_nos: list[str] = Field(default_factory=list)

    # --- 费用明细（四.2、四.3）
    line_no: int | None = Field(default=None, ge=1, description="在原单据中的行序号")
    charge_item: str | None = Field(
        default=None,
        description="费用名称：命中四.3 标准化表时为标准名，否则原样照抄原始名称（不做改写）",
    )
    original_charge_item: str = Field(min_length=1, description="原始费用名称")
    basis: str | None = None
    quantity: Amount | None = None
    unit_price: Amount | None = None
    amount: Amount | None = None
    currency: str | None = Field(default=None, pattern=CURRENCY_PATTERN)
    dc_direction: DCDirection | None = None
    payment_term: PaymentTerm | None = None
    exchange_rate: Amount | None = None
    local_amount: Amount | None = None
    local_currency: str | None = Field(default=None, pattern=CURRENCY_PATTERN)

    # --- 财务属性（四.6）
    accounting_entity: str | None = None
    posting_type: PostingType | None = None

    # --- 溯源（二.9）
    source_ids: list[str] = Field(default_factory=list)
    evidence: dict[str, Evidence] = Field(default_factory=dict)

    def bl_nos(self) -> list[str]:
        """匹配规则 2 的提单号：Master 和 House 都参与匹配。"""
        return [b for b in (self.master_bl_no, self.house_bl_no) if b]

    def business_fields(self) -> dict:
        """参与 UPDATE diff 的业务字段（不含溯源信息）。"""
        return self.model_dump(exclude={"source_ids", "evidence"})


class BillRecord(ChargeRecord):
    """账单库中的一条记录 = ChargeRecord + 存储元数据。"""

    record_id: str = Field(default_factory=lambda: uuid4().hex)
    version: int = Field(default=1, ge=1)
    updated_at: datetime = Field(default_factory=datetime.now)

    def business_fields(self) -> dict:
        return self.model_dump(exclude={"source_ids", "evidence", "record_id", "version", "updated_at"})
