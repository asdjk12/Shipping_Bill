"""操作判定与字段级 diff（6.7），并把结果写入账单库。

    无匹配                                  -> CREATE（写入新记录）
    唯一匹配：待更新字段为空                 -> NO_CHANGE（含重复导入，不写库）
              待更新字段非空                 -> UPDATE（只写这些字段，旧值快照进 bill_history）
    冲突 / 上游待人工 / AR/AP 无法确定        -> PENDING_REVIEW（不写库）

分批次抽取到同一笔交易时，后到的单据会匹配到已有记录，新信息以 UPDATE 合并进这条记录，
不会再新建一条。

待更新字段：
1. 单据带改账指令（change_instructions）时，命中指令目标的记录只改指令涉及的字段；
2. 否则视为修订后的单据：输入非 null 且与原值不同的字段才更新，null 一律保留原值；
   提单号 / 箱号取并集（新单据可能只写了其中一个），不删除已有的值；
   require_evidence=True 时还要求该字段有原文证据（抽取阶段已做代码核验，默认不再要求）；
3. 原文写法字段（*_raw、原始费用名）跟随对应的标准字段一起改，单独不同不算变更；
   费用名只是大小写、空格、标点不同（charge_key 相同）也不算变更；
4. accounting_entity / posting_type 不直接比较：在合并后的记录上重新跑 AR/AP 规则。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from bill_agent.models import (
    AuditTrail,
    BillRecord,
    ChargeRecord,
    ExtractedDocument,
    FieldChange,
    Operation,
    OutputRecord,
    parse_amount,
)
from bill_agent.models.extraction import ChangeInstruction
from bill_agent.normalize import Normalizer, charge_key, normalize_date, normalize_payment_term
from bill_agent.rules import EntityMatcher, apply_posting

from .matcher import Matcher, MatchResult, rule_name
from .repository import BillRepository

_DERIVED = ("accounting_entity", "posting_type")
_LIST_FIELDS = ("bl_nos", "container_nos")
# 标准字段 -> 跟着一起改的原文字段
_COMPANIONS = {
    "document_type": ("document_type_raw",),
    "document_date": ("document_date_raw",),
    "charge_item": ("original_charge_item",),
    "currency": ("currency_raw",),
    "payment_term": ("payment_term_raw",),
    "reference_no": ("line_no",),           # 行号是在原单据里的位置，换了单据才跟着换
}
_FOLLOWERS = {f for fs in _COMPANIONS.values() for f in fs}
# 业务字段 -> 抽取结果里的证据键
_EVIDENCE_KEY = {"document_type": "document_type_raw", "document_date": "document_date_raw",
                 "charge_item": "charge_name_raw", "original_charge_item": "charge_name_raw",
                 "currency": "currency_raw", "payment_term": "payment_term_raw"}
# 改账指令里的字段名 -> 记录字段
_INSTRUCTION_FIELDS = {
    "amount": "amount", "quantity": "quantity", "qty": "quantity", "unit_price": "unit_price",
    "currency": "currency", "payment_term": "payment_term", "document_date": "document_date",
    "date": "document_date", "charge_item": "charge_item", "charge_name": "charge_item",
    "reference_no": "reference_no", "booking_no": "booking_no", "issuer": "issuer",
    "recipient": "recipient", "payee": "payee", "payer": "payer",
}
_AMOUNT_NOISE = re.compile(r"^[^\d\-(]+|[^\d)]+$")


def _display(v: Any) -> str | float | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, list):
        return ", ".join(map(str, v))
    return v if isinstance(v, (str, float, int)) else str(v)


def _tag(rec: ChargeRecord) -> str:
    return f"{rec.reference_no or '-'} 第 {rec.line_no or '-'} 行 {rec.original_charge_item}"


@dataclass
class OperationResult:
    operation: Operation
    record: ChargeRecord                   # 写库后的记录（UPDATE / CREATE），否则为输入记录
    match: MatchResult | None = None
    changes: list[FieldChange] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)

    @property
    def record_id(self) -> str | None:
        return self.record.record_id if isinstance(self.record, BillRecord) else None

    def to_output(self) -> OutputRecord:
        m = self.match
        audit = AuditTrail(
            ai_reasoning=self.reasoning,
            source_text=[e.quote for e in self.record.evidence.values()],
            matched_rule=m.rule if m and m.record else None,
            matched_record_id=m.record.record_id if m and m.record else None,
            changed_fields=self.changes,
            review_reasons=self.review_reasons,
        )
        return OutputRecord.from_charge_record(self.record, self.operation, audit)


class BillProcessor:
    """一批单据：匹配 -> 操作判定 -> 写库。一个批次一个事务。"""

    def __init__(self, repo: BillRepository, matcher: Matcher | None = None,
                 entity: EntityMatcher | None = None, normalizer: Normalizer | None = None,
                 require_evidence: bool = False):
        self.repo = repo
        self.matcher = matcher or Matcher(repo)
        self.entity = entity
        self.normalizer = normalizer or Normalizer()
        self.require_evidence = require_evidence

    def process_batch(self, items: list[tuple[ExtractedDocument | None, list[ChargeRecord]]]) -> list[OperationResult]:
        """items：(抽取出的单据, 它标准化后的费用行)。单据只用来读改账指令，可为 None。"""
        self.matcher.new_batch()
        with self.repo.transaction():
            return [r for doc, recs in items for r in self.process_document(doc, recs)]

    def process_document(self, doc: ExtractedDocument | None, records: list[ChargeRecord]) -> list[OperationResult]:
        instructions = doc.change_instructions if doc else []
        planned, results = self._plan_instructions(doc, instructions) if instructions else ({}, [])
        for rec in records:
            results.append(self._one(rec, planned if instructions else None))
        # 指令目标不在本单据费用行里的（如邮件正文要求改另一张单据），单独输出 UPDATE
        for rid, (changes, notes, evidence) in planned.items():
            old = self.repo.get(rid)
            res = self._update(old, old, changes, notes, evidence, source_id=doc.source_id)
            results.append(res)
        return results

    # ------------------------------------------------------------------ 单条费用行

    def _one(self, rec: ChargeRecord, planned: dict | None) -> OperationResult:
        if self.entity is not None:
            rec = apply_posting([rec], self.entity)[0]
        m = self.matcher.match(rec)
        reasoning = [*m.reasoning, *(f"AR/AP：{r}" for r in rec.posting_reasoning)]

        if rec.review_reasons or m.conflict:
            reasons = [*rec.review_reasons, *([f"匹配冲突：{m.conflict}"] if m.conflict else [])]
            return OperationResult(Operation.PENDING_REVIEW, rec, m, reasoning=reasoning, review_reasons=reasons)

        if m.record is None:
            stored = self.repo.insert(BillRecord(**rec.model_dump()))
            self.matcher.claim(stored.record_id, rec)
            return OperationResult(Operation.CREATE, stored, m,
                                   reasoning=[*reasoning, f"新建记录 {stored.record_id}"])

        old = m.record
        if planned is not None:
            # 带改账指令的单据：只改指令涉及的字段
            changes, notes, evidence = planned.pop(old.record_id, ({}, ["改账指令没有涉及这条费用"], {}))
        else:
            changes, notes = self._diff(old, rec)
            evidence = rec.evidence
        res = self._update(old, rec, changes, notes, evidence, source_id=rec.source_ids[0] if rec.source_ids else None)
        res.match = m
        res.reasoning = [*reasoning, *res.reasoning]
        return res

    def _diff(self, old: BillRecord, rec: ChargeRecord) -> tuple[dict[str, Any], list[str]]:
        changes: dict[str, Any] = {}
        notes: list[str] = []
        for f in rec.business_fields():
            if f in _DERIVED or f in _FOLLOWERS:
                continue
            new, cur = getattr(rec, f), getattr(old, f)
            if f in _LIST_FIELDS:
                merged = list(dict.fromkeys([*cur, *new]))
                if merged != cur:
                    changes[f] = merged
                continue
            if new is None or new == cur:
                continue
            if f == "charge_item" and charge_key(new) == charge_key(cur or ""):
                continue                    # 只是大小写 / 标点不同，不算变更
            if self.require_evidence and _EVIDENCE_KEY.get(f, f) not in rec.evidence:
                notes.append(f"{f}: {_display(cur)!r} -> {_display(new)!r} 没有原文证据，保留原值")
                continue
            changes[f] = new
        for f in list(changes):
            for c in _COMPANIONS.get(f, ()):
                changes[c] = getattr(rec, c)
        return changes, notes

    def _update(self, old: BillRecord, rec: ChargeRecord, changes: dict[str, Any], notes: list[str],
                evidence: dict, source_id: str | None) -> OperationResult:
        reasoning = list(notes)
        review: list[str] = []
        # 合并后的记录上重新判断 AR/AP
        if self.entity is not None and changes:
            merged = apply_posting([old.model_copy(update=changes)], self.entity)[0]
            for f in _DERIVED:
                if getattr(merged, f) != getattr(old, f):
                    changes[f] = getattr(merged, f)
            changes["posting_reasoning"] = merged.posting_reasoning
            review += merged.review_reasons[len(old.review_reasons):]
        elif self.entity is None:
            for f in _DERIVED:
                v = getattr(rec, f)
                if v is not None and v != getattr(old, f):
                    changes[f] = v

        diff = [f for f in changes if f not in ("posting_reasoning",) and getattr(old, f) != changes[f]]
        visible = [f for f in diff if f not in _FOLLOWERS or f == "line_no"]
        if not visible:
            return OperationResult(Operation.NO_CHANGE, old, reasoning=[*reasoning, "没有需要更新的字段"])
        if review:
            return OperationResult(Operation.PENDING_REVIEW, rec, reasoning=reasoning, review_reasons=review)

        field_changes = [FieldChange(field=f, old_value=_display(getattr(old, f)), new_value=_display(changes[f]),
                                     evidence=(e.quote if (e := evidence.get(_EVIDENCE_KEY.get(f, f))) else None))
                         for f in visible]
        changes["evidence"] = {**old.evidence, **{k: v for k, v in evidence.items()}}
        stored = self.repo.update(old.record_id, changes, expected_version=old.version, source_id=source_id)
        reasoning += [f"{c.field}: {c.old_value!r} -> {c.new_value!r}" for c in field_changes]
        reasoning.append(f"更新记录 {old.record_id}，版本 {old.version} -> {stored.version}")
        return OperationResult(Operation.UPDATE, stored, changes=field_changes, reasoning=reasoning)

    # ------------------------------------------------------------------ 改账指令

    def _plan_instructions(self, doc: ExtractedDocument, instructions: list[ChangeInstruction]):
        """返回 ({record_id: (changes, notes, evidence)}, 无法执行的指令 -> PENDING_REVIEW 结果)。"""
        planned: dict[str, tuple[dict, list[str], dict]] = {}
        pending: list[OperationResult] = []
        for ins in instructions:
            desc = f"改账指令「{ins.evidence.quote}」"
            ref = ins.target_reference_no or doc.reference_no
            charge = self.normalizer.charge(ins.target_charge_name_raw) if ins.target_charge_name_raw else None
            placeholder = ChargeRecord(reference_no=ref, original_charge_item=ins.target_charge_name_raw or ins.field,
                                       charge_item=charge, source_ids=[doc.source_id],
                                       evidence={"change_instruction": ins.evidence})
            # 账单库按 charge_key 比较，指令里费用名的大小写、标点与单据不同也能找到
            targets = self.repo.find_by_ref_charge(ref, charge)
            problem = None
            if not ref or not charge:
                problem = f"{desc} 没有指明单据号和费用名称"
            elif len(targets) != 1:
                problem = f"{desc} 的目标 {ref} / {charge} 在账单库中有 {len(targets)} 条记录"
            if problem is None:
                f, value, problem = self._instruction_value(ins)
            if problem:
                pending.append(OperationResult(Operation.PENDING_REVIEW, placeholder, reasoning=[problem],
                                               review_reasons=[problem]))
                continue
            changes, notes, ev = planned.setdefault(targets[0].record_id, ({}, [], {}))
            changes.update(value)
            notes.append(f"{desc}：{f} -> {ins.new_value_raw}")
            ev[_EVIDENCE_KEY.get(f, f)] = ins.evidence
        return planned, pending

    def _instruction_value(self, ins: ChangeInstruction) -> tuple[str, dict[str, Any], str | None]:
        f = _INSTRUCTION_FIELDS.get(ins.field.strip().lower().replace(" ", "_"))
        raw = ins.new_value_raw.strip()
        if f is None:
            return ins.field, {}, f"改账指令要改的字段 {ins.field!r} 不支持"
        try:
            if f in ("amount", "quantity", "unit_price"):
                return f, {f: parse_amount(_AMOUNT_NOISE.sub("", raw))}, None
            if f == "currency":
                v, problem = self.normalizer.currency(raw)
                return f, {f: v, "currency_raw": raw}, problem
            if f == "payment_term":
                v = normalize_payment_term(raw)
                return f, {f: v, "payment_term_raw": raw}, None if v else f"付款方式 {raw!r} 无法识别"
            if f == "document_date":
                v, problem = normalize_date(raw)
                return f, {f: v, "document_date_raw": raw}, problem
            if f == "charge_item":
                return f, {f: self.normalizer.charge(raw), "original_charge_item": raw}, None
        except ValueError as e:
            return f, {}, f"改账指令的新值 {raw!r} 无法解析：{e}"
        return f, {f: raw}, None


__all__ = ["BillProcessor", "OperationResult", "rule_name"]
