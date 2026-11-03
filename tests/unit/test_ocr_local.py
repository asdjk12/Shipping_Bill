"""本地 OCR：PDF 文字层、PaddleOCR 结果转换、按页组合。

PaddleOCR 用假实现替代；真实模型的测试较慢（CPU 上每张图约一分钟），
设置环境变量 RUN_PADDLE=1 且样例原件存在时才运行。
"""

import io
import os

import pytest
from PIL import Image

from bill_agent.models import DocStatus, FileType, RawDocument
from bill_agent.ocr import LocalOCR, PaddleStructure, run_ocr
from bill_agent.ocr import local
from bill_agent.ocr.paddle import PageOCR, html_table_to_grid
from bill_agent.ocr.pdf_text import PdfPage, check_text_layer, clean_layout_text, read_pdf_pages
from bill_agent.parsers import RouterConfig, build_default_router

from .conftest import DN_PDF, SAMPLES, make_text_pdf

BLANK_PDF = make_text_pdf([])


class FakeImageOCR:
    name = "fake-paddle"

    def __init__(self):
        self.calls = 0

    def recognize_image(self, image) -> PageOCR:
        self.calls += 1
        return PageOCR(
            text="WHARFAGE 21.90 USD",
            tables=[[["Description", "Curr", "Amount"], ["WHARFAGE", "USD", "21.90"]]],
            confidence=0.9,
            low_conf_lines=[("Zin Amer1can", 0.62)],
            raw={"lines": [{"text": "WHARFAGE", "score": 0.99, "box": [0, 0, 10, 10]}]},
        )


def pending(file_type: FileType, tmp_path) -> RawDocument:
    return RawDocument(source_id="x", source_path="x", file_type=file_type,
                       status=DocStatus.PENDING_OCR, file_ref=str(tmp_path / "x"))


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, format="PNG")
    return buf.getvalue()


# ---------- PDF 文字层 ----------

def test_clean_layout_text():
    raw = "        \n      DEBIT NOTE   \n\n\n\n      D/C No. : DC-1\n          HANDLING  65.00\n"
    assert clean_layout_text(raw) == "DEBIT NOTE\n\nD/C No. : DC-1\n    HANDLING  65.00"


def test_check_text_layer():
    assert check_text_layer("DEBIT NOTE D/C No. DC-108478 HANDLING CHARGE") is None
    assert "字符" in check_text_layer("  1  ")
    assert "无法映射" in check_text_layer("(cid:12)(cid:13)(cid:14) " * 3 + "x" * 30)


def test_read_pdf_pages():
    [p] = read_pdf_pages(DN_PDF)
    assert p.usable and p.number == 1
    assert "DC-108478" in p.text and "HANDLING CHARGE" in p.text
    [blank] = read_pdf_pages(BLANK_PDF)
    assert not blank.usable and blank.text == ""


# ---------- PaddleOCR 结果转换 ----------

def test_html_table_colspan_rowspan():
    html = ("<table><tr><td colspan='2'>DEBIT INVOICE:</td><td>ORFR1618731</td></tr>"
            "<tr><td rowspan='2'>WHARFAGE</td><td>C</td><td>5.00</td></tr>"
            "<tr><td>PTT</td><td> 21.90 </td></tr><tr><td></td></tr></table>")
    assert html_table_to_grid(html) == [
        ["DEBIT INVOICE:", "DEBIT INVOICE:", "ORFR1618731"],
        ["WHARFAGE", "C", "5.00"],
        ["WHARFAGE", "PTT", "21.90"],
        [None, None, None],
    ]


def test_paddle_to_page():
    res = {
        "parsing_res_list": [
            {"block_label": "header", "block_content": "ZIM"},
            {"block_label": "image", "block_content": ""},
            {"block_label": "table", "block_content":
                "<table><tr><td>Base</td><td>Amount</td></tr><tr><td>C</td><td>5.00</td></tr></table>"},
        ],
        "overall_ocr_res": {"rec_texts": ["ZIM", "EcreneDoournt", "5.00"],
                            "rec_scores": [0.99, 0.45, 1.0],
                            "rec_boxes": [[0, 0, 1, 1], [1, 1, 2, 2], [2, 2, 3, 3]]},
    }
    page = PaddleStructure(low_conf=0.8).to_page(res)
    # 没有进入任何版面块的识别行补在末尾
    assert page.text == "ZIM\n\nBase | Amount\nC | 5.00\n\n[未归入版面块的识别文本]\nEcreneDoournt"
    assert page.tables == [[["Base", "Amount"], ["C", "5.00"]]]
    assert page.confidence == pytest.approx(0.8133, abs=1e-4)
    assert page.low_conf_lines == [("EcreneDoournt", 0.45)]
    assert page.raw["lines"][1] == {"text": "EcreneDoournt", "score": 0.45, "box": [1, 1, 2, 2]}


