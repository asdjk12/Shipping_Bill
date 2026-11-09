"""数据校验（validate/）：规则本身，以及在 ReviewGate 中的退回和转人工。"""

import pytest

from bill_agent.models import ExtractedDocument
from bill_agent.validate import Severity, Validator, container_check_digit, container_problem

from .test_review import SAMPLES


def make(charges=None, **kw) -> ExtractedDocument:
    charges = [{"amount": "10"}] if charges is None else charges
    rows = [{"line_no": i, "charge_name_raw": c.pop("name", "X"), **c} for i, c in enumerate(charges, start=1)]
    kw.setdefault("reference_no", "R-1")
    return ExtractedDocument(source_id="d", charges=rows, **kw)


def codes(*docs) -> list[str]:
    return [i.code for i in Validator().validate(list(docs)).issues]


# ---------- 规则 ----------

def test_golden_samples_have_no_issues():
    for f in SAMPLES.glob("*.expected_extraction.json"):
        d = ExtractedDocument.model_validate_json(f.read_text(encoding="utf-8"))
        assert Validator().validate([d]).issues == [], f.name


@pytest.mark.parametrize("value", ["MRSU2571577", "TCLU8619685", "JXLU4504463", "SAPU2040689", "MRSU 257157-7"])
def test_container_ok(value):
    assert container_problem(value) is None


def test_container_check_digit_and_ocr_confusion():
    assert container_check_digit("CSQU305438") == 3          # ISO 6346 标准示例
    assert "校验位应为 7" in container_problem("MRSU2571578")
    assert "可能是 MRSU2571577" in container_problem("MRSU257I577")    # 数字位上的 I
    assert "可能是 TCLU8619685" in container_problem("TCLU86I9685")
    assert "可能是 SAPU2040689" in container_problem("5APU2040689")    # 字母位上的 5
    assert "不符合 ISO 6346 格式" in container_problem("ABC123")


def test_container_error_is_reported_per_value():
    r = Validator().validate([make(container_nos=["MRSU2571577", "MRSU2571578"])])
    [issue] = r.errors
    assert issue.code == "V1" and issue.doc == 1 and "MRSU2571578" in issue.message


def test_missing_charges_amounts_and_keys():
    assert codes(make(charges=[], totals=[{"total_amount": "10"}])) == ["V2"]
    assert "漏抽" in str(Validator().validate([make(charges=[], totals=[{"total_amount": "1"}])]).issues[0])
    assert codes(make(charges=[{}])) == ["V3"]
    assert codes(make(reference_no=None)) == ["V4"]
    assert codes(make(reference_no=None, booking_no="B1")) == []


def test_zero_amount_and_duplicate_lines_are_warnings():
    r = Validator().validate([make(charges=[{"amount": "0"}, {"name": "h c", "amount": "5"},
                                            {"name": "H/C", "amount": "5.00"}])])
    assert [i.code for i in r.warnings] == ["V8", "V6"] and r.errors == []
    # 同名不同金额（DN 两行 HANDLING CHARGE）不算重复
    assert codes(make(charges=[{"name": "HANDLING", "amount": "65"}, {"name": "HANDLING", "amount": "35"}])) == []
    # 币种不同不算重复
    assert codes(make(charges=[{"amount": "5", "currency_raw": "USD"}, {"amount": "5", "currency_raw": "CNY"}])) == []


def test_duplicate_charge_across_documents():
    body = make(reference_no=None, booking_no="B1", charges=[{"name": "H/C", "amount": "50", "currency_raw": "$"}])
    attach = make(reference_no="INV-1", charges=[{"name": "H/C", "amount": "50.00", "currency_raw": "$"}])
    r = Validator().validate([body, attach])
    [w] = r.warnings
    assert w.code == "V7" and w.doc == 2 and "单据 1 第 1 行" in w.message and r.errors == []


def test_same_reference_conflict_across_documents():
    a = make(issuer="ACME CO., LTD", document_date_raw="10/03/2025", currency_raw="USD")
    b = make(issuer="ACME CO LTD", document_date_raw="10/04/2025", currency_raw="usd", charges=[{"amount": "20"}])
    r = Validator().validate([a, b])
    assert [(i.code, i.field) for i in r.errors] == [("V5", "document_date_raw")]   # 公司名标点、币种大小写不算冲突


def test_severity_values():
    assert {s.value for s in Severity} == {"error", "warning"}
