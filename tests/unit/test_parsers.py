import json
from pathlib import Path

import pytest

from bill_agent.models import DocStatus, FileType
from bill_agent.parsers import DetectedType, InputFile, RouterConfig, build_default_router, detect
from bill_agent.parsers.convert import convert_with_soffice, soffice_path

from .conftest import DN_PDF, SAMPLES, TINY_PNG

needs_soffice = pytest.mark.skipif(soffice_path() is None, reason="需要 LibreOffice")
needs_samples = pytest.mark.skipif(
    not (SAMPLES / "DN_DC-108478.pdf").exists() or not (SAMPLES / "INV_ZIM_ORFR1618731.png").exists(),
    reason="样例原件未入库（含银行信息），放到 tests/fixtures/samples/ 后运行",
)


@pytest.fixture
def router(tmp_path):
    return build_default_router(RouterConfig(blob_dir=str(tmp_path / "blobs")))


def route_bytes(router, data: bytes, name: str):
    return router.route(InputFile(data=data, name=name, source_path=name))


# ---------- 类型识别 ----------

def test_detect_by_magic(xlsx_bytes, docx_bytes, eml_bytes):
    assert detect(DN_PDF, "whatever.dat") is DetectedType.PDF
    assert detect(TINY_PNG, "x.pdf") is DetectedType.IMAGE
    assert detect(xlsx_bytes, "noext") is DetectedType.XLSX
    assert detect(docx_bytes, "a.xlsx") is DetectedType.DOCX
    assert detect(eml_bytes, "mail") is DetectedType.EML
    assert detect(b'[{"reference_no": "A"}]', "x") is DetectedType.BILL_JSON
    assert detect(b"hello", "notes.txt") is DetectedType.UNKNOWN


# ---------- Excel ----------

def test_excel_header_and_rows(router, xlsx_bytes):
    r = route_bytes(router, xlsx_bytes, "charges.xlsx")
    (doc,) = r.documents
    assert doc.file_type is FileType.EXCEL
    (t,) = doc.tables                       # 隐藏工作表被跳过
    assert t.name == "Debit Note"
    assert t.header == ["Description", "Qty", "Rate", "Amount", "Currency"]
    assert t.rows[0] == ["Handling Fee", "1", "40", "40", "USD"]
    assert t.rows[1] == ["THC", "2", "125.5", "251", "USD"]
    assert "Invoice No: INV-2026-001" in doc.text   # 表头以上的信息保留在 text
    assert "should not appear" not in doc.text
    assert any("隐藏" in w for w in doc.warnings)


def test_excel_merged_cells_expanded(router, xlsx_bytes):
    (doc,) = route_bytes(router, xlsx_bytes, "c.xlsx").documents
    first_line = doc.text.splitlines()[1]
    assert first_line.count("ACME LOGISTICS DEBIT NOTE") == 5


@needs_soffice
def test_xls_legacy(router, xlsx_bytes):
    xls = convert_with_soffice(xlsx_bytes, "xlsx", "xls")
    assert xls and detect(xls, "old.xls") is DetectedType.XLS
    (doc,) = route_bytes(router, xls, "old.xls").documents
    assert doc.tables[0].header == ["Description", "Qty", "Rate", "Amount", "Currency"]
    assert doc.tables[0].rows[0][0] == "Handling Fee"


def test_corrupt_xlsx_is_skipped_not_crash(router):
    r = route_bytes(router, b"PK\x03\x04" + b"garbage" * 50, "broken.xlsx")
    assert not r.documents and len(r.skipped) == 1


# ---------- Word ----------

def test_word_body_table_header_footer(router, docx_bytes):
    (doc,) = route_bytes(router, docx_bytes, "dn.docx").documents
    assert doc.file_type is FileType.WORD
    assert doc.text.index("BETA SHIPPING") < doc.text.index("Debit Note No: DN-2026-088")
    assert doc.text.index("Debit Note No") < doc.text.index("Doc Fee") < doc.text.index("Please remit")
    assert "Bank details on request" in doc.text
    (t,) = doc.tables
    assert t.header == ["Charge", "Amount", "Curr", "P/C"]
    assert t.rows == [["Doc Fee", "50.00", "USD", "PP"], ["Storage Fee", "1,200.00", "CNY", "CC"]]


@needs_soffice
def test_doc_legacy(router, docx_bytes):
    doc_bytes = convert_with_soffice(docx_bytes, "docx", "doc")
    assert doc_bytes and detect(doc_bytes, "old.doc") is DetectedType.DOC
    (doc,) = route_bytes(router, doc_bytes, "old.doc").documents
    assert "DN-2026-088" in doc.text
    assert doc.tables[0].rows[0][0] == "Doc Fee"


# ---------- EML ----------

