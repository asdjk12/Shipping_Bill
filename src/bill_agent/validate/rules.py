"""抽取结果的数据校验（6.5）：纯代码，不调用 LLM，在 ReviewGate 的审核通过之后运行。

    result = Validator().validate(documents)    # 一组文件（一个上传文件及其附件）的全部单据

把关环节（extract/）已经保证每个值都能在原文中找到、算术一致、语义合理。这里补的是
"值确实照抄自原文，但原文本身或 OCR 有问题、或者这组单据放在一起有矛盾"的情况：

| 编号 | 检查                                  | 级别    |
|------|---------------------------------------|---------|
| V1   | 箱号符合 ISO 6346（格式 + 校验位），给出 OCR 混淆字符的纠正建议 | error |
| V2   | 单据没有费用行（有合计却没有费用行，或什么都没有）              | error |
| V3   | 费用行没有金额                                                 | error |
| V4   | 单据没有任何匹配键（单据号、提单号、订舱号、箱号全空）           | error |
| V5   | 同组内同一单据号的单据，签发方 / 日期 / 币种不一致              | error |
| V6   | 同一单据内重复的费用行（费用名 + 金额 + 币种相同）               | warning |
| V7   | 同组不同单据间重复的费用（如邮件正文和附件都写了同一笔）          | warning |
| V8   | 金额为 0                                                       | warning |

error 由 ReviewGate 退回抽取重试一次，仍有则转待人工；warning 只记录，不阻止入账。
币种是否符合 ISO 4217、日期能否解析，放在标准化之后校验（用户确认）。
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

from bill_agent.context import loose, squash
from bill_agent.models import ExtractedDocument


class Severity(str, Enum):
    ERROR = "error"        # 不能入账：退回抽取，仍有则待人工
    WARNING = "warning"    # 可以入账，记录在审计中


@dataclass(frozen=True)
class ValidationIssue:
    code: str              # V1 ~ V8，见模块说明
    severity: Severity
    doc: int | None        # 单据序号，从 1 开始；跨单据的问题为 None
    field: str             # 字段路径，如 container_nos、charges[2].amount
    message: str

    def __str__(self) -> str:
        where = f"单据 {self.doc} " if self.doc is not None else ""
        return f"[{self.code}] {where}{self.field}: {self.message}"


@dataclass
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]


# ---------- V1 箱号 ISO 6346 ----------

# 箱主代码 3 个字母 + 类别 U/J/Z + 6 位序号 + 1 位校验位
_CONTAINER = re.compile(r"^[A-Z]{3}[UJZ]\d{7}$")
# OCR 常见混淆：字母位上识别成数字、数字位上识别成字母
_TO_LETTER = str.maketrans("012568", "OIZSGB")
_TO_DIGIT = str.maketrans("ODQILZSGB", "000112568")


def _letter_values() -> dict[str, int]:
    values, n = {}, 10
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if n % 11 == 0:          # 跳过 11 的倍数
            n += 1
        values[ch] = n
        n += 1
    return values


_LETTER_VALUES = _letter_values()


def container_check_digit(first10: str) -> int:
    """ISO 6346 校验位：前 10 位按位值 × 2^位置 求和，模 11 再模 10。"""
    total = sum((_LETTER_VALUES[ch] if ch.isalpha() else int(ch)) << i for i, ch in enumerate(first10))
    return total % 11 % 10


def container_problem(raw: str) -> str | None:
    """箱号合格返回 None，否则返回问题描述（能纠正时附上建议值）。"""
    value = re.sub(r"[\s\-/.]", "", raw).upper()
    if _CONTAINER.match(value):
        expected = container_check_digit(value[:10])
        if expected == int(value[10]):
            return None
        return f"箱号 {raw!r} 校验位应为 {expected}，实际为 {value[10]}，可能是 OCR 或原文错误"
    if len(value) == 11:
        fixed = value[:4].translate(_TO_LETTER) + value[4:].translate(_TO_DIGIT)
        if _CONTAINER.match(fixed) and container_check_digit(fixed[:10]) == int(fixed[10]):
            return f"箱号 {raw!r} 不符合 ISO 6346 格式，疑似 OCR 混淆字符，可能是 {fixed}"
    return f"箱号 {raw!r} 不符合 ISO 6346 格式（4 个字母 + 7 位数字）"


# ---------- 校验器 ----------

def _charge_key(name: str, amount, currency: str | None) -> tuple:
    return loose(name), amount, squash(currency or "")


class Validator:
    def validate(self, documents: list[ExtractedDocument]) -> ValidationResult:
        issues: list[ValidationIssue] = []
        for i, d in enumerate(documents, start=1):
            issues += self._document(i, d)
        issues += self._across(documents)
        return ValidationResult(issues)

    @staticmethod
    def _document(i: int, d: ExtractedDocument) -> list[ValidationIssue]:
        out: list[ValidationIssue] = []

        def add(code: str, severity: Severity, field_: str, message: str) -> None:
            out.append(ValidationIssue(code, severity, i, field_, message))

        for c in d.container_nos:                                                   # V1
            if problem := container_problem(c):
                add("V1", Severity.ERROR, "container_nos", problem)

        if not d.charges:                                                           # V2
            add("V2", Severity.ERROR, "charges",
                "有合计但没有费用行，可能漏抽了费用行" if d.totals else "单据没有任何费用行")

        seen: dict[tuple, int] = {}
        for c in d.charges:
            path = f"charges[{c.line_no}]"
            if c.amount is None:                                                    # V3
                add("V3", Severity.ERROR, f"{path}.amount", f"费用 {c.charge_name_raw!r} 没有金额")
                continue
            if c.amount == 0:                                                       # V8
                add("V8", Severity.WARNING, f"{path}.amount", f"费用 {c.charge_name_raw!r} 金额为 0")
            key = _charge_key(c.charge_name_raw, c.amount, c.currency_raw or d.currency_raw)
            if key in seen:                                                         # V6
                add("V6", Severity.WARNING, path,
                    f"与第 {seen[key]} 行费用名、金额、币种都相同（{c.charge_name_raw} {c.amount}），"
                    "确认不是重复提取")
            else:
                seen[key] = c.line_no

        if not (d.reference_no or d.bl_nos or d.booking_no or d.container_nos):     # V4
            add("V4", Severity.ERROR, "reference_no",
                "单据号、提单号、订舱号、箱号全部为空，无法与已有账单匹配，也无法识别重复")
        return out

    @staticmethod
    def _across(documents: list[ExtractedDocument]) -> list[ValidationIssue]:
        out: list[ValidationIssue] = []

        by_ref: dict[str, list[int]] = defaultdict(list)                           # V5
        for i, d in enumerate(documents, start=1):
            if d.reference_no:
                by_ref[squash(d.reference_no)].append(i)
        for idx in by_ref.values():
            if len(idx) < 2:
                continue
            ref = documents[idx[0] - 1].reference_no
            for name, norm in (("issuer", loose), ("document_date_raw", squash), ("currency_raw", squash)):
                values = {norm(v) for k in idx if (v := getattr(documents[k - 1], name))}
                if len(values) > 1:
                    shown = "、".join(f"单据 {k}={getattr(documents[k - 1], name)!r}" for k in idx)
                    out.append(ValidationIssue("V5", Severity.ERROR, None, name,
                                               f"单据号 {ref} 出现在多份单据中，{name} 不一致：{shown}"))

        first: dict[tuple, tuple[int, int]] = {}                                    # V7
        for i, d in enumerate(documents, start=1):
            for c in d.charges:
                if c.amount is None:
                    continue
                key = _charge_key(c.charge_name_raw, c.amount, c.currency_raw or d.currency_raw)
                if key in first and first[key][0] != i:
                    j, line = first[key]
                    out.append(ValidationIssue(
                        "V7", Severity.WARNING, i, f"charges[{c.line_no}]",
                        f"与单据 {j} 第 {line} 行费用名、金额、币种都相同（{c.charge_name_raw} {c.amount}），"
                        "可能是同一笔费用被重复提取"))
                first.setdefault(key, (i, c.line_no))
        return out
