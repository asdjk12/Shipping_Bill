"""PDF 文字层读取（pdfplumber）。

系统生成的 PDF（wkhtmltopdf、Oracle Reports 等）自带文字层，直接读取与原文完全一致，
不存在 0/O、1/l 之类的识别误差，也比 OCR 快得多。只有没有可用文字层的页面才交给 OCR。

- 文本用 layout 模式，保留列对齐（费用表的列关系靠它体现），再去掉行尾空白、公共缩进和多余空行
- 不使用 pdfplumber 的表格提取：在样例 DN 上它把三行费用挤进一个单元格、丢了 DESCRIPTION 列，
  错误的表格比没有表格更危险；费用表交给 LLM 从 layout 文本中读取
- 文字层存在但字体缺少 Unicode 映射（出现 "(cid:12)" 或替换字符）时视为不可用，改走 OCR
"""

from __future__ import annotations

import io
import re
import textwrap
from dataclasses import dataclass

MIN_TEXT_CHARS = 20          # 非空白字符少于此数视为没有文字层（扫描件常只有页码之类的零星文字）
MAX_BAD_CHAR_RATIO = 0.05    # 无法映射的字符占比超过此值视为文字层不可用

_CID = re.compile(r"\(cid:\d+\)")


@dataclass
class PdfPage:
    number: int              # 从 1 开始
    text: str
    usable: bool             # 文字层是否可用
    reason: str | None = None  # 不可用的原因


def clean_layout_text(text: str) -> str:
    lines = [l.rstrip() for l in text.splitlines()]
    text = textwrap.dedent("\n".join(lines))
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n")


def check_text_layer(text: str, min_chars: int = MIN_TEXT_CHARS) -> str | None:
    """返回文字层不可用的原因；可用返回 None。"""
    n = len(re.sub(r"\s", "", text))
    if n < min_chars:
        return f"文字层只有 {n} 个字符"
    bad = len(_CID.findall(text)) + text.count("�")
    if bad / n > MAX_BAD_CHAR_RATIO:
        return f"文字层有 {bad} 个无法映射的字符"
    return None


def read_pdf_pages(data: bytes, min_chars: int = MIN_TEXT_CHARS) -> list[PdfPage]:
    import pdfplumber

    pages: list[PdfPage] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for p in pdf.pages:
            text = clean_layout_text(p.extract_text(layout=True) or "")
            reason = check_text_layer(text, min_chars)
            pages.append(PdfPage(p.page_number, text if reason is None else "", reason is None, reason))
    return pages


def render_pages(data: bytes, numbers: list[int], dpi: int = 200) -> dict:
    """把指定页渲染成 RGB PIL 图片，供 OCR 使用。"""
    import pdfplumber

    out = {}
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for n in numbers:
            out[n] = pdf.pages[n - 1].to_image(resolution=dpi).original.convert("RGB")
    return out
