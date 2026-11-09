"""把通过把关的 ExtractedDocument 展开为标准化后的 ChargeRecord（每个费用行一条，四.4）。

    records = Normalizer().normalize_document(doc, source_text)   # 一份单据
    records = Normalizer().normalize_result(gate_result, texts)   # skill 返回的一组文件

只做确定性转换，不调用 LLM。原文和标准值都保留（*_raw 与标准字段）。处理不了的值置为 null，
原因写进 ChargeRecord.review_reasons；费用名称不在标准化表中不算问题，原样照抄。
"""

from __future__ import annotations

from pathlib import Path

from bill_agent.models import ChargeRecord, ExtractedCharge, ExtractedDocument

from .charge import ChargeNormalizer
from .config import DEFAULT_CONFIG_DIR
from .currency import CurrencyNormalizer
from .dates import normalize_date
from .doc_type import normalize_document_type
from .payment import normalize_payment_term


class Normalizer:
    def __init__(self, config_dir: Path = DEFAULT_CONFIG_DIR):
        self.charge = ChargeNormalizer(config_dir)
        self.currency = CurrencyNormalizer(config_dir)

    def normalize_document(self, doc: ExtractedDocument, source_text: str | None = None,
                           review_reasons: list[str] | None = None) -> list[ChargeRecord]:
        """source_text：单据原文，用来判断数字日期的月日顺序；review_reasons：上游（把关）给出的原因。"""
        upstream = list(review_reasons or [])
        doc_date, date_problem = normalize_date(doc.document_date_raw, source_text)
        doc_level = dict(
            document_type=normalize_document_type(doc.document_type_raw),
            document_type_raw=doc.document_type_raw,
            reference_no=doc.reference_no,
            document_date=doc_date,
            document_date_raw=doc.document_date_raw,
            issuer=doc.issuer, recipient=doc.recipient, payee=doc.payee, payer=doc.payer,
            shipper=doc.shipper, consignee=doc.consignee, carrier=doc.carrier,
            bl_nos=list(doc.bl_nos), booking_no=doc.booking_no, container_nos=list(doc.container_nos),
        )
        doc_problems = [f"document_date: {date_problem}"] if date_problem else []
        return [self._charge_record(doc, c, doc_level, upstream + doc_problems) for c in doc.charges]

    def _charge_record(self, doc: ExtractedDocument, c: ExtractedCharge, doc_level: dict,
                       reasons: list[str]) -> ChargeRecord:
        reasons = list(reasons)
        currency_raw = c.currency_raw or doc.currency_raw       # 行上没有币种时继承单据级币种
        currency, problem = self.currency(currency_raw)
        if problem:
            reasons.append(f"费用行 {c.line_no} currency: {problem}")
        elif currency is None and c.amount is not None:
            reasons.append(f"费用行 {c.line_no} currency: 费用行和单据都没有币种")
        evidence = {**doc.evidence, **c.evidence}
        source_ids = list(dict.fromkeys([doc.source_id, *(e.source_id for e in evidence.values())]))
        return ChargeRecord(
            **doc_level,
            line_no=c.line_no,
            charge_item=self.charge(c.charge_name_raw),
            original_charge_item=c.charge_name_raw,
            quantity=c.quantity, unit_price=c.unit_price, amount=c.amount,
            currency=currency, currency_raw=currency_raw,
            payment_term=normalize_payment_term(c.payment_term_raw), payment_term_raw=c.payment_term_raw,
            source_ids=source_ids, evidence=evidence, review_reasons=reasons,
        )

    def normalize_result(self, result, texts: dict[str, str] | None = None) -> list[ChargeRecord]:
        """result：extract.gate.GateResult。待人工复核的组也标准化，把关原因带到每条记录。

        texts：source_id -> 原文（RawDocument.text），没有时日期只按自身判断月日顺序。
        """
        upstream = list(result.reasons) if result.status.value != "passed" else []
        texts = texts or {}
        return [rec for doc in result.documents
                for rec in self.normalize_document(doc, texts.get(doc.source_id), upstream)]
