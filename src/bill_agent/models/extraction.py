"""LLM 抽取结果：ExtractedDocument / ExtractedCharge。

这是 LLM with_structured_output 的目标 schema。只包含 test.pdf 要求的字段，
单据上的其他信息（船名、港口、结算条款、汇率等）一律不抽取，后续业务需要时再加。

四.2 航运业务信息提取
    - 单据类型                  -> document_type_raw
    - 单据编号及日期            -> reference_no, document_date_raw
    - 签发方和接收方            -> issuer, recipient
    - 费用名称                  -> ExtractedCharge.charge_name_raw
    - 数量、单价和金额          -> ExtractedCharge.quantity / unit_price / amount
    - 币种                      -> ExtractedCharge.currency_raw（行上没有时取单据级 currency_raw）
    - PP、CC 等付款方式         -> ExtractedCharge.payment_term_raw
    - 小计、税额和总计          -> totals
四.5 更新已有账单
    - 匹配键：提单号 / 订舱号 / 箱号   -> bl_nos / booking_no / container_nos
      （单据编号、交易双方、日期、金额已在上面）
    - 只修改明确要求变更的字段         -> change_instructions
四.6 应收应付判断：结合收费方向
    - 单据写明的收款方 / 付款方        -> payee / payer
四.4、七.5 每个费用项单独一条记录；四.7 同一费用是否被重复提取
    - 费用行序号                       -> ExtractedCharge.line_no
二.9 记录文件来源和处理依据；七.1 不得虚构数据
    - source_id、evidence

需要标准化的字段（四.3、二.5）在这一层只保存原文（*_raw），由 normalize 模块转换，
保证"同时保存原始与标准值"。
"""

from __future__ import annotations

from pydantic import Field, model_validator

from .common import Amount, Evidence, StrictModel


class ExtractedCharge(StrictModel):
    """单个费用项（四.4 每个费用项生成一条记录）。"""

    line_no: int = Field(ge=1, description="费用在单据费用表中的行序号，从 1 开始；同名费用靠它区分")
    charge_name_raw: str = Field(min_length=1, description="单据上的原始费用名称")
    quantity: Amount | None = None
    unit_price: Amount | None = None
    amount: Amount | None = Field(default=None, description="金额；贷记、退款为负数")
    currency_raw: str | None = Field(default=None, description="该行币种原文；行上没有时留空")
    payment_term_raw: str | None = Field(default=None, description="付款方式原文，如 PP / CC / Prepaid / Collect")
    evidence: dict[str, Evidence] = Field(
        default_factory=dict,
        description="字段名 -> 原文证据，例如 {'amount': Evidence(...)}",
    )


class DocumentTotal(StrictModel):
    """小计、税额和总计。按币种分组，因为一张单据可能同时有 USD 和 CNY。"""

    currency_raw: str | None = None
    subtotal: Amount | None = None
    tax_amount: Amount | None = None
    total_amount: Amount | None = None


class ChangeInstruction(StrictModel):
    """输入材料中明确的改账要求，例如邮件正文"请将 Handling Fee 改为 USD 40"。"""

    target_reference_no: str | None = None
    target_charge_name_raw: str | None = None
    field: str = Field(description="要修改的字段，如 amount / currency / payment_term")
    new_value_raw: str
    evidence: Evidence


class ExtractedDocument(StrictModel):
    source_id: str = Field(description="来自哪个 RawDocument")

    # --- 单据信息
    document_type_raw: str | None = Field(default=None, description="单据标题原文，如 'DEBIT NOTE'、'INVOICE'")
    reference_no: str | None = Field(default=None, description="单据编号，如 Debit Note No.、Invoice No.")
    document_date_raw: str | None = Field(default=None, description="单据日期原文，不要改写格式")

    # --- 交易双方与收费方向
    issuer: str | None = Field(default=None, description="签发方")
    recipient: str | None = Field(default=None, description="接收方；单据上没有则为 null")
    payee: str | None = Field(default=None, description="单据写明的收款方，如 'BALANCE DUE TO X'、'REMIT TO X'")
    payer: str | None = Field(default=None, description="单据写明的付款方，如 'Payer: X'")

    # --- 匹配键
    bl_nos: list[str] = Field(default_factory=list, description="单据上所有提单号")
    booking_no: str | None = None
    container_nos: list[str] = Field(default_factory=list, description="单据上所有箱号")

    # --- 费用与金额
    currency_raw: str | None = Field(default=None, description="单据级币种；费用行没有币种时使用")
    charges: list[ExtractedCharge] = Field(default_factory=list)
    totals: list[DocumentTotal] = Field(default_factory=list)
    change_instructions: list[ChangeInstruction] = Field(default_factory=list)

    evidence: dict[str, Evidence] = Field(
        default_factory=dict,
        description="单据级字段名 -> 原文证据",
    )

    @model_validator(mode="after")
    def _check_consistency(self) -> "ExtractedDocument":
        # 1. 证据只能指向真实存在的字段，防止 LLM 编造字段名
        doc_fields = set(type(self).model_fields) - {"evidence", "charges", "totals", "change_instructions"}
        bad = set(self.evidence) - doc_fields
        charge_fields = set(ExtractedCharge.model_fields) - {"evidence"}
        for c in self.charges:
            bad |= set(c.evidence) - charge_fields
        if bad:
            raise ValueError(f"evidence 中存在未知字段: {sorted(bad)}")
        # 2. 行号唯一
        line_nos = [c.line_no for c in self.charges]
        if len(line_nos) != len(set(line_nos)):
            raise ValueError(f"费用行号重复: {line_nos}")
        return self