def test_eml_full(router, eml_bytes):
    r = route_bytes(router, eml_bytes, "update.eml")
    by_path = {d.source_path: d for d in r.documents}

    mail = by_path["update.eml"]
    assert mail.file_type is FileType.EML and mail.status is DocStatus.PARSED
    assert mail.file_name == "update.eml" and mail.mime_type == "message/rfc822"
    assert mail.email.subject == "账单更新 DC-108478"
    assert mail.email.sender == "Neptune LAX <billing@neptune.com>"
    assert mail.email.recipients == ["Pilot Ops <ops@pilot.com>", "finance@pilot.com"]
    assert mail.email.cc == ["boss@pilot.com"]
    assert "请将 Handling Fee 改为 USD 40" in mail.text
    assert mail.tables[0].header == ["Charge", "Amount", "Currency"]       # HTML 正文中的表格
    assert mail.tables[0].rows == [["Handling Fee", "40.00", "USD"]]

    # 附件递归解析，parent_id 指向邮件
    xl = by_path["update.eml#attachments/charges.xlsx"]
    assert xl.file_type is FileType.EXCEL and xl.parent_id == mail.source_id

    # PDF 附件（扩展名错误）：只登记，等待 OCR
    pdf = by_path["update.eml#attachments/DN_DC-108478.dat"]
    assert pdf.file_type is FileType.PDF and pdf.status is DocStatus.PENDING_OCR
    assert pdf.mime_type == "application/pdf" and pdf.file_name == "DN_DC-108478.dat"
    assert pdf.text == "" and pdf.file_ref.endswith(".pdf")
    assert Path(pdf.file_ref).read_bytes() == DN_PDF
    assert not pdf.ready_for_extraction

    # 转发邮件及其附件
    inner = next(d for p, d in by_path.items() if p.endswith(".eml") and p != "update.eml")
    assert inner.email.subject == "原始账单" and inner.parent_id == mail.source_id
    inner_docx = by_path[f"{inner.source_path}#attachments/DN-2026-088.docx"]
    assert inner_docx.parent_id == inner.source_id

    skipped = {s.source_path: s.reason for s in r.skipped}
    assert "已去重" in skipped["update.eml#attachments/charges_copy.xlsx"]
    assert "不支持" in skipped["update.eml#attachments/notes.txt"]
    assert any("内嵌小图片" in reason for reason in skipped.values())


def test_eml_bad_charset_does_not_fail(router):
    raw = (b"From: a@b.com\r\nTo: c@d.com\r\nSubject: test\r\nMIME-Version: 1.0\r\n"
           b"Content-Type: text/plain; charset=x-unknown-cs\r\n\r\nHello \xe4\xbd\xa0\xe5\xa5\xbd\r\n")
    (doc,) = route_bytes(router, raw, "bad.eml").documents
    assert "Hello" in doc.text


# ---------- PDF / 图片：登记为待 OCR ----------

def test_pdf_registered_pending_ocr(router):
    (doc,) = route_bytes(router, DN_PDF, "DN.pdf").documents
    assert doc.status is DocStatus.PENDING_OCR and doc.file_type is FileType.PDF
    assert doc.file_size == len(DN_PDF) and doc.text == "" and doc.tables == []
    assert Path(doc.file_ref).read_bytes() == DN_PDF


def test_image_with_wrong_extension(router):
    (doc,) = route_bytes(router, TINY_PNG, "scan.pdf").documents
    assert doc.file_type is FileType.IMAGE and doc.mime_type == "image/png"
    assert doc.file_ref.endswith(".png")


@needs_samples
def test_real_samples_registered(router):
    for name in ("DN_DC-108478.pdf", "INV_ZIM_ORFR1618731.png"):
        (doc,) = route_bytes(router, (SAMPLES / name).read_bytes(), name).documents
        assert doc.status is DocStatus.PENDING_OCR


# ---------- 账单 JSON ----------

@pytest.mark.parametrize("payload, n", [
    ([{"reference_no": "A"}, {"reference_no": "B"}], 2),
    ({"bills": [{"reference_no": "A"}]}, 1),
    ({"reference_no": "A"}, 1),
])
def test_bill_json_shapes(router, payload, n):
    r = route_bytes(router, json.dumps(payload).encode(), "existing.json")
    assert len(r.bill_jsons[0].records) == n and not r.documents


def test_bill_json_invalid_is_skipped(router):
    r = route_bytes(router, b"[1, 2, 3]", "existing.json")
    assert not r.bill_jsons and "对象数组" in r.skipped[0].reason


# ---------- 目录批量 ----------

def test_route_directory(tmp_path, router, xlsx_bytes, docx_bytes):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "a.xlsx").write_bytes(xlsx_bytes)
    sub = tmp_path / "in" / "sub"
    sub.mkdir()
    (sub / "b.docx").write_bytes(docx_bytes)
    (sub / ".DS_Store").write_bytes(b"junk")
    (tmp_path / "in" / "empty.pdf").write_bytes(b"")
    r = router.route_paths([tmp_path / "in"])
    assert sorted(Path(d.source_path).name for d in r.documents) == ["a.xlsx", "b.docx"]
    assert [Path(s.source_path).name for s in r.skipped] == ["empty.pdf"]


def test_key_value_row_is_not_header():
    from bill_agent.parsers.table_utils import find_header
    rows = [["TOTAL AMOUNT", "1,410.40", "CURRENCY", "USD"],
            ["DESCRIPTION", "RATE", "QTY", "AMOUNT"],
            ["HANDLING CHARGE", "65.00", "1.00", "65.00"]]
    assert find_header(rows) == 1
