import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from bill_agent.models import (
    AuditTrail,
    ChargeRecord,
    DocumentType,
    Evidence,
    ExtractedCharge,
    ExtractedDocument,
    FileType,
    Operation,
    OutputRecord,
    PaymentTerm,
    PostingType,
    RawDocument,
    parse_amount,
    to_json,
)

# test.pdf 五、标准输出格式 中的样例（去掉了样例里多余的尾逗号）
SPEC_SAMPLE = [
    {
        "operation": "UPDATE",
        "document_info": {"document_type": "Debit Note", "reference_no": "DN-202609-001", "document_date": "2026-09-20"},
        "transaction_party": {"issuer": "Destination Agent", "recipient": "Origin Agent"},
        "charge_details": {"charge_item": "Handling Charge", "amount": 40.00, "currency": "USD", "payment_term": "PP"},
        "financial_attributes": {"accounting_entity": "Origin Agent", "posting_type": "AP"},
        "audit_trail": {
            "ai_reasoning": [
                "通过单据编号和费用名称匹配到已有账单",
                "根据 OCR 识别结果将金额更新为 USD 40.00",
                "其他字段未被明确要求修改，因此保留原值",
            ]
        },
    }
]


# ---------- 金额解析 ----------

@pytest.mark.parametrize(
    "raw, expected",
    [
        (40, Decimal("40")),
        (40.1, Decimal("40.1")),
        ("1,234.50", Decimal("1234.50")),
        (" 40.00 ", Decimal("40.00")),
        ("(15.00)", Decimal("-15.00")),
        (None, None),
        ("", None),
    ],
)
def test_parse_amount_ok(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw", ["4O.00", "USD 40", "abc", "nan", "inf", True])
def test_parse_amount_rejects(raw):
    with pytest.raises(ValueError):
        parse_amount(raw)


# ---------- 输出格式 ----------

def test_spec_sample_roundtrip_strict():
    records = [OutputRecord.model_validate(r) for r in SPEC_SAMPLE]
    assert json.loads(to_json(records, strict=True)) == SPEC_SAMPLE


def test_amount_serialized_as_number():
    rec = OutputRecord.model_validate(SPEC_SAMPLE[0])
    out = json.loads(to_json([rec]))
    assert isinstance(out[0]["charge_details"]["amount"], float)


def test_missing_fields_are_null():
    rec = ChargeRecord(original_charge_item="Doc Fee")
    out = OutputRecord.from_charge_record(rec, Operation.PENDING_REVIEW, AuditTrail())
    data = json.loads(to_json([out], strict=True))[0]
    assert data["charge_details"]["amount"] is None
    assert data["document_info"]["reference_no"] is None


def test_currency_must_be_three_letters():
    with pytest.raises(ValidationError):
        ChargeRecord(original_charge_item="THC", currency="US$")


def test_extra_keys_forbidden():
    bad = json.loads(json.dumps(SPEC_SAMPLE[0]))
    bad["charge_details"]["foo"] = 1
    with pytest.raises(ValidationError):
        OutputRecord.model_validate(bad)


def test_from_charge_record_keeps_original_name():
    rec = ChargeRecord(
        document_type=DocumentType.DEBIT_NOTE,
        reference_no="DN-202609-001",
        charge_item="Handling Charge",
        original_charge_item="H/D Fee",
        amount="40.00",
        currency="USD",
        payment_term=PaymentTerm.PP,
        accounting_entity="Origin Agent",
        posting_type=PostingType.AP,
    )
    out = OutputRecord.from_charge_record(rec, Operation.CREATE, AuditTrail())
    full = json.loads(to_json([out]))[0]
    assert full["charge_details"]["original_charge_item"] == "H/D Fee"
    strict = json.loads(to_json([out], strict=True))[0]
    assert "original_charge_item" not in strict["charge_details"]


# ---------- 抽取 schema ----------

def test_extracted_document_evidence_keys_validated():
    ev = Evidence(source_id="s1", quote="DN-202609-001")
    ExtractedDocument(source_id="s1", reference_no="DN-202609-001", evidence={"reference_no": ev})
    with pytest.raises(ValidationError):
        ExtractedDocument(source_id="s1", evidence={"made_up_field": ev})
    with pytest.raises(ValidationError):
        ExtractedDocument(
            source_id="s1",
            charges=[ExtractedCharge(line_no=1, charge_name_raw="THC", evidence={"nonsense": ev})],
        )


def test_extracted_charge_amount_from_string():
    c = ExtractedCharge(line_no=1, charge_name_raw="THC", amount="1,200.00", currency_raw="US$")
    assert c.amount == Decimal("1200.00")


def test_raw_document_minimal():
    d = RawDocument(source_id="abc", source_path="charges.xlsx", file_type=FileType.EXCEL)
    assert d.tables == [] and d.email is None and d.ready_for_extraction
