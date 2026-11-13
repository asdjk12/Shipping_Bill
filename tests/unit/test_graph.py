"""M5 编排：节点串联、置信度、人工复核暂停与恢复、checkpoint 序列化、幂等。

抽取用替身 extract_fn（不调用 LLM），检验的是编排逻辑。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from bill_agent.extract import GateResult, GateStatus
from bill_agent.graph import NodeDeps, build_graph
from bill_agent.graph.build import sqlite_checkpointer
from bill_agent.harness.confidence import score_record
from bill_agent.models import ExtractedDocument, Operation, RawDocument
from bill_agent.normalize import Normalizer
from bill_agent.parsers import RouterConfig
from bill_agent.rules import EntityMatcher

from .conftest import make_xlsx

DOC_FIELDS = dict(document_type_raw="DEBIT NOTE", reference_no="DC-108478", document_date_raw="2026-10-24",
                  issuer="NEPTUNE SHIPPING LIMITED", recipient="AEBO TCO IMPORT LLC",
                  payee="NEPTUNE SHIPPING LIMITED", consignee="AEBO TCO IMPORT LLC",
                  bl_nos=["MAEU275752922"], container_nos=["MRSU2571577"], currency_raw="USD")
CHARGES = [{"line_no": 1, "charge_name_raw": "HANDLING CHARGE", "amount": 65, "payment_term_raw": "C"},
           {"line_no": 2, "charge_name_raw": "DETENTION CHARGE", "amount": "1,310.40", "payment_term_raw": "C"}]


def extracted(source_id: str, evidence: bool = True, **kw) -> ExtractedDocument:
    fields = {**DOC_FIELDS, **kw}
    ev = lambda v: {"source_id": source_id, "quote": str(v)}          # noqa: E731
    charges = [{**c, "evidence": {k: ev(v) for k, v in c.items() if k != "line_no"} if evidence else {}}
               for c in CHARGES]
    doc_ev = {k: ev(v) for k, v in fields.items()} if evidence else {}
    return ExtractedDocument.model_validate({"source_id": source_id, **fields, "charges": charges,
                                             "evidence": doc_ev})


class FakeExtract:
    """按调用次数返回预设的 GateResult；记录调用次数。"""

    def __init__(self, status=GateStatus.PASSED, reasons=(), evidence=True, warnings=(), **kw):
        self.status, self.reasons, self.evidence, self.warnings, self.kw = status, list(reasons), evidence, list(warnings), kw
        self.calls = 0

    def __call__(self, paths, docs: list[RawDocument]):
        self.calls += 1
        d = next(d for d in docs if d.ready_for_extraction)
        return [GateResult([d.source_path], self.status, [extracted(d.source_id, self.evidence, **self.kw)],
                           self.reasons, list(self.warnings))]


@pytest.fixture
def inputs(tmp_path):
    p = tmp_path / "dn.xlsx"
    p.write_bytes(make_xlsx())
    return [str(p)]


def deps(tmp_path, extract_fn, **kw) -> NodeDeps:
    return NodeDeps(router_config=RouterConfig(blob_dir=str(tmp_path / "blobs")), ocr_backend=None,
                    extract_fn=extract_fn, normalizer=Normalizer(), db_path=str(tmp_path / "bills.db"),
                    entity=EntityMatcher.load("PAT"), audit_dir=str(tmp_path / "audit"),
                    out_path=str(tmp_path / "out.json"), **kw)


def cfg(tid="r1"):
    return {"configurable": {"thread_id": tid}}


def ops(state):
    return [o.operation for o in state["outputs"]]


def test_end_to_end_creates_then_no_change(tmp_path, inputs):
    fake = FakeExtract()
    d = deps(tmp_path, fake)
    s = build_graph(d, InMemorySaver()).invoke({"inputs": inputs, "run_id": "r1"}, cfg())
    assert ops(s) == [Operation.CREATE, Operation.CREATE]
    assert [e.node for e in s["trace"]] == ["parse", "import_bills", "extract", "normalize", "score",
                                            "reconcile", "output"]
    o = s["outputs"][0]
    assert o.audit_trail.confidence == 1.0 and o.audit_trail.source_files == ["dn.xlsx"]
    assert any(r.startswith("置信度 1.00") for r in o.audit_trail.ai_reasoning)
    # 审计与输出文件
    assert len(json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))) == 2
    assert len((tmp_path / "audit" / "trace.jsonl").read_text(encoding="utf-8").splitlines()) == 7
    assert json.loads((tmp_path / "audit" / "extraction.json").read_text(encoding="utf-8"))[0]["status"] == "passed"

    # 同一文件再跑一次：结果一致，判定为 NO_CHANGE
    s2 = build_graph(d, InMemorySaver()).invoke({"inputs": inputs, "run_id": "r2"}, cfg("r2"))
    assert ops(s2) == [Operation.NO_CHANGE, Operation.NO_CHANGE]


def test_low_confidence_goes_to_review(tmp_path, inputs):
    s = build_graph(deps(tmp_path, FakeExtract(evidence=False), hitl=False)).invoke({"inputs": inputs})
    assert ops(s) == [Operation.PENDING_REVIEW] * 2
    reasons = s["outputs"][0].audit_trail.review_reasons
    assert any("置信度低于 0.8" in r and "缺少证据" in r for r in reasons)


def test_warnings_and_retries_lower_confidence():
    doc = extracted("s1")
    rec = Normalizer().normalize_document(doc)[0]
    c = score_record(rec, doc, {}, warnings=2, rounds=3)
    assert c.ocr == 1.0 and c.coverage == 1.0 and c.value == pytest.approx(0.95 ** 4, abs=1e-4)
    raw = RawDocument(source_id="s1", source_path="x.png", file_type="image", status="ocr_done",
                      text="x", ocr_engine="paddle", ocr_confidence=0.7)
    assert score_record(rec, doc, {"s1": raw}).ocr == 0.7


def test_needs_review_pauses_and_approve_resumes(tmp_path, inputs):
    fake = FakeExtract(GateStatus.NEEDS_REVIEW, ["审核未完成: 超时"])
    g = build_graph(deps(tmp_path, fake), InMemorySaver())
    s = g.invoke({"inputs": inputs, "run_id": "r1"}, cfg())
    tickets = s["__interrupt__"][0].value["tickets"]
    assert [t["ticket_id"] for t in tickets] == ["T1", "T2"]
    assert any("审核未完成" in r for r in tickets[0]["reasons"])

    s = g.invoke(Command(resume=[{"ticket_id": "T1", "action": "approve", "note": "已核对原件"},
                                 {"ticket_id": "T2", "action": "reject"}]), cfg())
    assert "__interrupt__" not in s
    assert ops(s) == [Operation.CREATE, Operation.PENDING_REVIEW]
    assert s["outputs"][0].audit_trail.ai_reasoning[0] == "人工确认（已核对原件）"
    assert s["outputs"][1].audit_trail.review_reasons[-1] == "人工驳回"
    assert fake.calls == 1                       # 恢复不会重跑抽取


def test_edit_applies_changes(tmp_path, inputs):
    g = build_graph(deps(tmp_path, FakeExtract(GateStatus.NEEDS_REVIEW, ["x"])), InMemorySaver())
    g.invoke({"inputs": inputs, "run_id": "r1"}, cfg())
    s = g.invoke(Command(resume=[{"ticket_id": "T1", "action": "edit", "changes": {"amount": "70"}},
                                 {"ticket_id": "T2", "action": "edit", "changes": {"currency": "US"}}]), cfg())
    assert s["outputs"][0].operation is Operation.CREATE and float(s["outputs"][0].charge_details.amount) == 70
    # 不合法的修改不入账，再问一轮
    assert s["outputs"][1].operation is Operation.PENDING_REVIEW
    assert [t["ticket_id"] for t in s["__interrupt__"][0].value["tickets"]] == ["T2"]


def test_review_rounds_are_capped(tmp_path, inputs):
    g = build_graph(deps(tmp_path, FakeExtract(GateStatus.NEEDS_REVIEW, ["x"]), max_review_rounds=1),
                    InMemorySaver())
    g.invoke({"inputs": inputs, "run_id": "r1"}, cfg())
    s = g.invoke(Command(resume=[{"ticket_id": "T1", "action": "edit", "changes": {"currency": "US"}}]), cfg())
    assert "__interrupt__" not in s and ops(s) == [Operation.PENDING_REVIEW] * 2


def test_sqlite_checkpoint_survives_restart(tmp_path, inputs):
    """状态里的 Pydantic 类型能存进 SQLite checkpoint，换一个图实例（模拟进程重启）也能恢复。"""
    db = tmp_path / "ckpt.db"
    fake = FakeExtract(GateStatus.NEEDS_REVIEW, ["x"])
    with sqlite3.connect(db, check_same_thread=False) as conn:
        build_graph(deps(tmp_path, fake), sqlite_checkpointer(conn)).invoke({"inputs": inputs, "run_id": "r1"}, cfg())
    with sqlite3.connect(db, check_same_thread=False) as conn:
        s = build_graph(deps(tmp_path, fake), sqlite_checkpointer(conn)).invoke(
            Command(resume=[{"ticket_id": "T1", "action": "approve"}, {"ticket_id": "T2", "action": "approve"}]),
            cfg())
    assert ops(s) == [Operation.CREATE, Operation.CREATE] and fake.calls == 1


def test_coverage_accepts_whole_line_quotes():
    """一条整行摘录挂在 amount 下，也能支撑同一行的名称、币种、单价（数字按数值比较）。"""
    from bill_agent.harness.confidence import evidence_coverage

    line = "WHARFAGE | TT | 2.85 | USD | 21.90"
    doc = ExtractedDocument.model_validate({
        "source_id": "s", "reference_no": "R1", "bl_nos": ["B1", "B2"],
        "evidence": {"reference_no": {"source_id": "s", "quote": "NO. R1"}, "bl_nos": {"source_id": "s", "quote": "B/L B1"}},
        "charges": [
            {"line_no": 1, "charge_name_raw": "WHARFAGE", "unit_price": 2.85, "amount": 21.9, "currency_raw": "USD",
             "evidence": {"amount": {"source_id": "s", "quote": line}}},
            {"line_no": 2, "charge_name_raw": "THC", "amount": 5, "currency_raw": "USD",
             "evidence": {"amount": {"source_id": "s", "quote": "5.00"}}},
        ]})
    assert evidence_coverage(doc, 1) == (1.0, [])
    cov, missing = evidence_coverage(doc, 2)
    assert missing == ["charge.charge_name_raw", "charge.currency_raw"] and cov == pytest.approx(3 / 5)
