"""把解析 / OCR 结果拼成给 LLM 的上下文，以及回到原文核验摘录。问答和抽取共用。"""

from __future__ import annotations

import re

from bill_agent.models import RawDocument


def document_context(doc: RawDocument, index: int) -> str:
    meta = [f"类型 {doc.file_type.value}"]
    if doc.parent_id:
        meta.append("邮件附件")
    if doc.ocr_engine:
        meta.append(f"识别方式 {doc.ocr_engine}")
    if doc.ocr_confidence is not None:
        meta.append(f"识别置信度 {doc.ocr_confidence:.2f}")
    parts = [f"===== 文档 {index}：{doc.source_path}（{'，'.join(meta)}）=====", doc.text]
    low = [w for w in doc.warnings if "低置信度" in w]
    if low:
        parts.append("[识别提示] 以下文本识别置信度低，可能有误：\n" + "\n".join(low))
    return "\n".join(parts)


def build_context(docs: list[RawDocument]) -> str:
    ready = [d for d in docs if d.ready_for_extraction and d.text.strip()]
    return "\n\n".join(document_context(d, i) for i, d in enumerate(ready, start=1))


def squash(s: str) -> str:
    """去掉所有空白并转小写：layout 文本里的多余空格不影响比对。"""
    return re.sub(r"\s+", "", s).lower()


def verify_evidence(evidence: list[str], context: str) -> list[bool]:
    """忽略空白和大小写后，摘录必须是上下文的子串。"""
    ctx = squash(context)
    return [bool(squash(e)) and squash(e) in ctx for e in evidence]
