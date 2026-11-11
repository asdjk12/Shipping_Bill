"""AR/AP 判断（6.8）：收款方 / 付款方的来源顺序、主体别名匹配、样例期望结果。"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from bill_agent.models import ChargeRecord, DocumentType, ExtractedDocument, PaymentTerm, PostingType
from bill_agent.normalize import Normalizer
from bill_agent.rules import EntityMatch, EntityMatcher, apply_posting, decide_posting_type

SAMPLES = Path(__file__).parents[1] / "fixtures" / "samples"
PAT = EntityMatcher.load("PAT")


def rec(**kw) -> ChargeRecord:
    return ChargeRecord(original_charge_item=kw.pop("original_charge_item", "Handling Charge"),
                        amount=kw.pop("amount", Decimal("40")), **kw)


# ---------- 主体别名 ----------

@pytest.mark.parametrize("name, expected", [
    ("PAT", EntityMatch.YES),
    ("pat", EntityMatch.YES),
    ("PILOT AISHIP TECH CO LTD", EntityMatch.YES),          # 忽略大小写和标点
    ("湃乐多（上海）航运科技有限公司", EntityMatch.YES),
    ("Pilot Aiship Technology Co., Ltd.", EntityMatch.UNSURE),  # 很像但不相同
    ("PAT LOGISTICS LLC", EntityMatch.UNSURE),              # 包含别名
    ("SHA", EntityMatch.NO),                                 # 已确认不是 PAT
    ("PATRICK SHIPPING", EntityMatch.NO),                    # 只是前缀相同，不是完整的词
    ("NEPTUNE SHIPPING LIMITED", EntityMatch.NO),
    (None, EntityMatch.NO),
])
def test_entity_match(name, expected):
    assert PAT.match(name)[0] == expected


def test_entity_load_by_alias_and_unknown():
    assert EntityMatcher.load("Pilot Aiship Tech co., Ltd.").name == "PAT"
    with pytest.raises(ValueError):
        EntityMatcher.load("ACME")


# ---------- 收款方 ----------

def test_payee_stated_wins_over_doc_type():
    d = decide_posting_type(rec(document_type=DocumentType.DEBIT_NOTE, issuer="PAT", payee="NEPTUNE",
                                recipient="X"), PAT)
    assert d.payee == "NEPTUNE"


def test_payee_by_doc_type():
    assert decide_posting_type(rec(document_type=DocumentType.INVOICE, issuer="A", recipient="B"), PAT).payee == "A"
    assert decide_posting_type(rec(document_type=DocumentType.CREDIT_NOTE, issuer="A", recipient="B"),
                               PAT).payee == "B"


# ---------- 付款方 ----------

def test_payer_stated_wins():
    d = decide_posting_type(rec(payee="PAT", payer="SHA", payment_term=PaymentTerm.CC, consignee="C"), PAT)
    assert d.payer == "SHA"
    assert d.posting_type == PostingType.AR


def test_payer_collect_takes_consignee():
    d = decide_posting_type(rec(document_type=DocumentType.DEBIT_NOTE, issuer="NEPTUNE", payee="NEPTUNE",
                                recipient="PAT", payment_term=PaymentTerm.CC, shipper="S", consignee="C"), PAT)
    assert d.payer == "C"                         # 付款方式优先于单据类型兜底
    assert d.posting_type is None


def test_payer_prepaid_takes_shipper():
    d = decide_posting_type(rec(payee="NEPTUNE", payment_term=PaymentTerm.PP, shipper="PAT", consignee="C"), PAT)
    assert d.payer == "PAT"
    assert d.posting_type == PostingType.AP


def test_issuer_never_used_as_payer():
    """PAT 只是印在抬头（签发方），只写明了收款方：不能推成 PAT 付款（原伪代码的问题）。"""
    d = decide_posting_type(rec(document_type=DocumentType.DEBIT_NOTE, issuer="PAT", payee="NEPTUNE"), PAT)
    assert d.payer is None
    assert d.posting_type is None
    assert d.review_reason is None


def test_payer_fallback_when_party_missing():
    """到付但没有 consignee：按单据类型兜底取接收方。"""
    d = decide_posting_type(rec(document_type=DocumentType.DEBIT_NOTE, issuer="Destination Agent",
                                recipient="PAT", payment_term=PaymentTerm.CC), PAT)
    assert d.payer == "PAT"
    assert d.posting_type == PostingType.AP


def test_fallback_payer_same_as_payee_is_unknown():
    d = decide_posting_type(rec(document_type=DocumentType.DEBIT_NOTE, issuer="X", recipient="PAT",
                                payee="PAT"), PAT)
    assert d.payer is None
    assert d.posting_type == PostingType.AR


def test_task_sample_is_ap():
    """题目样例：Destination Agent 向 Origin Agent 签发 DN，主体 Origin Agent -> AP。"""
    origin = EntityMatcher("Origin Agent", [])
    d = decide_posting_type(rec(document_type=DocumentType.DEBIT_NOTE, issuer="Destination Agent",
                                recipient="Origin Agent", payment_term=PaymentTerm.PP), origin)
    assert d.posting_type == PostingType.AP


# ---------- 方向取反、转人工 ----------

def test_negative_amount_reverses():
    d = decide_posting_type(rec(document_type=DocumentType.INVOICE, issuer="PAT", recipient="B",
                                amount=Decimal("-10")), PAT)
    assert (d.payee, d.payer, d.posting_type) == ("B", "PAT", PostingType.AP)


def test_refund_hint_reverses():
    d = decide_posting_type(rec(document_type=DocumentType.INVOICE, issuer="PAT", recipient="B",
                                original_charge_item="Freight Refund"), PAT)
    assert d.posting_type == PostingType.AP


def test_unsure_entity_goes_to_review():
    [r] = apply_posting([rec(payee="PAT LOGISTICS LLC", payer="B", line_no=1)], PAT)
    assert r.posting_type is None
    assert any("posting_type" in x for x in r.review_reasons)


def test_not_a_party_is_null_without_review():
    [r] = apply_posting([rec(payee="TAR", payer="SHA", line_no=1)], PAT)
    assert (r.accounting_entity, r.posting_type, r.review_reasons) == ("PAT", None, [])
    assert r.posting_reasoning


def test_reasoning_not_business_field():
    [r] = apply_posting([rec(payee="PAT", payer="B")], PAT)
    assert "posting_reasoning" not in r.business_fields()
    assert r.business_fields()["posting_type"] == PostingType.AR


# ---------- 样例 ----------

@pytest.mark.parametrize("name, payee, payer", [
    ("DN_DC-108478.expected_extraction.json", "NEPTUNE SHIPPING LIMITED", "AEBO TCO IMPORT LLC"),
    ("INV_ZIM_ORFR1618731.expected_extraction.json",
     "Zim American Integrated Shipping Services Co., LLC", "USIAHALLAM"),
    ("NOTICE_CMA_CMDUCHN4053601.expected_extraction.json", "CMA CGM (AMERICA) LLC", "NEPTUNE SHIPPING LIMITED"),
])
def test_golden_samples(name, payee, payer):
    doc = ExtractedDocument.model_validate(json.loads((SAMPLES / name).read_text(encoding="utf-8")))
    for r in Normalizer().normalize_document(doc):
        d = decide_posting_type(r, PAT)
        assert (d.payee, d.payer, d.posting_type, d.review_reason) == (payee, payer, None, None)
