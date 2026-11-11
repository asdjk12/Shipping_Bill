"""AR/AP 判断（6.8）：从记账主体的视角，按收付款方算出 posting_type。

    entity = EntityMatcher.load("PAT")
    records = apply_posting(records, entity)          # 填 accounting_entity / posting_type

收款方：单据写明 > 单据类型（DN / Invoice 取签发方，CN 取接收方）。
付款方：单据写明 > 付款方式（CC 取 consignee，PP 取 shipper）> 单据类型兜底（DN / Invoice 取接收方，
CN 取签发方）。不用签发方补付款方：签发方常常只是印在抬头的航运公司，不一定是账单一方。
兜底推出的付款方与收款方是同一家公司时，视为未知。金额为负或有退款语义时方向取反。

主体是收款方 -> AR；是付款方 -> AP；不是任何一方 -> null（不转人工）；
别名匹配不确定（名字很像或包含别名但不相同）-> null，并转人工。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path

from bill_agent.context import loose
from bill_agent.models import ChargeRecord
from bill_agent.models.enums import DocumentType, PaymentTerm, PostingType
from bill_agent.normalize.config import DEFAULT_CONFIG_DIR, load_mapping

FUZZY_THRESHOLD = 0.85       # 不完全相同、相似度不低于此值的名字：无法确定是不是主体，转人工
REFUND_HINT = re.compile(r"(?i)\brefund|\brebate|退款|退费|返还|返利")


class EntityMatch(str, Enum):
    YES = "yes"
    NO = "no"
    UNSURE = "unsure"


def same_party(a: str | None, b: str | None) -> bool:
    """两个公司名是否同一家：忽略大小写、空白和标点后相同。"""
    return bool(a and b) and loose(a) == loose(b) != ""


class EntityMatcher:
    """记账主体及其别名。别名表在 config/entity_alias.yaml，不让 LLM 去猜“我方是谁”。"""

    def __init__(self, name: str, aliases: list[str], fuzzy: float = FUZZY_THRESHOLD):
        self.name = name
        self.fuzzy = fuzzy
        self._keys = sorted({k for k in map(loose, [name, *aliases]) if k})
        self._words = [w for w in (re.sub(r"[\W_]+", " ", a).lower().split() for a in [name, *aliases])]

    @classmethod
    def load(cls, name: str, config_dir: Path = DEFAULT_CONFIG_DIR, fuzzy: float = FUZZY_THRESHOLD):
        table = load_mapping("entity_alias.yaml", config_dir)
        hit = next((k for k, vs in table.items() if same_party(k, name) or any(same_party(v, name) for v in vs)),
                   None)
        if hit is None:
            raise ValueError(f"entity_alias.yaml 中没有记账主体 {name!r}")
        return cls(hit, table[hit], fuzzy)

    def match(self, company: str | None) -> tuple[EntityMatch, str]:
        """返回匹配结果和说明。"""
        key = loose(company or "")
        if not key:
            return EntityMatch.NO, ""
        if key in self._keys:
            return EntityMatch.YES, f"{company!r} 是 {self.name} 的别名"
        score = max(SequenceMatcher(None, key, k).ratio() for k in self._keys)
        if score >= self.fuzzy:
            return EntityMatch.UNSURE, f"{company!r} 与 {self.name} 的别名相似（{score:.2f}），无法确定是不是同一家"
        tokens = re.sub(r"[\W_]+", " ", company).lower().split()
        for words in self._words:
            n = len(words)
            if n and any(tokens[i:i + n] == words for i in range(len(tokens) - n + 1)):
                return EntityMatch.UNSURE, f"{company!r} 包含 {self.name} 的别名 {' '.join(words)!r}，无法确定是不是同一家"
        return EntityMatch.NO, ""


@dataclass
class PostingDecision:
    posting_type: PostingType | None
    payee: str | None
    payer: str | None
    reasoning: list[str] = field(default_factory=list)
    review_reason: str | None = None          # 有值时转人工


_BY_TYPE = {
    DocumentType.DEBIT_NOTE: ("issuer", "recipient"),
    DocumentType.INVOICE: ("issuer", "recipient"),
    DocumentType.CREDIT_NOTE: ("recipient", "issuer"),
}
_ROLE = {"issuer": "签发方", "recipient": "接收方"}


def decide_posting_type(rec: ChargeRecord, entity: EntityMatcher) -> PostingDecision:
    """按 6.8 的规则判断一条费用记录的 AR/AP。只读 rec，不修改。"""
    why: list[str] = []
    by_type = _BY_TYPE.get(rec.document_type)
    doc_type = rec.document_type.value if rec.document_type else "未知"

    if rec.payee:
        payee = rec.payee
        why.append(f"收款方 {payee!r}：单据写明")
    elif by_type and getattr(rec, by_type[0]):
        payee = getattr(rec, by_type[0])
        why.append(f"收款方 {payee!r}：单据类型 {doc_type}，取{_ROLE[by_type[0]]}")
    else:
        payee = None
        why.append("收款方未知：单据没有写明，单据类型也推不出")

    payer = None
    if rec.payer:
        payer = rec.payer
        why.append(f"付款方 {payer!r}：单据写明")
    else:
        if rec.payment_term == PaymentTerm.CC and rec.consignee:
            payer = rec.consignee
            why.append(f"付款方 {payer!r}：到付（CC），取 consignee")
        elif rec.payment_term == PaymentTerm.PP and rec.shipper:
            payer = rec.shipper
            why.append(f"付款方 {payer!r}：寄付（PP），取 shipper")
        elif by_type and getattr(rec, by_type[1]):
            cand = getattr(rec, by_type[1])
            if same_party(cand, payee):
                why.append(f"付款方未知：单据类型推出的{_ROLE[by_type[1]]} {cand!r} 就是收款方")
            else:
                payer = cand
                why.append(f"付款方 {payer!r}：没有写明，也没有可用的付款方式，按单据类型 {doc_type} 取"
                           f"{_ROLE[by_type[1]]}")
        if payer is None and not why[-1].startswith("付款方未知"):
            why.append("付款方未知：单据没有写明，付款方式和单据类型都推不出")

    if payee is None and payer is None:
        return PostingDecision(None, None, None, why + ["无法确定收付款方，posting_type 为 null"])

    refund = []
    if rec.amount is not None and rec.amount < 0:
        refund.append(f"金额为负（{rec.amount}）")
    text = " ".join(filter(None, [rec.original_charge_item, rec.document_type_raw]))
    if REFUND_HINT.search(text):
        refund.append("有退款语义")
    if refund:
        payee, payer = payer, payee
        why.append(f"{'、'.join(refund)}，方向取反：收款方 {payee!r}，付款方 {payer!r}")

    as_payee, note_payee = entity.match(payee)
    as_payer, note_payer = entity.match(payer)
    if as_payee == EntityMatch.YES and as_payer != EntityMatch.YES:
        return PostingDecision(PostingType.AR, payee, payer,
                               why + [f"{entity.name} 是收款方，钱流入 {entity.name}，记 AR"])
    if as_payer == EntityMatch.YES and as_payee != EntityMatch.YES:
        return PostingDecision(PostingType.AP, payee, payer,
                               why + [f"{entity.name} 是付款方，钱从 {entity.name} 流出，记 AP"])
    if as_payee == as_payer == EntityMatch.YES:
        reason = f"收款方和付款方都是 {entity.name}，无法判断 AR/AP"
        return PostingDecision(None, payee, payer, why + [reason], reason)
    unsure = [n for m, n in [(as_payee, note_payee), (as_payer, note_payer)] if m == EntityMatch.UNSURE]
    if unsure:
        reason = "；".join(unsure)
        return PostingDecision(None, payee, payer, why + [reason + "，posting_type 为 null"], reason)
    return PostingDecision(None, payee, payer, why + [f"{entity.name} 不是交易任何一方，posting_type 为 null"])


def apply_posting(records: list[ChargeRecord], entity: EntityMatcher) -> list[ChargeRecord]:
    """返回填好 accounting_entity / posting_type / posting_reasoning 的新记录；不确定时追加 review_reasons。"""
    out = []
    for rec in records:
        d = decide_posting_type(rec, entity)
        reasons = list(rec.review_reasons)
        if d.review_reason:
            reasons.append(f"费用行 {rec.line_no} posting_type: {d.review_reason}")
        out.append(rec.model_copy(update=dict(
            accounting_entity=entity.name, posting_type=d.posting_type,
            posting_reasoning=d.reasoning, review_reasons=reasons)))
    return out
