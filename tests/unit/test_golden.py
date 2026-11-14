"""Golden 回归（harness/golden.py）：每个场景的输出与 tests/fixtures/golden/scenarios 中的期望逐字段一致。

期望变动时运行 `python -m bill_agent golden-eval --update`，人工核对 diff 后再提交。
"""

from __future__ import annotations

import pytest

from bill_agent.harness.golden import (FIELDS, compare_records, load_scenarios, project, record_key,
                                       run_scenario, set_path)

SCENARIOS = load_scenarios()


def test_scenarios_present():
    assert len(SCENARIOS) >= 6
    assert all(any(b.expected for b in sc.batches) for sc in SCENARIOS), "每个场景至少有一个带期望的批次"


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_scenario_matches_golden(sc):
    for i, r in enumerate(run_scenario(sc), 1):
        if r.comparison is None:
            continue
        c = r.comparison
        detail = [f"{d.key} {d.field}: 期望 {d.expected!r}，实际 {d.actual!r}" for d in c.diffs]
        assert c.ok, f"批次 {i}：差异 {detail}；缺少 {len(c.missing)} 条；多出 {len(c.extra)} 条"


def test_compare_reports_field_diff_missing_and_extra():
    base = {f: None for f in FIELDS} | {"operation": "CREATE", "reference_no": "DN-1", "line_no": 1,
                                         "original_charge_item": "THC", "amount": 40.0, "bl_nos": []}
    other = base | {"line_no": 2}
    c = compare_records([base, other], [base | {"amount": 40.001, "operation": "UPDATE"}, base | {"line_no": 9}])
    assert [d.field for d in c.diffs] == ["operation"]       # 金额差 < 0.005 视为一致
    assert c.missing == [other] and len(c.extra) == 1
    assert not c.ok


def test_compare_party_names_ignore_punctuation():
    base = {f: None for f in FIELDS} | {"reference_no": "X", "line_no": 1, "issuer": "ZIM Co., LLC"}
    assert compare_records([base], [base | {"issuer": "zim co llc"}]).ok


def test_record_key_falls_back_to_bl():
    assert record_key({"reference_no": None, "bl_nos": ["CMDU1"], "line_no": 1, "original_charge_item": "a"}) == \
        ("cmdu1", 1, "a")


def test_set_path():
    data = {"charges": [{"amount": 1}, {"amount": 2}], "payer": None}
    set_path(data, "charges[1].amount", 9)
    set_path(data, "payer", "PAT")
    assert data == {"charges": [{"amount": 1}, {"amount": 9}], "payer": "PAT"}


def test_project_flattens_output_record():
    from bill_agent.models import (AuditTrail, ChargeDetails, DocumentInfo, FieldChange, FinancialAttributes,
                                   Operation, OutputRecord, TransactionParty)

    rec = OutputRecord(operation=Operation.UPDATE, document_info=DocumentInfo(reference_no="DN-1"),
                       transaction_party=TransactionParty(issuer="A"), charge_details=ChargeDetails(amount=1),
                       financial_attributes=FinancialAttributes(),
                       audit_trail=AuditTrail(changed_fields=[FieldChange(field="amount", old_value=0, new_value=1)],
                                              review_reasons=["x"]))
    p = project(rec)
    assert set(FIELDS) <= set(p)
    assert p["changed_fields"] == ["amount"] and p["needs_review"] is True and p["operation"] == "UPDATE"
