"""EML 解析（四.1：解析主题、发件人、收件人、正文及附件；附件按实际格式递归解析）。

输出：
- 一个 file_type=EML 的 RawDocument：邮件头 + 正文（正文里可能有改账指令或 HTML 费用表）
- 每个附件重新送回 Router，按真实格式解析，parent_id 指向这封邮件
- 附件里的 .eml / message/rfc822（转发邮件）同样递归
"""

from __future__ import annotations

import mimetypes
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from bill_agent.models import EmailMeta, FileType, RawDocument

from .base import InputFile, ParseError, ParseResult, SkippedItem
from .html_text import html_to_text_and_tables
from .table_utils import build_table


def _decode_text(part: EmailMessage) -> str:
    """取文本内容；遇到未知或错误的 charset 时逐级降级，不让整封邮件失败。"""
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, AssertionError):
        pass
    raw = part.get_payload(decode=True) or b""
    charset = (part.get_content_charset() or "utf-8").lower()
    if charset in ("gb2312", "gbk"):
        charset = "gb18030"
    for cs in (charset, "utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _addresses(msg: EmailMessage, header: str) -> list[str]:
    values = msg.get_all(header, [])
    out = []
    for name, addr in getaddresses([str(v) for v in values]):
        if addr or name:
            out.append(f"{name} <{addr}>" if name and addr else (addr or name))
    return out


def _meta(msg: EmailMessage) -> EmailMeta:
    sent_at = None
    if msg["Date"]:
        try:
            sent_at = parsedate_to_datetime(str(msg["Date"]))
        except (TypeError, ValueError):
            sent_at = None
    sender = _addresses(msg, "From")
    return EmailMeta(
        subject=str(msg["Subject"]) if msg["Subject"] is not None else None,
        sender=sender[0] if sender else None,
        recipients=_addresses(msg, "To"),
        cc=_addresses(msg, "Cc"),
        sent_at=sent_at,
        message_id=str(msg["Message-ID"]).strip() if msg["Message-ID"] else None,
    )


def _is_attachment(part: EmailMessage) -> bool:
    if part.get_content_disposition() == "attachment" or part.get_filename():
        return True
    return part.get_content_maintype() not in ("text", "multipart")


def _walk(part: EmailMessage, bodies: list[EmailMessage], attachments: list[EmailMessage]) -> None:
    """自定义遍历：不进入 message/rfc822 内部（它作为附件整体递归），区分正文与附件。"""
    ctype = part.get_content_type()
    if ctype == "message/rfc822":
        attachments.append(part)
    elif part.is_multipart():
        for sub in part.iter_parts():
            _walk(sub, bodies, attachments)
    elif _is_attachment(part):
        attachments.append(part)
    elif ctype in ("text/plain", "text/html"):
        bodies.append(part)


def _attachment_bytes(part: EmailMessage) -> bytes:
    if part.get_content_type() == "message/rfc822":
        inner = part.get_payload()
        inner = inner[0] if isinstance(inner, list) else inner
        return inner.as_bytes(policy=policy.default)
    return part.get_payload(decode=True) or b""


def _attachment_name(part: EmailMessage, index: int, used: set[str]) -> str:
    name = part.get_filename()
    if not name:
        ctype = part.get_content_type()
        ext = ".eml" if ctype == "message/rfc822" else (mimetypes.guess_extension(ctype) or "")
        name = f"attachment-{index}{ext}"
    name = name.replace("/", "_").replace("\\", "_")
    base, n = name, 1
    while name in used:
        n += 1
        name = f"{n}_{base}"
    used.add(name)
    return name


class EmlParser:
    def parse(self, f: InputFile, router) -> ParseResult:
        try:
            msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(f.data)
        except Exception as e:
            raise ParseError(f"无法解析邮件: {e}") from e
        if not msg.keys():
            raise ParseError("不是有效的邮件：没有邮件头")

        warnings: list[str] = []
        meta = _meta(msg)
        bodies: list[EmailMessage] = []
        attachments: list[EmailMessage] = []
        _walk(msg, bodies, attachments)

        # 正文：text/plain 优先；表格从 text/html 里提取（plain 版本会丢失表格结构）
        plains = [_decode_text(p) for p in bodies if p.get_content_type() == "text/plain"]
        htmls = [_decode_text(p) for p in bodies if p.get_content_type() == "text/html"]
        html_texts, html_grids = [], []
        for h in htmls:
            t, grids = html_to_text_and_tables(h)
            html_texts.append(t)
            html_grids.extend(grids)
        body_text = "\n\n".join(x.strip() for x in plains if x.strip()) or "\n\n".join(html_texts)
        tables = [build_table(f"html-table-{i + 1}", g) for i, g in enumerate(html_grids)]
        tables = [t for t in tables if t.rows or t.header]

        # 附件
        result = ParseResult()
        used: set[str] = set()
        att_names: list[str] = []
        children: list[InputFile] = []
        for i, part in enumerate(attachments, start=1):
            name = _attachment_name(part, i, used)
            path = f"{f.source_path}#attachments/{name}"
            data = _attachment_bytes(part)
            att_names.append(name)
            limit = router.config.skip_inline_images_below
            if (
                part.get_content_maintype() == "image"
                and part.get_content_disposition() != "attachment"
                and part["Content-ID"]
                and len(data) < limit
            ):
                result.skipped.append(SkippedItem(path, f"正文内嵌小图片（<{limit // 1024}KB，通常是 logo/签名），不送 OCR"))
                continue
            children.append(InputFile(
                data=data, name=name, source_path=path, parent_id=f.source_id,
                depth=f.depth + 1, content_type=part.get_content_type(),
            ))

        header_lines = [
            f"Subject: {meta.subject or ''}",
            f"From: {meta.sender or ''}",
            f"To: {', '.join(meta.recipients)}",
        ]
        if meta.cc:
            header_lines.append(f"Cc: {', '.join(meta.cc)}")
        if msg["Date"]:
            header_lines.append(f"Date: {msg['Date']}")
        if att_names:
            header_lines.append(f"Attachments: {', '.join(att_names)}")
        if not body_text.strip():
            warnings.append("邮件正文为空")

        result.documents.append(RawDocument(
            source_id=f.source_id,
            source_path=f.source_path,
            parent_id=f.parent_id,
            file_type=FileType.EML,
            text="\n".join(header_lines) + "\n\n" + body_text.strip(),
            tables=tables,
            email=meta,
            warnings=warnings,
        ))
        for child in children:
            result.extend(router.route(child))
        return result
