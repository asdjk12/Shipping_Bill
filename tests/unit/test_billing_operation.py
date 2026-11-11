"""4 级匹配（billing/matcher.py）与操作判定（billing/operation.py）。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from bill_agent.billing import BillProcessor, BillRepository, Matcher, PartyNormalizer
from bill_agent.models import ExtractedDocument, MatchRule, Operation, PostingType, to_json
from bill_agent.normalize import Normalizer
from bill_agent.rules import EntityMatcher

NORMALIZER = Normalizer()


def doc(source_id="dn-v1", charges=None, **kw) -> ExtractedDocument:
    base = dict(source_id=source_id, document_type_raw="DEBIT NOTE", reference_no="DC-108478",
                document_date_raw="2026-10-24", issuer="NEPTUNE SHIPPING LIMITED", recipient="AEBO TCO IMPORT LLC",
                payee="NEPTUNE SHIPPING LIMITED", consignee="AEBO TCO IMPORT LLC",
                bl_nos=["MAEU275752922"], container_nos=["MRSU2571577"], currency_raw="USD",
                charges=charges if charges is not None else [
                    {"line_no": 1, "charge_name_raw": "HANDLING CHARGE", "amount": 65, "payment_term_raw": "C"},
                    {"line_no": 2, "charge_name_raw": "DETENTION CHARGE", "amount": "1,310.40", "payment_term_raw": "C"},
                    {"line_no": 3, "charge_name_raw": "HANDLING CHARGE", "amount": 35, "payment_term_raw": "C"},
                ])
    return ExtractedDocument.model_validate({**base, **kw})


def item(d: ExtractedDocument, reasons=None):
    return d, NORMALIZER.normalize_document(d, reasons)


@pytest.fixture
def repo():
    with BillRepository(":memory:") as r:
        yield r


@pytest.fixture
def proc(repo):
    return BillProcessor(repo, Matcher(repo, PartyNormalizer.load()), EntityMatcher.load("PAT"), NORMALIZER)


def ops(results):
    return [r.operation for r in results]


def test_first_batch_creates_same_name_lines(proc, repo):
    res = proc.process_batch([item(doc())])
    assert ops(res) == [Operation.CREATE] * 3
    assert repo.count() == 3
    # 第 3 行与第 1 行同名，规则 1 命中第 1 行但因同一单据行号不同被排除
    assert res[2].match.hits[0].candidates and not res[2].match.hits[0].narrowed


def test_same_document_again_is_no_change(proc, repo):
    proc.process_batch([item(doc())])
    res = proc.process_batch([item(doc())])
    assert ops(res) == [Operation.NO_CHANGE] * 3
    assert all(r.match.rule is MatchRule.REF_NO_AND_CHARGE for r in res)
    assert repo.count() == 3 and all(rec.version == 1 for rec in repo.all())


def test_revised_document_updates_only_changed_fields(proc, repo):
    first = proc.process_batch([item(doc())])
    revised = doc("dn-v2", consignee=None, charges=[
        {"line_no": 1, "charge_name_raw": "HANDLING CHARGE", "amount": 70, "payment_term_raw": "C"},
        {"line_no": 2, "charge_name_raw": "DETENTION CHARGE", "amount": "1,310.40", "payment_term_raw": "C"},
        {"line_no": 3, "charge_name_raw": "HANDLING CHARGE", "amount": 35, "payment_term_raw": "C"},
    ])
    res = proc.process_batch([item(revised)])
    assert ops(res) == [Operation.UPDATE, Operation.NO_CHANGE, Operation.NO_CHANGE]
    upd = res[0]
    assert upd.record_id == first[0].record_id
    assert [(c.field, c.old_value, c.new_value) for c in upd.changes] == [("amount", 65.0, 70.0)]
    stored = repo.get(upd.record_id)
    assert stored.amount == Decimal("70") and stored.version == 2
    assert stored.consignee == "AEBO TCO IMPORT LLC"                 # null 不覆盖原值
    assert stored.source_ids == ["dn-v1", "dn-v2"]
    assert repo.history(upd.record_id)[0]["changed_fields"] == [
        {"field": "amount", "old_value": "65", "new_value": "70"}]


def test_later_batch_without_ref_merges_by_bl(proc, repo):
    """分批次抽到同一笔交易：没有单据号的到货通知按提单号匹配，合并进已有记录。"""
    proc.process_batch([item(doc())])
    notice = doc("notice", reference_no=None, document_type_raw="ARRIVAL NOTICE",
                 container_nos=["TCLU8619685"], booking_no="BK-1",
                 charges=[{"line_no": 1, "charge_name_raw": "DETENTION CHARGE", "amount": "1,310.40"}])
    [r] = proc.process_batch([item(notice)])
    assert r.operation is Operation.UPDATE and r.match.rule is MatchRule.BL_NO_CHARGE_CURRENCY
    stored = repo.get(r.record_id)
    assert stored.container_nos == ["MRSU2571577", "TCLU8619685"]     # 箱号取并集
    assert stored.booking_no == "BK-1" and stored.reference_no == "DC-108478"
    assert stored.payment_term.value == "CC"                          # 新单据没有付款方式，保留原值
    assert repo.count() == 3


def test_rule4_compares_parties_through_alias(proc, repo):
    proc.process_batch([item(doc(issuer="PAT", charges=[{"line_no": 1, "charge_name_raw": "THC", "amount": 80}]))])
    other = doc("x", reference_no=None, bl_nos=[], issuer="Pilot Aiship Tech co., Ltd.",
                charges=[{"line_no": 1, "charge_name_raw": "Terminal Handling", "amount": 80}])
    [r] = proc.process_batch([item(other)])
    assert r.match.rule is MatchRule.CONTAINER_PARTIES_DATE_AMOUNT and r.match.record is not None


def test_ambiguous_same_name_lines_conflict(proc, repo):
    proc.process_batch([item(doc())])
    other = doc("other", charges=[{"line_no": 9, "charge_name_raw": "HANDLING CHARGE", "amount": 50}])
    [r] = proc.process_batch([item(other)])
    assert r.operation is Operation.PENDING_REVIEW and "多条" in r.review_reasons[0]
    assert repo.count() == 3


def test_rules_hitting_different_records_conflict(proc, repo):
    proc.process_batch([item(doc("a", reference_no="R1", bl_nos=["B0"], charges=[
        {"line_no": 1, "charge_name_raw": "THC", "amount": 10}]))])
    proc.process_batch([item(doc("b", reference_no="R2", bl_nos=["B1"], charges=[
        {"line_no": 1, "charge_name_raw": "THC", "amount": 20}]))])
    [r] = proc.process_batch([item(doc("c", reference_no="R1", bl_nos=["B1"], charges=[
        {"line_no": 1, "charge_name_raw": "THC", "amount": 30}]))])
    assert r.operation is Operation.PENDING_REVIEW and "另一条记录" in r.review_reasons[0]


def test_two_rows_in_one_batch_matching_same_record(proc, repo):
    proc.process_batch([item(doc(charges=[{"line_no": 1, "charge_name_raw": "THC", "amount": 10}]))])
    a = doc("a", reference_no=None, charges=[{"line_no": 1, "charge_name_raw": "THC", "amount": 11}])
    b = doc("b", reference_no=None, charges=[{"line_no": 1, "charge_name_raw": "THC", "amount": 12}])
    res = proc.process_batch([item(a), item(b)])
    assert ops(res) == [Operation.UPDATE, Operation.PENDING_REVIEW]
    assert "本批次" in res[1].review_reasons[0]


def test_upstream_review_reasons_are_not_written(proc, repo):
    res = proc.process_batch([item(doc(), reasons=["审核未通过"])])
    assert ops(res) == [Operation.PENDING_REVIEW] * 3 and repo.count() == 0


def test_change_instruction_only_touches_named_field(proc, repo):
    created = proc.process_batch([item(doc())])
    email = doc("mail", reference_no=None, charges=[], change_instructions=[
        {"target_reference_no": "DC-108478", "target_charge_name_raw": "Detention Charge", "field": "amount",
         "new_value_raw": "USD 1,200.00", "evidence": {"source_id": "mail", "quote": "请把滞箱费改为 USD 1,200.00"}},
        {"target_reference_no": "DC-108478", "target_charge_name_raw": "HANDLING CHARGE", "field": "amount",
         "new_value_raw": "40", "evidence": {"source_id": "mail", "quote": "Handling 改 40"}},
    ])
    res = proc.process_batch([(email, [])])
    upd = next(r for r in res if r.operation is Operation.UPDATE)
    pend = next(r for r in res if r.operation is Operation.PENDING_REVIEW)
    assert upd.record_id == created[1].record_id
    assert [(c.field, c.new_value, c.evidence) for c in upd.changes] == [
        ("amount", 1200.0, "请把滞箱费改为 USD 1,200.00")]
    assert "2 条记录" in pend.review_reasons[0]                       # 两行 HANDLING，无法确定


def test_document_with_instruction_ignores_other_differences(proc, repo):
    proc.process_batch([item(doc())])
    revised = doc("dn-v2", issuer="SOMEONE ELSE", change_instructions=[
        {"target_charge_name_raw": "DETENTION CHARGE", "field": "currency", "new_value_raw": "EUR",
         "evidence": {"source_id": "dn-v2", "quote": "currency EUR"}}])
    res = proc.process_batch([item(revised)])
    assert ops(res) == [Operation.NO_CHANGE, Operation.UPDATE, Operation.NO_CHANGE]
    assert [c.field for c in res[1].changes] == ["currency"]
    assert all(r.issuer == "NEPTUNE SHIPPING LIMITED" for r in repo.all())


def test_posting_type_recomputed_on_merged_record(proc, repo):
    [c] = proc.process_batch([item(doc(charges=[{"line_no": 1, "charge_name_raw": "THC", "amount": 10}]))])
    assert c.record.posting_type is None
    [u] = proc.process_batch([item(doc("v2", payer="PAT", charges=[{"line_no": 1, "charge_name_raw": "THC",
                                                                     "amount": 10}]))])
    assert u.operation is Operation.UPDATE
    assert {ch.field for ch in u.changes} == {"payer", "posting_type"}
    assert repo.get(u.record_id).posting_type is PostingType.AP


def test_output_is_valid_json(proc):
    res = proc.process_batch([item(doc())])
    out = json.loads(to_json([r.to_output() for r in res]))
    assert out[0]["operation"] == "CREATE" and out[0]["charge_details"]["amount"] == 65.0
    assert json.loads(to_json([r.to_output() for r in res], strict=True))[0].keys() == {
        "operation", "document_info", "transaction_party", "charge_details", "financial_attributes", "audit_trail"}


def test_unmapped_charge_name_case_differs_across_batches(proc, repo):
    """标准化表里没有的费用名照抄原文；后一批只是大小写 / 标点不同，仍匹配到同一条记录且不算变更。"""
    [c] = proc.process_batch([item(doc(charges=[{"line_no": 1, "charge_name_raw": "CUSTOMS INSPECTION FEE",
                                                  "amount": 80}]))])
    [u] = proc.process_batch([item(doc("v2", charges=[{"line_no": 1, "charge_name_raw": "Customs Inspection-Fee",
                                                        "amount": 80}]))])
    assert u.operation is Operation.NO_CHANGE and u.record_id == c.record_id
    assert repo.get(c.record_id).original_charge_item == "CUSTOMS INSPECTION FEE"
    [u] = proc.process_batch([item(doc("v3", charges=[{"line_no": 1, "charge_name_raw": "customs inspection fee",
                                                        "amount": 90}]))])
    assert u.operation is Operation.UPDATE and [ch.field for ch in u.changes] == ["amount"]
    assert repo.count() == 1
