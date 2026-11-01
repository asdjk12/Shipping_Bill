"""用网盘样例校验数据模型能完整承载真实单据。

fixtures/samples/*.expected_extraction.json 是人工标注的"理想抽取结果"，
后续也作为端到端测试的 golden 答案。
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from bill_agent.models import DCDirection, ExtractedDocument

SAMPLES = Path(__file__).parents[1] / "fixtures" / "samples"


def load(name: str) -> ExtractedDocument:
    return ExtractedDocument.model_validate_json((SAMPLES / name).read_text(encoding="utf-8"))


@pytest.fixture
def dn() -> ExtractedDocument:
    return load("DN_DC-108478.expected_extraction.json")


@pytest.fixture
def zim() -> ExtractedDocument:
    return load("INV_ZIM_ORFR1618731.expected_extraction.json")


def test_all_fixtures_validate():
    files = sorted(SAMPLES.glob("*.expected_extraction.json"))
    assert len(files) >= 2
    for f in files:
        ExtractedDocument.model_validate_json(f.read_text(encoding="utf-8"))


# ---------- DN DC-108478 ----------

def test_dn_same_name_charges_distinguished_by_line_no(dn):
    handling = [c for c in dn.charges if c.charge_name_raw == "HANDLING CHARGE"]
    assert [c.line_no for c in handling] == [1, 3]
    assert [c.amount for c in handling] == [Decimal("65.00"), Decimal("35.00")]


def test_dn_lines_sum_to_total(dn):
    total = dn.totals[0]
    assert sum(c.amount for c in dn.charges) == total.total_amount == total.balance_due
    assert total.total_credit == 0


def test_dn_recipient_empty_but_payee_explicit(dn):
    assert dn.recipient is None
    assert dn.payee == "NEPTUNE SHIPPING LIMITED (LAX)"


def test_dn_two_bls_two_containers(dn):
    assert (dn.master_bl_no, dn.house_bl_no) == ("MAEU275752922", "CGT260801188")
    assert dn.container_nos == ["MRSU2571577", "TCLU8619685"]


def test_dn_currency_only_at_document_level(dn):
    assert dn.currency_raw == "USD"
    assert all(c.currency_raw is None for c in dn.charges)


def test_dn_cod_is_credit_term_not_payment_term(dn):
    assert dn.credit_terms_raw == "COD"
    assert {c.payment_term_raw for c in dn.charges} == {"C"}
    assert all(c.dc_direction is DCDirection.DEBIT for c in dn.charges)


# ---------- ZIM Invoice ORFR1618731 ----------

def test_zim_local_amount_equals_amount_times_roe(zim):
    for c in zim.charges:
        assert c.amount * c.exchange_rate == c.local_amount


def test_zim_lines_sum_to_total(zim):
    assert sum(c.amount for c in zim.charges) == zim.totals[0].total_amount


def test_zim_no_payment_term_on_document(zim):
    # 单据上没有 PP/CC，不能推测
    assert all(c.payment_term_raw is None for c in zim.charges)
    assert zim.place_of_payment == "USORF"


def test_zim_date_kept_raw(zim):
    # 10/03/2025 有 MM/DD 与 DD/MM 歧义，抽取层只存原文
    assert zim.document_date_raw == "10/03/2025"


# ---------- schema 约束 ----------

def test_duplicate_line_no_rejected(dn):
    data = dn.model_dump(mode="json")
    data["charges"][2]["line_no"] = 1
    with pytest.raises(ValidationError):
        ExtractedDocument.model_validate(data)


def test_zim_basis_column_kept_raw(zim):
    # Base 列原样保存；币种（USD）在单独的 Curr 列
    assert [c.basis_raw for c in zim.charges] == ["C", "PTT"]
    assert {c.currency_raw for c in zim.charges} == {"USD"}


# ---------- CMA CGM General Notice（PDF示例账单1） ----------

@pytest.fixture
def notice() -> ExtractedDocument:
    return load("NOTICE_CMA_CMDUCHN4053601.expected_extraction.json")


def test_notice_has_no_reference_no_but_has_bl(notice):
    # 没有单据编号，匹配规则 1 不可用，只能走规则 2（提单号 + 费用名 + 币种）
    assert notice.reference_no is None
    assert notice.master_bl_no == "CMDUCHN4053601"


def test_notice_only_billed_charge_extracted(notice):
    # 3 页重复页眉不能导致重复抽取；条款里的 $25 / $150 / $1100 是费率说明，不是本单费用
    assert len(notice.charges) == 1
    c = notice.charges[0]
    assert (c.charge_code_raw, c.amount, c.payment_term_raw) == ("OCA17", Decimal("245.0000"), "C")
    assert not {Decimal("25"), Decimal("150"), Decimal("1100")} & {x.amount for x in notice.charges}


def test_notice_payee_from_remit_to(notice):
    # 单据类型不是 DN/Invoice，AR/AP 方向只能依靠 payee（REMIT TO）
    assert notice.payee == notice.issuer == "CMA CGM (AMERICA) LLC"
    assert notice.recipient == "NEPTUNE SHIPPING LIMITED"


def test_notice_total_matches(notice):
    assert sum(c.amount for c in notice.charges) == notice.totals[0].total_amount
