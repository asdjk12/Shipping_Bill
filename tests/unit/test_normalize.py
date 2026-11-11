"""标准化（6.4）：费用名、币种、付款方式、单据类型、日期，以及展开为 ChargeRecord。"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from bill_agent.extract.extractor import payment_term_problem
from bill_agent.models import DocumentType, ExtractedDocument, PaymentTerm
from bill_agent.normalize import (
    ChargeNormalizer,
    CurrencyNormalizer,
    Normalizer,
    normalize_date,
    normalize_document_type,
    normalize_payment_term,
)

SAMPLES = Path(__file__).parents[1] / "fixtures" / "samples"


def golden(name: str) -> ExtractedDocument:
    return ExtractedDocument.model_validate(json.loads((SAMPLES / name).read_text(encoding="utf-8")))


# ---------- 费用名称 ----------

@pytest.mark.parametrize("raw, expected", [
    ("HANDLING CHARGE", "Handling Charge"),          # 标准名本身，统一大小写后命中
    ("Handling Fee", "Handling Charge"),
    ("h/d  fee", "Handling Charge"),                 # 标点、空格
    ("H-D Fee", "Handling Charge"),
    ("DOC FEE", "Documentation Fee"),
    ("thc", "Terminal Handling Charge"),
    ("Customs Fee", "Customs Clearance Fee"),
    ("H/C", "H/C"),                                  # 不在表中：原样照抄
    ("OCA17 On Carriage Additional - US Em", "OCA17 On Carriage Additional - US Em"),
    ("THC - Terminal Handling", "THC - Terminal Handling"),   # 不做模糊匹配
    ("Handling Fees", "Handling Fees"),
])
def test_charge_name(raw, expected):
    assert ChargeNormalizer()(raw) == expected


def test_charge_mapping_rejects_conflicts(tmp_path):
    (tmp_path / "charge_mapping.yaml").write_text("A Charge: [X Fee]\nB Charge: [x-fee]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="多个标准名称"):
        ChargeNormalizer(tmp_path)


# ---------- 币种 ----------

@pytest.mark.parametrize("raw, expected", [
    ("USD", "USD"), ("usd", "USD"), ("US$", "USD"), ("USD$", "USD"), ("U.S.$", "USD"),
    ("$", "USD"), ("＄", "USD"), ("RMB", "CNY"), ("¥", "CNY"), ("CNY", "CNY"), ("EUR", "EUR"), ("HK$", "HKD"),
])
def test_currency(raw, expected):
    assert CurrencyNormalizer()(raw) == (expected, None)


@pytest.mark.parametrize("raw", ["XYZ", "dollars"])
def test_currency_unknown(raw):
    code, problem = CurrencyNormalizer()(raw)
    assert code is None and raw in problem


def test_currency_empty():
    assert CurrencyNormalizer()(None) == (None, None)


# ---------- 付款方式 ----------

@pytest.mark.parametrize("raw, expected", [
    ("P", PaymentTerm.PP), ("pp", PaymentTerm.PP), ("Prepaid", PaymentTerm.PP),
    ("FREIGHT PREPAID", PaymentTerm.PP), ("预付", PaymentTerm.PP), ("寄付", PaymentTerm.PP),
    ("C", PaymentTerm.CC), ("CC", PaymentTerm.CC), ("Collect", PaymentTerm.CC), ("到付", PaymentTerm.CC),
    ("TT", None), (None, None),
])
def test_payment_term(raw, expected):
    assert normalize_payment_term(raw) is expected


def test_payment_term_table_shared_with_extraction():
    """抽取核验接受的写法，标准化都能转换。"""
    for raw in ["P", "C", "PP", "CC", "Prepaid", "Collect", "Freight Collect", "到付"]:
        assert payment_term_problem(raw, "") is None
        assert normalize_payment_term(raw) is not None


# ---------- 单据类型 ----------

@pytest.mark.parametrize("raw, expected", [
    ("DEBIT NOTE", DocumentType.DEBIT_NOTE),
    ("DEBIT INVOICE", DocumentType.INVOICE),
    ("Credit Note", DocumentType.CREDIT_NOTE),
    ("CREDIT INVOICE", DocumentType.CREDIT_NOTE),
    ("INVOICE", DocumentType.INVOICE),
    ("GENERAL NOTICE", DocumentType.OTHER),
    ("商业发票", DocumentType.INVOICE),
    (None, None),
])
def test_document_type(raw, expected):
    assert normalize_document_type(raw) is expected


# ---------- 日期 ----------

@pytest.mark.parametrize("raw, expected", [
    ("10-24-2026", date(2026, 10, 24)),             # DN：年在后，默认月/日
    ("10/19/2026 05:58 PM", date(2026, 10, 19)),    # CMA：去掉时间
    ("24/10/2026", date(2026, 10, 24)),             # 第一段大于 12，日/月
    ("2026-10-30", date(2026, 10, 30)),
    ("2026年10月30日", date(2026, 10, 30)),
    ("24 OCT 2026", date(2026, 10, 24)),
    ("Oct 24, 2026", date(2026, 10, 24)),
])
def test_date(raw, expected):
    assert normalize_date(raw) == (expected, None)


def test_date_year_last_defaults_to_month_day():
    """年在后默认月/日/年（ZIM 的 10/03/2025 -> 2025-10-03），不转人工。"""
    assert normalize_date("10/03/2025") == (date(2025, 10, 3), None)
    assert normalize_date("03.10.2025") == (date(2025, 3, 10), None)


@pytest.mark.parametrize("raw", ["13/13/2026", "02/30/2026", "10/03/25", "N/A"])
def test_date_invalid(raw):
    value, problem = normalize_date(raw)
    assert value is None and problem


# ---------- 展开为 ChargeRecord ----------

def test_dn_golden():
    records = Normalizer().normalize_document(golden("DN_DC-108478.expected_extraction.json"))
    assert [r.charge_item for r in records] == [
        "Handling Charge", "4 ADD'L DETENTION FREE TIME CHARGE", "Handling Charge"]
    assert [r.original_charge_item for r in records] == [
        "HANDLING CHARGE", "4 ADD'L DETENTION FREE TIME CHARGE", "HANDLING CHARGE"]
    r = records[1]
    assert r.amount == Decimal("1310.40")
    assert (r.currency, r.currency_raw) == ("USD", "USD")         # 行上没有币种，继承单据级
    assert (r.payment_term, r.payment_term_raw) == (PaymentTerm.CC, "C")
    assert r.document_type is DocumentType.DEBIT_NOTE
    assert r.document_date == date(2026, 10, 24)
    assert r.carrier == "NEPTUNE SHIPPING LIMITED"
    assert all(not r.review_reasons for r in records)


def test_zim_golden():
    records = Normalizer().normalize_document(golden("INV_ZIM_ORFR1618731.expected_extraction.json"))
    assert [r.document_type for r in records] == [DocumentType.INVOICE] * 2
    assert all(r.document_date == date(2025, 10, 3) and r.document_date_raw == "10/03/2025" for r in records)
    assert all(not r.review_reasons for r in records)
    assert all(r.payment_term is None for r in records)


def test_cma_golden():
    (r,) = Normalizer().normalize_document(golden("NOTICE_CMA_CMDUCHN4053601.expected_extraction.json"))
    assert r.charge_item == r.original_charge_item == "OCA17 On Carriage Additional - US Em"
    assert r.document_type is DocumentType.OTHER
    assert r.document_date == date(2026, 10, 19)
    assert (r.currency, r.payment_term) == ("USD", PaymentTerm.CC)
    assert not r.review_reasons


def test_email_dollar_sign_defaults_to_usd():
    """邮件的 `SHA pay TAR：H/C $50`：单独的 $ 默认为美元（已确认），原文保留 $。"""
    doc = ExtractedDocument(source_id="eml", charges=[
        {"line_no": 1, "charge_name_raw": "H/C", "amount": "50", "currency_raw": "$"}])
    (r,) = Normalizer().normalize_document(doc)
    assert (r.charge_item, r.currency, r.currency_raw) == ("H/C", "USD", "$")
    assert not r.review_reasons


def test_missing_currency_needs_review():
    doc = ExtractedDocument(source_id="x", charges=[{"line_no": 1, "charge_name_raw": "THC", "amount": "10"}])
    (r,) = Normalizer().normalize_document(doc)
    assert r.charge_item == "Terminal Handling Charge"
    assert r.currency is None and "没有币种" in r.review_reasons[0]


def test_record_keeps_source_and_evidence():
    doc = ExtractedDocument(
        source_id="pdf1", currency_raw="USD",
        evidence={"reference_no": {"source_id": "pdf1", "quote": "DN NO. 1"}},
        charges=[{"line_no": 1, "charge_name_raw": "Doc Fee", "amount": "5",
                  "evidence": {"amount": {"source_id": "body", "quote": "Doc Fee 5"}}}])
    (r,) = Normalizer().normalize_document(doc)
    assert r.source_ids == ["pdf1", "body"]
    assert set(r.evidence) == {"reference_no", "amount"}


def test_normalize_result_carries_gate_reasons():
    doc = golden("NOTICE_CMA_CMDUCHN4053601.expected_extraction.json")
    needs_review = SimpleNamespace(status=SimpleNamespace(value="needs_review"), documents=[doc],
                                   reasons=["证据不足被清空，需人工确认: payee"])
    passed = SimpleNamespace(status=SimpleNamespace(value="passed"), documents=[doc], reasons=["x"])
    (r,) = Normalizer().normalize_result(needs_review)
    assert r.review_reasons == ["证据不足被清空，需人工确认: payee"]
    assert Normalizer().normalize_result(passed)[0].review_reasons == []


def test_raw_spelling_is_not_a_business_change():
    doc = golden("DN_DC-108478.expected_extraction.json")
    a = Normalizer().normalize_document(doc)[0]
    b = a.model_copy(update={"document_date_raw": "2026-10-24", "currency_raw": "US$"})
    assert a.business_fields() == b.business_fields()
