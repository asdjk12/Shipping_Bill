"""标准化（6.4）：抽取结果通过把关和数据校验后，展开为 ChargeRecord，再进入匹配和 AR/AP。

| 字段     | 规则                                                       | 处理不了时        |
|----------|------------------------------------------------------------|-------------------|
| 费用名称 | config/charge_mapping.yaml，统一大小写/空格/标点后精确匹配   | 原样照抄          |
| 币种     | 行级优先、否则单据级；config/currency_alias.yaml + ISO 4217 | null，转人工      |
| 付款方式 | P/PP/Prepaid/预付/寄付 -> PP；C/CC/Collect/到付 -> CC        | null              |
| 单据类型 | 关键词：credit / invoice / debit，其余 Other                 | 原文为空时 null   |
| 日期     | 去掉时间；年在前为年/月/日，年在后为月/日/年（按 4 位年份定位） | null，转人工      |
"""

from .charge import ChargeNormalizer, charge_key
from .currency import ISO_4217, CurrencyNormalizer
from .dates import normalize_date
from .doc_type import normalize_document_type
from .normalizer import Normalizer
from .payment import PAYMENT_TERM_MAP, PAYMENT_TERMS, normalize_payment_term

__all__ = ["ChargeNormalizer", "charge_key", "ISO_4217", "CurrencyNormalizer", "normalize_date",
           "normalize_document_type", "Normalizer", "PAYMENT_TERM_MAP", "PAYMENT_TERMS",
           "normalize_payment_term"]