# ---------- LocalOCR 组合 ----------

class ExplodingOCR:
    name = "should-not-run"

    def recognize_image(self, image):
        raise AssertionError("文字层 PDF 不应调用 OCR")


def test_text_pdf_uses_text_layer_only(tmp_path):
    r = LocalOCR(ExplodingOCR()).recognize(pending(FileType.PDF, tmp_path), DN_PDF)
    assert r.engine == "pdf-text-layer" and r.confidence == 1.0
    assert "DC-108478" in r.text and r.tables == [] and r.warnings == []


def test_scanned_pdf_falls_back_to_ocr(tmp_path):
    fake = FakeImageOCR()
    r = LocalOCR(fake, dpi=50).recognize(pending(FileType.PDF, tmp_path), BLANK_PDF)
    assert fake.calls == 1 and r.engine == "fake-paddle" and r.confidence == 0.9
    assert r.tables[0].name == "page-1-table-1" and r.tables[0].header == ["Description", "Curr", "Amount"]
    assert any("第 1 页" in w and "fake-paddle" in w for w in r.warnings)
    assert any("Zin Amer1can" in w for w in r.warnings)


def test_mixed_pdf_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(local, "read_pdf_pages", lambda data: [
        PdfPage(1, "DEBIT NOTE DC-1", True),
        PdfPage(2, "", False, "文字层只有 0 个字符"),
    ])
    monkeypatch.setattr(local, "render_pages", lambda data, numbers, dpi: {n: object() for n in numbers})
    fake = FakeImageOCR()
    r = LocalOCR(fake).recognize(pending(FileType.PDF, tmp_path), b"%PDF")
    assert fake.calls == 1
    assert r.engine == "pdf-text-layer+fake-paddle"
    assert r.confidence == 0.9                                   # 取各页最小值
    assert r.text.startswith("--- page 1 ---\nDEBIT NOTE DC-1") and "--- page 2 ---\nWHARFAGE" in r.text
    assert [t.name for t in r.tables] == ["page-2-table-1"]
    assert [p["engine"] for p in r.raw_response["pages"]] == ["pdf-text-layer", "fake-paddle"]


def test_image_and_run_ocr(tmp_path):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "inv.png").write_bytes(png_bytes())
    (tmp_path / "in" / "dn.pdf").write_bytes(DN_PDF)
    router = build_default_router(RouterConfig(blob_dir=str(tmp_path / "blobs")))
    fake = FakeImageOCR()
    docs = {d.file_name: d for d in run_ocr(router.route_paths([tmp_path / "in"]).documents, LocalOCR(fake))}

    img, pdf = docs["inv.png"], docs["dn.pdf"]
    assert img.status is DocStatus.OCR_DONE and img.ocr_engine == "fake-paddle" and img.ocr_confidence == 0.9
    assert img.tables[0].rows == [["WHARFAGE", "USD", "21.90"]]
    assert pdf.status is DocStatus.OCR_DONE and pdf.ocr_engine == "pdf-text-layer"
    assert fake.calls == 1


# ---------- 真实 PaddleOCR（慢） ----------

@pytest.mark.skipif(
    os.environ.get("RUN_PADDLE") != "1" or not (SAMPLES / "INV_ZIM_ORFR1618731.png").exists(),
    reason="设置 RUN_PADDLE=1 并放入样例原件后运行",
)
def test_paddle_on_zim_sample(tmp_path):
    data = (SAMPLES / "INV_ZIM_ORFR1618731.png").read_bytes()
    r = LocalOCR().recognize(pending(FileType.IMAGE, tmp_path), data)
    assert "ORFR1618731" in r.text and r.confidence > 0.9
    charges = next(t for t in r.tables if t.header and "Amount" in t.header)
    assert ["WHARFAGE ADMINISTRATION FEE", "C", "5.00", "USD", "5.00", "1.0000", "USD", "5.00"] in charges.rows
    assert any(row[0] == "WHARFAGE" and row[4] == "21.90" for row in charges.rows)
