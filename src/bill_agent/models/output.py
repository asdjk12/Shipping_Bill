"""标准输出格式（五、标准输出格式；六、主要字段说明；七、处理要求）。

核心字段严格按 test.pdf 样例；带 [扩展] 标注的字段是为满足其他条款而加的，
可通过 to_json(strict=True) 去掉，只输出样例中出现的字段。
"""

from __future__ import annotations

import json
from datetime import date

from pydantic import Field

from .common import Amount, StrictModel
from .enums import DCDirection, DocumentType, MatchRule, Operation, PaymentTerm, PostingType
from .record import CURRENCY_PATTERN, ChargeRecord


class DocumentInfo(StrictModel):
    document_type: DocumentType | None = None
    reference_no: str | None = None
    document_date: date | None = None
    # [扩展] 四.5 匹配键
    master_bl_no: str | None = None
    house_bl_no: str | None = None
    booking_no: str | None = None
    container_nos: list[str] = Field(default_factory=list)


class TransactionParty(StrictModel):
    issuer: str | None = None
    recipient: str | None = None
    # [扩展] 四.6 收费方向证据
    payee: str | None = None
    payer: str | None = None


class ChargeDetails(StrictModel):
    charge_item: str | None = None
    amount: Amount | None = None
    currency: str | None = Field(default=None, pattern=CURRENCY_PATTERN)
    payment_term: PaymentTerm | None = None
    # [扩展] 四.3 同时保存原始费用名称；四.2 数量、单价；样例中的借贷方向与汇率
    original_charge_item: str | None = None
    charge_code: str | None = None
    line_no: int | None = None
    basis: str | None = None
    quantity: Amount | None = None
    unit_price: Amount | None = None
    dc_direction: DCDirection | None = None
    exchange_rate: Amount | None = None
    local_amount: Amount | None = None
    local_currency: str | None = Field(default=None, pattern=CURRENCY_PATTERN)


class FinancialAttributes(StrictModel):
    accounting_entity: str | None = None
    posting_type: PostingType | None = None


class FieldChange(StrictModel):
    field: str
    old_value: str | float | None
    new_value: str | float | None
    evidence: str | None = None


class AuditTrail(StrictModel):
    ai_reasoning: list[str] = Field(default_factory=list)
    # [扩展] 二.8、二.9
    source_files: list[str] = Field(default_factory=list)
    source_text: list[str] = Field(default_factory=list, description="支撑本条记录的原文片段")
    matched_rule: MatchRule | None = None
    matched_record_id: str | None = None
    changed_fields: list[FieldChange] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    review_reasons: list[str] = Field(default_factory=list)


class OutputRecord(StrictModel):
    operation: Operation
    document_info: DocumentInfo
    transaction_party: TransactionParty
    charge_details: ChargeDetails
    financial_attributes: FinancialAttributes
    audit_trail: AuditTrail

    @classmethod
    def from_charge_record(cls, rec: ChargeRecord, operation: Operation, audit: AuditTrail) -> "OutputRecord":
        return cls(
            operation=operation,
            document_info=DocumentInfo(
                document_type=rec.document_type,
                reference_no=rec.reference_no,
                document_date=rec.document_date,
                master_bl_no=rec.master_bl_no,
                house_bl_no=rec.house_bl_no,
                booking_no=rec.booking_no,
                container_nos=rec.container_nos,
            ),
            transaction_party=TransactionParty(
                issuer=rec.issuer, recipient=rec.recipient, payee=rec.payee, payer=rec.payer
            ),
            charge_details=ChargeDetails(
                charge_item=rec.charge_item,
                amount=rec.amount,
                currency=rec.currency,
                payment_term=rec.payment_term,
                original_charge_item=rec.original_charge_item,
                charge_code=rec.charge_code,
                line_no=rec.line_no,
                basis=rec.basis,
                quantity=rec.quantity,
                unit_price=rec.unit_price,
                dc_direction=rec.dc_direction,
                exchange_rate=rec.exchange_rate,
                local_amount=rec.local_amount,
                local_currency=rec.local_currency,
            ),
            financial_attributes=FinancialAttributes(
                accounting_entity=rec.accounting_entity,
                posting_type=rec.posting_type,
            ),
            audit_trail=audit,
        )


# strict 模式下只保留 test.pdf 样例中出现的字段
_STRICT_INCLUDE = {
    "operation": True,
    "document_info": {"document_type", "reference_no", "document_date"},
    "transaction_party": {"issuer", "recipient"},
    "charge_details": {"charge_item", "amount", "currency", "payment_term"},
    "financial_attributes": {"accounting_entity", "posting_type"},
    "audit_trail": {"ai_reasoning"},
}


def to_json(records: list[OutputRecord], *, strict: bool = False, indent: int = 2) -> str:
    """七.10 最终结果必须是合法 JSON；七.2 无法提取的字段使用 null。"""
    include = _STRICT_INCLUDE if strict else None
    payload = [r.model_dump(mode="json", include=include) for r in records]
    return json.dumps(payload, ensure_ascii=False, indent=indent)
