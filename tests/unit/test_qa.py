"""文件问答与 OCR 缓存。LLM 用假实现，不调用接口；真实评测用 python -m bill_agent qa-eval。"""

import json
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableLambda

from bill_agent.models import DocStatus, FileType, RawDocument
from bill_agent.ocr import OCRResult
from bill_agent.ocr.cache import CachedOCR
from bill_agent.qa import Answer, DocumentQA, build_context, score, verify_evidence

QA_DIR = Path(__file__).parents[1] / "fixtures" / "qa"


def ocr_doc(text: str, warnings=()) -> RawDocument:
    return RawDocument(source_id="abc", source_path="inv.png", file_type=FileType.IMAGE,
                       status=DocStatus.OCR_DONE, text=text, ocr_engine="paddleocr/PP-StructureV3",
                       ocr_confidence=0.96, warnings=list(warnings))


class FakeLLM:
    def __init__(self, answer: Answer):
        self.answer = answer
        self.prompts = []

    def with_structured_output(self, schema, **kw):
        assert schema is Answer

        def run(prompt_value):
            self.prompts.append(prompt_value.to_messages())
            return self.answer
        return RunnableLambda(run)


def test_build_context_includes_meta_and_low_conf():
    pending = RawDocument(source_id="p", source_path="scan.pdf", file_type=FileType.PDF,
                         status=DocStatus.PENDING_OCR, file_ref="x")
    ctx = build_context([pending, ocr_doc("INVOICE ORFR1618731", ["低置信度文本 'Zin Amer'（0.75）"])])
    assert "scan.pdf" not in ctx                       # 未完成 OCR 的文档不进入上下文
    assert "文档 1：inv.png" in ctx and "识别置信度 0.96" in ctx
    assert "INVOICE ORFR1618731" in ctx and "Zin Amer" in ctx


def test_verify_evidence_ignores_whitespace():
    ctx = "WHARFAGE        TT     2.85   USD    21.90"
    assert verify_evidence(["WHARFAGE TT 2.85 USD 21.90", "wharfage", "PTT 2.85", ""], ctx) == [True, True, False, False]


def test_document_qa_with_fake_llm():
    llm = FakeLLM(Answer(found=True, answer="发票号是 ORFR1618731", evidence=["INVOICE ORFR1618731", "编造的原文"]))
    qa = DocumentQA([ocr_doc("DEBIT INVOICE ORFR1618731 {braces}")], llm)
    r = qa.ask("发票号？")
    assert r.answer.answer == "发票号是 ORFR1618731"
    assert r.evidence_ok == [True, False]               # 编造的证据被识别出来
    system, human = llm.prompts[0]
    assert "ORFR1618731 {braces}" in system.content and human.content == "发票号？"


def test_document_qa_requires_content():
    pending = RawDocument(source_id="p", source_path="scan.pdf", file_type=FileType.PDF,
                          status=DocStatus.PENDING_OCR, file_ref="x")
    with pytest.raises(ValueError, match="OCR"):
        DocumentQA([pending], FakeLLM(Answer(found=False, answer="")))


def _result(found, answer, evidence=(), ok=()):
    from bill_agent.qa import QAResult
    return QAResult("q", Answer(found=found, answer=answer, evidence=list(evidence)), list(ok))


def test_score():
    item = {"id": "x", "q": "q", "expect": ["1,410.40", "usd"]}
    assert score(item, _result(True, "总金额 USD 1410.40", ["1,410.40"], [True]))[0]
    assert "缺少" in score(item, _result(True, "USD 1,410"))[1]
    assert "找不到" in score(item, _result(True, "USD 1,410.40", ["x"], [False]))[1]
    assert "没有答案" in score(item, _result(False, "文件中没有该信息"))[1]
    trap = {"id": "t", "q": "税额？", "expect_found": False}
    assert score(trap, _result(False, "文件中没有该信息"))[0]
    assert not score(trap, _result(True, "税额 0"))[0]


def test_qa_sets_are_well_formed():
    sets = sorted(QA_DIR.glob("*.qa.json"))
    assert len(sets) == 4
    ids = []
    for f in sets:
        s = json.loads(f.read_text(encoding="utf-8"))
        assert s["file"] and s["questions"]
        for item in s["questions"]:
            assert item["q"] and (item.get("expect") or item.get("expect_found") is False)
            ids.append(item["id"])
    assert len(ids) == len(set(ids))


# ---------- OCR 缓存 ----------

class CountingBackend:
    name = "local-ocr"

    def __init__(self):
        self.calls = 0

    def recognize(self, doc, data):
        self.calls += 1
        return OCRResult(text="WHARFAGE 21.90", tables=[[["Item", "Amount"], ["WHARFAGE", "21.90"]]],
                         confidence=0.9, warnings=["低置信度文本 'x'（0.5）"], engine="paddleocr/PP-StructureV3")


def test_cached_ocr(tmp_path):
    doc = RawDocument(source_id="sha1", source_path="a.png", file_type=FileType.IMAGE,
                      status=DocStatus.PENDING_OCR, file_ref="x")
    backend = CountingBackend()
    cached = CachedOCR(backend, tmp_path)
    first = cached.recognize(doc, b"img")
    second = CachedOCR(backend, tmp_path).recognize(doc, b"img")
    assert backend.calls == 1
    assert second == first and second.engine == "paddleocr/PP-StructureV3"
    assert (tmp_path / "sha1.local-ocr.json").exists()
