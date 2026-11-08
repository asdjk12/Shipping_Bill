"""代码层数字检查、LLM 审核、重试上限。

LLM 用固定输出的替身，检验的是代码逻辑；审核模型的真实效果用
python -m bill_agent review-eval 跑（调用 DeepSeek）。
"""

from pathlib import Path

from langchain_core.runnables import RunnableLambda

from bill_agent.extract import Extractor, GateStatus, ReviewGate, Reviewer
from bill_agent.extract.extractor import LLMExtraction, LLMPatch, convert
from bill_agent.extract.numbers import check_numbers, numbers_in
from bill_agent.extract.review_eval import detected, mutations
from bill_agent.extract.reviewer import LLMReview, ReviewIssue, ReviewResult, flatten
from bill_agent.models import ExtractedDocument

from .test_extract import DN_TEXT, dn_output, doc

SAMPLES = Path(__file__).parents[1] / "fixtures" / "samples"


class ScriptedLLM:
    """抽取和审核各按顺序返回预设输出，用完后重复最后一个。"""

    def __init__(self, extractions: list[dict], reviews: list[dict | None], patches: list[dict] | None = None):
        self.script = {LLMExtraction: extractions, LLMReview: reviews, LLMPatch: patches or [{}]}
        self.calls = {LLMExtraction: [], LLMReview: [], LLMPatch: []}

    def with_structured_output(self, schema, include_raw=False, **kw):
        def run(messages):
            self.calls[schema].append(messages)
            outputs = self.script[schema]
            out = outputs[min(len(self.calls[schema]), len(outputs)) - 1]
            if out is None:
                return {"raw": "不是 JSON", "parsed": None, "parsing_error": ValueError("not json")}
            return {"raw": None, "parsed": schema.model_validate(out), "parsing_error": None}
        return RunnableLambda(run)


def all_ok(extraction: dict) -> dict:
    """对这份抽取结果的每个字段都判合格的审核输出。"""
    docs, _ = convert(LLMExtraction.model_validate(extraction), [doc("d", DN_TEXT)])
    return {"verdicts": [{"document": i, "field": f, "ok": True}
                         for i, d in enumerate(docs, start=1) for f in flatten(d)]}


def rejected(extraction: dict, field="reference_no", quote="D/C No. : DC-108478") -> dict:
    review = all_ok(extraction)
    keep = [v for v in review["verdicts"] if v["field"] != field]
    return {"verdicts": keep + [{"document": 1, "field": field, "ok": False, "reason": "填错了", "quote": quote}]}


def gate(llm, **kw) -> ReviewGate:
    return ReviewGate(Extractor(llm, max_retries=1), Reviewer(llm), **kw)


def golden(name="DN_DC-108478") -> ExtractedDocument:
    return ExtractedDocument.model_validate_json(
        (SAMPLES / f"{name}.expected_extraction.json").read_text(encoding="utf-8"))


def make(charges, totals, **kw) -> ExtractedDocument:
    rows = [{"line_no": i, "charge_name_raw": "X", **c} for i, c in enumerate(charges, start=1)]
    return ExtractedDocument(source_id="d", charges=rows, totals=totals, **kw)


# ---------- 数字检查 ----------

def test_numbers_in_takes_whole_numbers():
    assert numbers_in("TOTAL 1,310.40 USD, qty 2") == numbers_in("1310.4 2")
    assert 50 not in numbers_in("150.00")


def test_check_numbers_on_all_golden_samples():
    files = list(SAMPLES.glob("*.expected_extraction.json"))
    assert len(files) == 3
    for f in files:
        assert check_numbers(ExtractedDocument.model_validate_json(f.read_text(encoding="utf-8"))) == []


def test_check_numbers_line_and_totals():
    d = make([{"quantity": 2, "unit_price": 10, "amount": 25}],
             [{"currency_raw": "USD", "subtotal": 20, "tax_amount": 2, "total_amount": 30}], currency_raw="USD")
    assert len(check_numbers(d)) == 3


def test_check_numbers_groups_by_currency_and_allows_symbols():
    usd_cny = make([{"amount": 10, "currency_raw": "USD"}, {"amount": 70, "currency_raw": "CNY"}],
                   [{"currency_raw": "USD", "total_amount": 10}, {"currency_raw": "CNY", "total_amount": 70}])
    assert check_numbers(usd_cny) == []
    symbol = make([{"amount": 10, "currency_raw": "$"}], [{"currency_raw": "USD", "total_amount": 10}])
    assert check_numbers(symbol) == []


def test_check_numbers_credit_note_and_missing_values():
    credit = make([{"amount": -35}], [{"currency_raw": "USD", "total_amount": 35}], currency_raw="USD")
    assert check_numbers(credit) == []
    assert check_numbers(make([{"amount": None}], [{"currency_raw": "USD", "total_amount": 35}])) == []
    assert check_numbers(make([{"amount": 5, "quantity": None, "unit_price": 3}], [])) == []


# ---------- 审核 ----------

def test_flatten_keeps_null_fields():
    f = flatten(golden())
    assert f["recipient"] is None and f["charges[2].amount"] == "1310.40" and "totals[1].total_amount" in f


