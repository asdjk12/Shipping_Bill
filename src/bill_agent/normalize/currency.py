"""币种标准化（七.4 币种统一使用三位代码）：别名表 -> ISO 4217 白名单。"""

from __future__ import annotations

import re
from pathlib import Path

from .config import DEFAULT_CONFIG_DIR, load_mapping

# ISO 4217 现行代码
ISO_4217 = frozenset("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD
CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD
GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT
LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR
NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD
SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND VUV WST XAF XCD
XCG XOF XPF YER ZAR ZMW ZWG
""".split())


def currency_key(raw: str) -> str:
    """忽略大小写、空白和点号：`U.S.D` -> `USD`，`us $` -> `US$`。"""
    return re.sub(r"[\s.]+", "", raw).upper()


class CurrencyNormalizer:
    def __init__(self, config_dir: Path = DEFAULT_CONFIG_DIR):
        self.alias: dict[str, str] = {}
        for code, aliases in load_mapping("currency_alias.yaml", config_dir).items():
            if code not in ISO_4217:
                raise ValueError(f"currency_alias.yaml 中 {code!r} 不是 ISO 4217 代码")
            for a in aliases:
                self.alias[currency_key(a)] = code

    def __call__(self, raw: str | None) -> tuple[str | None, str | None]:
        """返回 (ISO 代码, 问题)。原文为空时两者都是 None。"""
        if raw is None or not raw.strip():
            return None, None
        key = currency_key(raw)
        code = self.alias.get(key, key)
        if code in ISO_4217:
            return code, None
        return None, f"币种 {raw!r} 无法确定为 ISO 4217 代码"
