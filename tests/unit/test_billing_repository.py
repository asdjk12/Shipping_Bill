"""账单库（billing/repository.py）。"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from bill_agent.billing import BillRepository, VersionConflict, bill_from_json
from bill_agent.billing.repository import _COLUMNS, _LIST_TABLES
from bill_agent.models import BillRecord, DocumentType, Evidence, PaymentTerm
from bill_agent.parsers.base import BillJSON


@pytest.fixture
def repo():
    with BillRepository(":memory:") as r:
        yield r


def make(**kw) -> BillRecord:
    base = dict(document_type=DocumentType.DEBIT_NOTE, reference_no="DC-108478", document_date=date(2026, 10, 24),
                issuer="NEPTUNE", recipient="AEBO", bl_nos=["BL1", "BL2"], booking_no="BK1",
                container_nos=["CNTR0000001"], line_no=1, charge_item="Handling Charge",
                original_charge_item="HANDLING CHARGE", amount="65.00", currency="USD",
                payment_term=PaymentTerm.CC, source_ids=["src-a"],
                evidence={"amount": Evidence(source_id="src-a", quote="65.00")})
    return BillRecord(**{**base, **kw})


def test_columns_cover_model():
    assert set(_COLUMNS) | set(_LIST_TABLES) == set(BillRecord.model_fields)


def test_insert_get_roundtrip(repo):
    rec = repo.insert(make())
    got = repo.get(rec.record_id)
    assert got == rec
    assert got.amount == Decimal("65") and got.bl_nos == ["BL1", "BL2"]
    assert got.evidence["amount"].quote == "65.00"
    assert repo.count() == 1 and repo.get("nope") is None


def test_find_rules(repo):
    a = repo.insert(make())
    b = repo.insert(make(line_no=2, amount="35.00"))              # 同名费用行（样例 DN）
    c = repo.insert(make(reference_no="X-1", charge_item="Ocean Freight", original_charge_item="O/F",
                         bl_nos=["BL9"], container_nos=["CNTR0000009"], amount="100"))

    assert {r.record_id for r in repo.find_by_ref_charge("DC-108478", "Handling Charge")} == {a.record_id, b.record_id}
    assert [r.record_id for r in repo.find_by_bl("BL9", "Ocean Freight", "USD")] == [c.record_id]
    assert repo.find_by_bl("BL9", "Ocean Freight", "EUR") == []
    assert len(repo.find_by_booking("BK1", "Handling Charge", "USD")) == 2
    # 金额按数值比较："35" 与 "35.00" 是同一个值
    assert [r.record_id for r in repo.find_by_container("CNTR0000001", date(2026, 10, 24), Decimal("35"))] == [b.record_id]
    assert [r.record_id for r in repo.find_by_container("CNTR0000009", date(2026, 10, 24), Decimal("1E+2"))] == [c.record_id]


@pytest.mark.parametrize("call", [
    lambda r: r.find_by_ref_charge(None, "Handling Charge"),
    lambda r: r.find_by_bl("BL1", "Handling Charge", None),
    lambda r: r.find_by_booking("BK1", None, "USD"),
    lambda r: r.find_by_container("CNTR0000001", None, Decimal("65")),
])
def test_null_key_skips_rule(repo, call):
    repo.insert(make())
    assert call(repo) == []


def test_update_writes_history(repo):
    rec = repo.insert(make())
    new = repo.update(rec.record_id, {"amount": "70", "bl_nos": ["BL3"]}, expected_version=1, source_id="src-b")
    assert new.version == 2 and new.amount == Decimal("70") and new.source_ids == ["src-a", "src-b"]
    assert repo.get(rec.record_id) == new
    assert repo.find_by_bl("BL1", "Handling Charge", "USD") == []
    assert len(repo.find_by_bl("BL3", "Handling Charge", "USD")) == 1

    [h] = repo.history(rec.record_id)
    assert h["version"] == 1 and h["snapshot"]["amount"] == 65.0 and h["source_id"] == "src-b"
    assert {c["field"]: (c["old_value"], c["new_value"]) for c in h["changed_fields"]} == {
        "amount": ("65", "70"), "bl_nos": (["BL1", "BL2"], ["BL3"])}


def test_update_same_value_is_noop(repo):
    rec = repo.insert(make())
    assert repo.update(rec.record_id, {"amount": "65"}, expected_version=1).version == 1
    assert repo.history(rec.record_id) == []


def test_update_version_conflict(repo):
    rec = repo.insert(make())
    repo.update(rec.record_id, {"amount": "70"}, expected_version=1)
    with pytest.raises(VersionConflict):
        repo.update(rec.record_id, {"amount": "80"}, expected_version=1)
    with pytest.raises(VersionConflict):
        repo.update("missing", {"amount": "80"}, expected_version=1)


def test_update_rejects_invalid_value(repo):
    rec = repo.insert(make())
    with pytest.raises(ValueError):
        repo.update(rec.record_id, {"currency": "usd dollar"}, expected_version=1)
    assert repo.get(rec.record_id).currency == "USD"


def test_transaction_rolls_back(repo):
    with pytest.raises(RuntimeError):
        with repo.transaction():
            repo.insert(make())
            repo.insert(make(line_no=2))
            raise RuntimeError("boom")
    assert repo.count() == 0


def test_file_db_persists(tmp_path):
    path = tmp_path / "sub" / "bills.db"
    with BillRepository(path) as r:
        rec = r.insert(make())
    with BillRepository(path) as r:
        assert r.get(rec.record_id) == rec


# ---------------------------------------------------------------- 导入已有账单 JSON

OUTPUT_SHAPE = {
    "operation": "CREATE",
    "document_info": {"document_type": "Debit Note", "reference_no": "DN-202609-001", "document_date": "2026-09-20"},
    "transaction_party": {"issuer": "Destination Agent", "recipient": "Origin Agent"},
    "charge_details": {"charge_item": "Handling Charge", "amount": 40.00, "currency": "USD", "payment_term": "PP"},
    "financial_attributes": {"accounting_entity": "Origin Agent", "posting_type": "AP"},
    "audit_trail": {"ai_reasoning": ["..."]},
}


def test_bill_from_output_shape():
    rec, warnings = bill_from_json(OUTPUT_SHAPE, "sid", 0)
    assert rec.reference_no == "DN-202609-001" and rec.amount == Decimal("40")
    assert rec.original_charge_item == "Handling Charge" and rec.posting_type.value == "AP"
    assert rec.source_ids == ["sid"]
    assert any("原始费用名称" in w for w in warnings)
    assert bill_from_json(OUTPUT_SHAPE, "sid", 0)[0].record_id == rec.record_id   # 固定 id


def test_bill_from_flat_shape():
    rec, warnings = bill_from_json({"record_id": "r1", "reference_no": "A", "charge_item": "THC",
                                    "charge_item_raw": "T.H.C", "bl_no": "BL1", "container_no": ["C1", "C2"],
                                    "foo": 1, "version": 9}, "sid", 0)
    assert rec.record_id == "r1" and rec.original_charge_item == "T.H.C" and rec.version == 1
    assert rec.bl_nos == ["BL1"] and rec.container_nos == ["C1", "C2"]
    assert any("'foo'" in w for w in warnings)


def test_bill_from_json_errors():
    with pytest.raises(ValueError, match="费用名称"):
        bill_from_json({"reference_no": "A"}, "sid", 0)
    with pytest.raises(ValueError, match="currency"):
        bill_from_json({"charge_item": "THC", "currency": "dollar"}, "sid", 0)


def test_import_is_idempotent(repo):
    bad = {"charge_item": "THC", "currency": "dollar"}
    bj = BillJSON("bills.json", "sid", [OUTPUT_SHAPE, bad])
    r1 = repo.import_bills(bj)
    assert len(r1.created) == 1 and len(r1.errors) == 1
    r2 = repo.import_bills(bj)
    assert r2.unchanged == r1.created and not r2.created and not r2.updated

    changed = json.loads(json.dumps(OUTPUT_SHAPE))
    changed["charge_details"]["amount"] = 45
    r3 = repo.import_bills(BillJSON("bills.json", "sid", [changed]))
    assert r3.updated == r1.created
    assert repo.get(r1.created[0]).version == 2 and len(repo.history(r1.created[0])) == 1


# ---------------------------------------------------------------- 费用名按 charge_key 比较

def test_find_ignores_case_space_punctuation(repo):
    rec = repo.insert(make(charge_item="DETENTION CHARGE", original_charge_item="DETENTION CHARGE"))
    for name in ("Detention Charge", "detention-charge", " Detention  Charge. "):
        assert [r.record_id for r in repo.find_by_ref_charge("DC-108478", name)] == [rec.record_id]
        assert [r.record_id for r in repo.find_by_bl("BL1", name, "USD")] == [rec.record_id]
        assert [r.record_id for r in repo.find_by_booking("BK1", name, "USD")] == [rec.record_id]
    assert repo.find_by_ref_charge("DC-108478", "Detention") == []     # 不做同义 / 部分匹配


def test_charge_key_follows_update(repo):
    rec = repo.insert(make())
    repo.update(rec.record_id, {"charge_item": "Storage"}, expected_version=1)
    assert repo.find_by_ref_charge("DC-108478", "Handling Charge") == []
    assert len(repo.find_by_ref_charge("DC-108478", "STORAGE")) == 1


def test_migrate_v1_database(tmp_path):
    """第 1 版结构（没有 charge_key 列）的库打开时自动迁移并补齐 charge_key。"""
    import sqlite3

    from bill_agent.billing.repository import _DDL

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(_DDL)
    conn.execute("INSERT INTO bill_records (record_id, reference_no, charge_item, original_charge_item, version, "
                 "updated_at) VALUES ('r1', 'A', 'DETENTION CHARGE', 'DETENTION CHARGE', 1, '2026-01-01T00:00:00')")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    with BillRepository(path) as r:
        assert r.conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert [x.record_id for x in r.find_by_ref_charge("A", "Detention Charge")] == ["r1"]