def test_review_issue_needs_quote_found_in_source():
    good = dn_output()
    review = rejected(good)
    review["verdicts"][0] |= {"ok": False, "reason": "编的", "quote": "这句话原文里没有"}
    review["omissions"] = [{"description": "漏了", "quote": "HANDLING CHARGE"}]
    llm = ScriptedLLM([good], [review])
    docs = Extractor(llm).extract_group([doc("d", DN_TEXT)]).documents
    r = Reviewer(llm).review([doc("d", DN_TEXT)], docs)
    assert [i.field for i in r.issues] == ["单据 1 reference_no", "漏抽"]
    assert [i.reason for i in r.dismissed] == ["编的"]


def test_review_retries_when_fields_are_missing_then_gives_up():
    good = dn_output()
    full = all_ok(good)
    partial = {"verdicts": full["verdicts"][:3]}
    llm = ScriptedLLM([good], [partial, full])
    docs = Extractor(llm).extract_group([doc("d", DN_TEXT)]).documents
    r = Reviewer(llm).review([doc("d", DN_TEXT)], docs)
    assert r.attempts == 2 and r.incomplete is None
    assert "缺少这些字段的结论" in llm.calls[LLMReview][1][-1].content

    llm = ScriptedLLM([good], [partial])
    r = Reviewer(llm).review([doc("d", DN_TEXT)], docs)
    assert r.attempts == 2 and "缺少这些字段的结论" in r.incomplete


def test_review_unparsed_output_is_retried():
    good = dn_output()
    llm = ScriptedLLM([good], [None, all_ok(good)])
    docs = Extractor(llm).extract_group([doc("d", DN_TEXT)]).documents
    r = Reviewer(llm).review([doc("d", DN_TEXT)], docs)
    assert r.attempts == 2 and r.incomplete is None and r.raw_outputs[0] == {"unparsed": "不是 JSON"}


# ---------- 把关流程与上限 ----------

def test_gate_passes():
    good = dn_output()
    r = gate(ScriptedLLM([good], [all_ok(good)])).run_group([doc("d", DN_TEXT)])
    assert r.status is GateStatus.PASSED and r.reasons == [] and len(r.rounds) == 1


def test_gate_sends_review_issue_back_to_extraction():
    good = dn_output()
    llm = ScriptedLLM([good], [rejected(good), all_ok(good)])
    r = gate(llm).run_group([doc("d", DN_TEXT)])
    assert r.status is GateStatus.PASSED and len(r.rounds) == 2
    retry = llm.calls[LLMExtraction][1][-1].content
    assert "单据 1 reference_no: 填错了" in retry and "DC-108478" in retry and "保持不变" in retry


def test_gate_stops_at_review_limit():
    good = dn_output()
    llm = ScriptedLLM([good], [rejected(good)])
    r = gate(llm, max_review_retries=1).run_group([doc("d", DN_TEXT)])
    assert r.status is GateStatus.NEEDS_REVIEW and len(r.documents) == 1
    assert len(r.rounds) == 2 and len(llm.calls[LLMReview]) == 2
    assert "填错了" in r.reasons[0]

    llm = ScriptedLLM([good], [rejected(good)])
    r = gate(llm, max_review_retries=0).run_group([doc("d", DN_TEXT)])
    assert len(r.rounds) == 1 and r.status is GateStatus.NEEDS_REVIEW


def test_gate_skips_review_when_code_checks_fail():
    bad = dn_output(reference_no="DC-000000")
    llm = ScriptedLLM([bad], [{}])
    r = gate(llm).run_group([doc("d", DN_TEXT)])
    assert r.status is GateStatus.NEEDS_REVIEW and any("DC-000000" in e for e in r.reasons)
    assert len(llm.calls[LLMExtraction]) == 1 and len(llm.calls[LLMPatch]) == 1   # max_retries=1，重试只补字段
    assert llm.calls[LLMReview] == []


def test_gate_sends_cleared_fields_to_human_review():
    no_evidence = dn_output(issuer="MAEU275752922")
    llm = ScriptedLLM([no_evidence], [all_ok(no_evidence)])
    r = gate(llm).run_group([doc("d", DN_TEXT)])
    assert r.status is GateStatus.NEEDS_REVIEW
    assert r.reasons == ["证据不足被清空，需人工确认: 单据 1 issuer='MAEU275752922'"]


def test_gate_flags_unfinished_review():
    good = dn_output()
    r = gate(ScriptedLLM([good], [None])).run_group([doc("d", DN_TEXT)])
    assert r.status is GateStatus.NEEDS_REVIEW and r.reasons[0].startswith("审核未完成")


def test_gate_extract_no_bills_passes():
    llm = ScriptedLLM([{"documents": []}], [{"verdicts": []}])
    [r] = gate(llm).extract([doc("a", "BILL OF LADING")])
    assert r.status is GateStatus.PASSED and r.documents == []


# ---------- 评测用的改坏方式 ----------

def test_mutations_keep_arithmetic_except_dropped_line():
    d = golden()
    ms = {m.name: m for m in mutations(d)}
    assert {"单据号与提单号互换", "收款方漏填", "数量与单价互换", "漏抽一行费用"} <= ms.keys()
    assert ms["单据号与提单号互换"].document.reference_no == d.bl_nos[0]
    assert ms["数量与单价互换"].document.charges[0].unit_price == 1
    assert check_numbers(ms["数量与单价互换"].document) == []


def test_detected_matches_field_and_omission():
    m = next(x for x in mutations(golden()) if x.name == "收款方漏填")
    assert detected(ReviewResult(issues=[ReviewIssue("单据 1 payee", "漏填")]), m)
    assert not detected(ReviewResult(issues=[ReviewIssue("单据 1 payer", "漏填")]), m)
