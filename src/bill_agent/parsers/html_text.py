"""HTML 转文本 + 表格（用于 HTML 格式的邮件正文，费用表经常以 <table> 形式出现在正文里）。"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article", "hr"}
_SKIP = {"script", "style", "head", "title"}


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.tables: list[list[list[str | None]]] = []
        self._stack: list[list[list[str | None]]] = []   # 正在构建的表格（支持嵌套）
        self._cell: list[str] | None = None
        self._colspan = 1
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
            return
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._stack[-1].append([])
        elif tag in ("td", "th") and self._stack:
            self._cell = []
            try:
                self._colspan = max(1, int(dict(attrs).get("colspan") or 1))
            except ValueError:
                self._colspan = 1
        elif tag in _BLOCK:
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag in ("td", "th") and self._stack and self._cell is not None:
            value = re.sub(r"\s+", " ", "".join(self._cell)).strip() or None
            if not self._stack[-1]:
                self._stack[-1].append([])
            self._stack[-1][-1].extend([value] * self._colspan)
            self._emit((value or "") + " | ")
            self._cell = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())
            self._emit("\n")
        elif tag in _BLOCK:
            self._emit("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)
        else:
            self._emit(data)

    def _emit(self, s: str) -> None:
        self.parts.append(s)


def html_to_text_and_tables(html: str) -> tuple[str, list[list[list[str | None]]]]:
    p = _Extractor()
    p.feed(html)
    p.close()
    text = "".join(p.parts)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" ?\| *\n", "\n", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text, p.tables
