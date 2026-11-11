"""账单匹配（6.6）：新费用行在账单库里对应哪条已有记录。

| 优先级 | 匹配键                         |
|--------|--------------------------------|
| 1      | 单据号 + 费用名称              |
| 2      | 提单号 + 费用名称 + 币种       |
| 3      | 订舱号 + 费用名称 + 币种       |
| 4      | 箱号 + 交易双方 + 日期 + 金额  |

- 四级都会查，记录每级的命中情况；取最高一级有命中的结果；
- 匹配键有 null 的那一级跳过；提单号 / 箱号有多个时，任一个命中就算；
- 同一级命中多条时先消歧：同一份单据里行号不同的是别的费用行，排除；再优先行号相同、金额相同的。
  消歧后仍有多条 -> 冲突；
- 低一级规则唯一命中了另一条费用名称相同的记录 -> 冲突（命中费用名不同的记录只记录，不算冲突，
  规则 4 不含费用名，同一箱同一天同金额的不同费用很常见）；
- 同一批次里两条新费用行匹配到同一条已有记录 -> 冲突；
- 交易双方先经过公司别名表（config/entity_alias.yaml）归一化再比较。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bill_agent.context import loose
from bill_agent.models import BillRecord, ChargeRecord, MatchRule
from bill_agent.normalize.charge import charge_key
from bill_agent.normalize.config import DEFAULT_CONFIG_DIR, load_mapping

from .repository import BillRepository

_RULE_NAMES = {
    MatchRule.REF_NO_AND_CHARGE: "单据号 + 费用名称",
    MatchRule.BL_NO_CHARGE_CURRENCY: "提单号 + 费用名称 + 币种",
    MatchRule.BOOKING_NO_CHARGE_CURRENCY: "订舱号 + 费用名称 + 币种",
    MatchRule.CONTAINER_PARTIES_DATE_AMOUNT: "箱号 + 交易双方 + 日期 + 金额",
}


def rule_name(rule: MatchRule) -> str:
    return f"规则 {rule.value}（{_RULE_NAMES[rule]}）"


class PartyNormalizer:
    """公司名 -> 比较用的键：命中别名表时取标准名，否则去掉大小写、空白和标点。"""

    def __init__(self, table: dict[str, list[str]] | None = None):
        self._map: dict[str, str] = {}
        for name, aliases in (table or {}).items():
            for a in [name, *aliases]:
                if k := loose(a):
                    self._map[k] = loose(name)

    @classmethod
    def load(cls, config_dir: Path = DEFAULT_CONFIG_DIR) -> "PartyNormalizer":
        return cls(load_mapping("entity_alias.yaml", config_dir))

    def key(self, name: str | None) -> str | None:
        k = loose(name or "")
        return self._map.get(k, k) or None

    def same(self, a: str | None, b: str | None) -> bool:
        ka, kb = self.key(a), self.key(b)
        return ka is not None and ka == kb


@dataclass
class RuleHit:
    rule: MatchRule
    skipped: bool = False                                  # 匹配键有 null，跳过
    candidates: list[str] = field(default_factory=list)    # 查询命中的记录
    narrowed: list[str] = field(default_factory=list)      # 消歧后剩下的记录

    def describe(self) -> str:
        if self.skipped:
            return f"{rule_name(self.rule)}：匹配键缺失，跳过"
        if not self.candidates:
            return f"{rule_name(self.rule)}：未命中"
        s = f"{rule_name(self.rule)}：命中 {len(self.candidates)} 条"
        if not self.narrowed:
            return s + f" {self.candidates}，都是同一单据的其他费用行，排除"
        if len(self.narrowed) != len(self.candidates):
            s += f"，消歧后剩 {len(self.narrowed)} 条"
        return s + f" {self.narrowed or self.candidates}"


@dataclass
class MatchResult:
    record: BillRecord | None = None      # 唯一匹配到的已有记录
    rule: MatchRule | None = None
    hits: list[RuleHit] = field(default_factory=list)
    conflict: str | None = None

    @property
    def reasoning(self) -> list[str]:
        out = [h.describe() for h in self.hits]
        if self.conflict:
            out.append(f"匹配冲突：{self.conflict}")
        elif self.record:
            out.append(f"按{rule_name(self.rule)}匹配到已有记录 {self.record.record_id}")
        else:
            out.append("账单库中没有匹配的记录")
        return out


class Matcher:
    def __init__(self, repo: BillRepository, parties: PartyNormalizer | None = None):
        self.repo = repo
        self.parties = parties or PartyNormalizer()
        self._claimed: dict[str, str] = {}     # 本批次已匹配的 record_id -> 说明

    def new_batch(self) -> None:
        self._claimed.clear()

    def match(self, rec: ChargeRecord) -> MatchResult:
        hits: list[RuleHit] = []
        found: dict[str, BillRecord] = {}
        for rule, cands in self._query(rec):
            hit = RuleHit(rule, skipped=cands is None)
            if cands:
                found.update((c.record_id, c) for c in cands)
                hit.candidates = [c.record_id for c in cands]
                hit.narrowed = [c.record_id for c in self._narrow(rec, cands)]
            hits.append(hit)

        result = MatchResult(hits=hits)
        primary = next((h for h in hits if h.narrowed), None)
        if primary is None:
            return result
        result.rule = primary.rule
        if len(primary.narrowed) > 1:
            result.conflict = f"{rule_name(primary.rule)} 命中多条记录 {primary.narrowed}，无法确定是哪一条"
            return result
        chosen = primary.narrowed[0]
        for h in hits:
            if h is primary or len(h.narrowed) != 1 or h.narrowed[0] == chosen:
                continue
            other = found[h.narrowed[0]]
            if charge_key(other.charge_item or "") == charge_key(rec.charge_item or ""):
                result.conflict = (f"{rule_name(primary.rule)} 命中 {chosen}，"
                                   f"但{rule_name(h.rule)} 命中另一条记录 {other.record_id}")
                return result
        tag = f"{rec.reference_no or '-'} 第 {rec.line_no} 行 {rec.original_charge_item}"
        if chosen in self._claimed:
            result.conflict = f"本批次中 {self._claimed[chosen]} 已匹配到记录 {chosen}"
            return result
        self._claimed[chosen] = tag
        result.record = found[chosen]
        return result

    def claim(self, record_id: str, rec: ChargeRecord) -> None:
        """新建的记录也登记到本批次，防止同批次另一条费用行再匹配到它后被当成更新。"""
        self._claimed[record_id] = f"{rec.reference_no or '-'} 第 {rec.line_no} 行 {rec.original_charge_item}"

    def _query(self, rec: ChargeRecord):
        """按优先级返回 (规则, 候选)；候选为 None 表示匹配键缺失、跳过该级。"""
        r = self.repo
        yield MatchRule.REF_NO_AND_CHARGE, (
            r.find_by_ref_charge(rec.reference_no, rec.charge_item)
            if rec.reference_no and rec.charge_item else None)
        yield MatchRule.BL_NO_CHARGE_CURRENCY, (
            _union(r.find_by_bl(bl, rec.charge_item, rec.currency) for bl in rec.bl_nos)
            if rec.bl_nos and rec.charge_item and rec.currency else None)
        yield MatchRule.BOOKING_NO_CHARGE_CURRENCY, (
            r.find_by_booking(rec.booking_no, rec.charge_item, rec.currency)
            if rec.booking_no and rec.charge_item and rec.currency else None)
        ok4 = (rec.container_nos and rec.issuer and rec.recipient and rec.document_date is not None
               and rec.amount is not None)
        yield MatchRule.CONTAINER_PARTIES_DATE_AMOUNT, (
            [c for c in _union(r.find_by_container(cn, rec.document_date, rec.amount) for cn in rec.container_nos)
             if self.parties.same(c.issuer, rec.issuer) and self.parties.same(c.recipient, rec.recipient)]
            if ok4 else None)

    @staticmethod
    def _narrow(rec: ChargeRecord, cands: list[BillRecord]) -> list[BillRecord]:
        src = rec.source_ids[0] if rec.source_ids else None
        # 同一份单据里行号不同的，是这张单据的另一行费用（样例 DN 有两行 HANDLING CHARGE）
        cands = [c for c in cands
                 if not (src and src in c.source_ids and c.line_no is not None and rec.line_no is not None
                         and c.line_no != rec.line_no)]
        for key in ("line_no", "amount"):
            v = getattr(rec, key)
            if len(cands) > 1 and v is not None:
                same = [c for c in cands if getattr(c, key) == v]
                if same:
                    cands = same
        return cands


def _union(groups) -> list[BillRecord]:
    out: dict[str, BillRecord] = {}
    for g in groups:
        out.update((c.record_id, c) for c in g)
    return list(out.values())
