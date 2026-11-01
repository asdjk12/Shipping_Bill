"""LLM 抽取结果：ExtractedDocument / ExtractedCharge。

这是 LLM with_structured_output 的目标 schema，字段对应：

四.2 航运业务信息提取
    - 单据类型                  -> document_type_raw
    - 单据编号及日期            -> reference_no, document_date_raw
    - 签发方和接收方            -> issuer, recipient
    - 费用名称                  -> ExtractedCharge.charge_name_raw
    - 数量、单价和金额          -> ExtractedCharge.quantity / unit_price / amount
    - 币种                      -> ExtractedCharge.currency_raw（行上没有时取 currency_raw）
    - PP、CC 等付款方式         -> ExtractedCharge.payment_term_raw
    - 小计、税额和总计          -> totals
四.5 更新已有账单（匹配键）
    - 提单号 / 订舱号 / 箱号    -> master_bl_no, house_bl_no / booking_no / container_nos
    - 只修改明确要求变更的字段  -> change_instructions
四.6 应收应付判断（收费方向）
    - payee / payer、ExtractedCharge.dc_direction

根据样例补充的字段（见 tests/fixtures/samples/）：
    - 样例 DN：Master/House 两个提单号、两个箱号、两行同名 HANDLING CHARGE（需 line_no 区分）、
      DEBIT/CREDIT 分列、收件方为空但写明 "BALANCE DUE TO ..."（payee）、TERMS: COD（credit_terms_raw）、
      行上无币种仅表头有 CURRENCY。
    - 样例 ZIM Invoice："All payments must be issued to ..."（payee）、R.O.E. 汇率与本币金额、
      Place of payment、日期写法 10/03/2025 存在 MM/DD 与 DD/MM 歧义（只存原文）。
    - 样例 CMA CGM General Notice：没有单据编号（只能靠 B/L 匹配）、费用代码 OCA17、
      "REMIT TO"（payee）；正文条款里的 $25 / $150 / $1100 是费率说明，不是本单费用。

需要标准化的字段（四.3、二.5）在这一层只保存原文（*_raw），
由 normalize 模块转换，保证"同时保存原始与标准值"。

注意：单据上的银行账号、路由号等敏感信息不在 schema 中，禁止抽取。
"""

from __future__ import annotations

from pydantic import Field, model_validator

from .common import Amount, Evidence, StrictModel
from .enums import DCDirection


class ExtractedCharge(StrictModel):
    """单个费用项（四.4 每个费用项生成一条记录）。"""

    line_no: int = Field(ge=1, description="费用在单据费用表中的行序号，从 1 开始；同名费用靠它区分")
    charge_name_raw: str = Field(min_length=1, description="单据上的原始费用名称（被截断时也原样保留）")
    charge_code_raw: str | None = Field(default=None, description="费用代码，如 CMA CGM 的 'OCA17'")
    basis_raw: str | None = Field(
        default=None, description="计费基准列原文（如 ZIM 的 Base 列 'C'、'PTT'，DN 的 UNIT 列 '-'），含义不解释，原样保存"
    )
    quantity: Amount | None = None
    unit_price: Amount | None = Field(default=None, description="单价 / Rate")
    amount: Amount | None = Field(default=None, description="原币金额，取绝对值，方向见 dc_direction")
    currency_raw: str | None = Field(default=None, description="该行原币币种原文；行上没有时留空")
    dc_direction: DCDirection | None = Field(
        default=None, description="金额所在列：DEBIT(+) 或 CREDIT(-)；单据没有借贷分列时留空"
    )
    payment_term_raw: str | None = Field(default=None, description="P/C、PP/CC、Prepaid/Collect 列原文")
    exchange_rate: Amount | None = Field(default=None, description="R.O.E. / 汇率")
    local_amount: Amount | None = Field(default=None, description="按汇率折算后的金额")
    local_currency_raw: str | None = None
    container_no: str | None = Field(default=None, description="该费用对应的箱号（如有）")
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
    total_debit: Amount | None = Field(default=None, description="借方合计（有借贷分列时）")
    total_credit: Amount | None = Field(default=None, description="贷方合计（有借贷分列时）")
    paid_amount: Amount | None = None
    balance_due: Amount | None = Field(default=None, description="应付余额 / Grand Total Balance Due")


class ReferenceNo(StrictModel):
    """其他业务编号，如 OUR FILE NO.、AGENT FILE NO.、Your ref。"""

    label: str = Field(description="单据上的标签原文")
    value: str


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
    document_type_raw: str | None = Field(default=None, description="单据标题原文，如 'DEBIT NOTE'、'DEBIT INVOICE'")
    reference_no: str | None = Field(default=None, description="单据编号，如 D/C No.、Invoice No.")
    document_date_raw: str | None = Field(default=None, description="单据日期原文，不要改写格式")
    due_date_raw: str | None = None

    # --- 交易双方与收费方向
    issuer: str | None = Field(default=None, description="签发方（抬头公司）")
    recipient: str | None = Field(default=None, description="接收方（Bill To / To）；单据上为空则为 null")
    payee: str | None = Field(
        default=None,
        description="单据写明的收款方，如 'BALANCE DUE TO X'、'payments must be issued to X'、Beneficiary",
    )
    payer: str | None = Field(default=None, description="单据写明的付款方，如 'Payer: X'")

    # --- 匹配键
    master_bl_no: str | None = Field(default=None, description="Master B/L；只有一个 B/L 时填这里")
    house_bl_no: str | None = None
    booking_no: str | None = None
    container_nos: list[str] = Field(default_factory=list, description="单据上所有箱号")
    other_references: list[ReferenceNo] = Field(default_factory=list)

    # --- 运输信息（辅助人工复核与 PP/CC 交叉校验）
    vessel_voyage: str | None = None
    port_of_loading: str | None = None
    port_of_discharge: str | None = None
    place_of_payment: str | None = None

    # --- 金额与条款
    currency_raw: str | None = Field(default=None, description="单据级币种；费用行没有币种列时使用")
    credit_terms_raw: str | None = Field(
        default=None, description="结算条款，如 'COD'、'PAY IMMEDIATELY'、'30 DAYS'；不是 PP/CC"
    )
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
