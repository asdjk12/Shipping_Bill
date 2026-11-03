"""本地 OCR：PaddleOCR PP-StructureV3（版面分析 + PP-OCRv5 文字识别 + 表格结构识别）。

- 输出按阅读顺序拼成文本；表格由 HTML（含 colspan / rowspan）转成二维数组，
  合并单元格重复填值，与 Excel / Word 的处理一致
- 置信度取全部文本行识别分数的平均值；低于 low_conf 的行逐条写入 warnings，
  每行文字、分数、坐标都保存在 raw 里，供校验环节核对具体字段
- 模型在第一次识别时才加载（约几秒，首次运行还要下载模型到 ~/.paddlex），
  只含文字层 PDF 的批次不会加载 Paddle
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from bill_agent.parsers.table_utils import format_cell, grid_to_text

LOW_CONF = 0.8


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s)


@dataclass
class PageOCR:
    text: str
    tables: list[list[list[str | None]]] = field(default_factory=list)
    confidence: float | None = None
    low_conf_lines: list[tuple[str, float]] = field(default_factory=list)
    raw: dict | None = None


class _TableHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[tuple[str, int, int]]] = []
        self._cell: list[str] | None = None
        self._span = (1, 1)

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            a = dict(attrs)
            self._cell = []
            self._span = (int(a.get("rowspan") or 1), int(a.get("colspan") or 1))

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            if not self.rows:
                self.rows.append([])
            self.rows[-1].append(("".join(self._cell), *self._span))
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def html_table_to_grid(html: str) -> list[list[str | None]]:
    """HTML 表格转二维数组；rowspan / colspan 展开后重复填值。"""
    p = _TableHTML()
    p.feed(html)
    grid: list[list[str | None]] = []
    filled: dict[tuple[int, int], str | None] = {}   # 被上方 rowspan 占用的位置
    for r, cells in enumerate(p.rows):
        row: list[str | None] = []
        c = 0
        for text, rowspan, colspan in cells:
            while (r, c) in filled:
                row.append(filled.pop((r, c)))
                c += 1
            v = format_cell(text)
            for dc in range(colspan):
                row.append(v)
                for dr in range(1, rowspan):
                    filled[(r + dr, c + dc)] = v
            c += colspan
        while (r, c) in filled:
            row.append(filled.pop((r, c)))
            c += 1
        grid.append(row)
    width = max((len(r) for r in grid), default=0)
    return [r + [None] * (width - len(r)) for r in grid]


class PaddleStructure:
    """PP-StructureV3 封装。device: "cpu" / "gpu"（gpu 需安装 paddlepaddle-gpu）。"""

    name = "paddleocr/PP-StructureV3"

    def __init__(self, device: str = "cpu", low_conf: float = LOW_CONF):
        self.device = device
        self.low_conf = low_conf
        self._pipeline = None

    def _load(self):
        if self._pipeline is None:
            # 跳过启动时对模型下载源的联网检查；模型已缓存时可离线运行
            os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
            from paddleocr import PPStructureV3

            self._pipeline = PPStructureV3(
                device=self.device,
                # Paddle 3.3 CPU 版开启 oneDNN 时版面模型报 ConvertPirAttribute2RuntimeAttribute 错误
                enable_mkldnn=False,
                # 账单是正向的电子件或扫描件，不需要文档矫正；公式 / 印章 / 图表识别用不到
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                use_formula_recognition=False,
                use_seal_recognition=False,
                use_chart_recognition=False,
            )
        return self._pipeline

    def recognize_image(self, image) -> PageOCR:
        """image: RGB PIL 图片。"""
        import numpy as np

        bgr = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()
        res = self._load().predict(bgr)[0].json["res"]
        return self.to_page(res)

    def to_page(self, res: dict) -> PageOCR:
        """把 PP-StructureV3 的单页结果（res.json["res"]）转成 PageOCR。"""
        parts: list[str] = []
        tables: list[list[list[str | None]]] = []
        for block in res.get("parsing_res_list", []):
            content = (block.get("block_content") or "").strip()
            if not content:
                continue
            if block.get("block_label") == "table":
                grid = html_table_to_grid(content)
                tables.append(grid)
                parts.append(grid_to_text(grid))
            else:
                parts.append(content)

        ocr = res.get("overall_ocr_res") or {}
        texts, scores = ocr.get("rec_texts", []), [float(s) for s in ocr.get("rec_scores", [])]

        # 版面分析可能漏掉部分文本行（ZIM 样例的箱号 JXLU4504463 不在任何版面块里），
        # 识别到但没有进入版面块的行补到末尾，不丢内容
        covered = _squash("\n".join(parts))
        orphans = [t for t in texts if _squash(t) and _squash(t) not in covered]
        if orphans:
            parts.append("[未归入版面块的识别文本]\n" + "\n".join(orphans))
        boxes = ocr.get("rec_boxes", [])
        lines = [
            {"text": t, "score": round(s, 4), "box": [int(v) for v in b] if len(b) else None}
            for t, s, b in zip(texts, scores, boxes)
        ]
        return PageOCR(
            text="\n\n".join(parts),
            tables=tables,
            confidence=round(sum(scores) / len(scores), 4) if scores else None,
            low_conf_lines=[(t, s) for t, s in zip(texts, scores) if s < self.low_conf],
            raw={"lines": lines},
        )
