"""RawDocument 状态规则与 OCR 接入接口。"""

import pytest
from pydantic import ValidationError

from bill_agent.models import DocStatus, FileType, RawDocument, RawTable
from bill_agent.ocr import OCRResult, run_ocr
from bill_agent.parsers import RouterConfig, build_default_router

from .conftest import DN_PDF


def pending(tmp_path) -> RawDocument:
    ref = tmp_path / "x.pdf"
    ref.write_bytes(DN_PDF)
    return RawDocument(source_id="x", source_path="x.pdf", file_type=FileType.PDF,
                       status=DocStatus.PENDING_OCR, file_ref=str(ref))


# ---------- 状态与字段一致性 ----------

def test_pdf_cannot_be_parsed():
    with pytest.raises(ValidationError, match="PENDING_OCR"):
        RawDocument(source_id="x", source_path="x.pdf", file_type=FileType.PDF, text="abc")


def test_excel_cannot_be_pending():
    with pytest.raises(ValidationError, match="只能是 PARSED"):
        RawDocument(source_id="x", source_path="x.xlsx", file_type=FileType.EXCEL,
                    status=DocStatus.PENDING_OCR, file_ref="/tmp/x")


def test_pending_requires_file_ref_and_empty_content():
    with pytest.raises(ValidationError, match="file_ref"):
        RawDocument(source_id="x", source_path="x.pdf", file_type=FileType.PDF, status=DocStatus.PENDING_OCR)
    with pytest.raises(ValidationError, match="必须为空"):
        RawDocument(source_id="x", source_path="x.pdf", file_type=FileType.PDF,
                    status=DocStatus.PENDING_OCR, file_ref="/tmp/x", text="early")


# ---------- 状态流转 ----------

def test_complete_ocr(tmp_path):
    doc = pending(tmp_path)
    done = doc.complete_ocr(text="DEBIT NOTE DC-108478", engine="vision-x", confidence=0.92,
                            tables=[RawTable(header=["DESCRIPTION"], rows=[["HANDLING CHARGE"]])])
    assert done.status is DocStatus.OCR_DONE and done.ready_for_extraction
    assert done.ocr_engine == "vision-x" and done.ocr_completed_at is not None
    assert done.file_ref == doc.file_ref                     # 原文件引用保留，便于审计
    assert doc.status is DocStatus.PENDING_OCR               # 原对象不变


def test_complete_ocr_rejects_empty_result(tmp_path):
    with pytest.raises(ValidationError, match="不能都为空"):
        pending(tmp_path).complete_ocr(text="  ", engine="vision-x")


def test_complete_ocr_rejects_bad_confidence(tmp_path):
    with pytest.raises(ValidationError):
        pending(tmp_path).complete_ocr(text="abc", engine="vision-x", confidence=1.5)


def test_fail_then_retry(tmp_path):
    failed = pending(tmp_path).fail_ocr("timeout", engine="vision-x")
    assert failed.status is DocStatus.OCR_FAILED and not failed.ready_for_extraction
    done = failed.complete_ocr(text="abc", engine="vision-x")
    assert done.status is DocStatus.OCR_DONE and done.ocr_error is None


def test_parsed_doc_cannot_take_ocr():
    doc = RawDocument(source_id="x", source_path="x.xlsx", file_type=FileType.EXCEL, text="abc")
    with pytest.raises(ValueError, match="不能写入"):
        doc.complete_ocr(text="abc", engine="vision-x")


def test_json_roundtrip(tmp_path):
    doc = pending(tmp_path)
    assert RawDocument.model_validate_json(doc.model_dump_json()) == doc


# ---------- run_ocr 接口 ----------

class FakeVision:
    name = "fake-vision"

    def __init__(self, fail_on: set[str] = frozenset()):
        self.fail_on = fail_on
        self.seen: list[tuple[str, int]] = []

    def recognize(self, doc, data):
        self.seen.append((doc.file_name, len(data)))
        if doc.file_name in self.fail_on:
            raise TimeoutError("model timeout")
        return OCRResult(
            text=f"OCR of {doc.file_name}",
            tables=[[["D/C No.", "DC-108478"], ["DESCRIPTION", "RATE", "QTY", "AMOUNT"], ["HANDLING CHARGE", "65.00", "1.00", "65.00"]]],
            confidence=0.88,
        )


def test_run_ocr_end_to_end(tmp_path, eml_bytes, xlsx_bytes):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "mail.eml").write_bytes(eml_bytes)
    (tmp_path / "in" / "b.pdf").write_bytes(DN_PDF + b"\n% other")   # 另一个 PDF
    router = build_default_router(RouterConfig(blob_dir=str(tmp_path / "blobs")))
    parsed = router.route_paths([tmp_path / "in"]).documents

    backend = FakeVision(fail_on={"b.pdf"})
    docs = run_ocr(parsed, backend)

    assert len(docs) == len(parsed)
    assert [d.source_path for d in docs] == [d.source_path for d in parsed]   # 顺序不变
    by_name = {d.file_name: d for d in docs}
    ok = by_name["DN_DC-108478.dat"]
    assert ok.status is DocStatus.OCR_DONE and ok.ocr_engine == "fake-vision" and ok.ocr_confidence == 0.88
    assert ok.tables[0].header == ["DESCRIPTION", "RATE", "QTY", "AMOUNT"]   # 表头由统一逻辑定位
    assert ok.tables[0].rows == [["HANDLING CHARGE", "65.00", "1.00", "65.00"]]
    bad = by_name["b.pdf"]
    assert bad.status is DocStatus.OCR_FAILED and "model timeout" in bad.ocr_error
    # 非 OCR 文档原样通过，后端只收到 PDF/图片
    assert {n for n, _ in backend.seen} == {"DN_DC-108478.dat", "b.pdf"}
    assert by_name["charges.xlsx"] is next(d for d in parsed if d.file_name == "charges.xlsx")
