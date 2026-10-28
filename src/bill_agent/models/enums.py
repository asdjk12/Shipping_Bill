"""枚举类型。

字段取值来源（test.pdf）：
- DocumentType：一、项目背景 "Debit Note、Credit Note、Invoice 等单据"
- PaymentTerm：四.2 "PP（预付）、CC（到付）等付款方式"
- Operation：六、主要字段说明 operation
- PostingType：四.6 应收应付判断
"""

from enum import Enum


class FileType(str, Enum):
    """三、输入数据 / 四.1 多格式文档解析。"""

    EML = "eml"
    PDF = "pdf"
    IMAGE = "image"          # JPG / PNG / TIFF
    EXCEL = "excel"          # XLS / XLSX
    WORD = "word"            # DOC / DOCX
    TEXT = "text"            # 邮件正文等纯文本
    BILL_JSON = "bill_json"  # 已存在的账单 JSON


class DocumentType(str, Enum):
    DEBIT_NOTE = "Debit Note"
    CREDIT_NOTE = "Credit Note"
    INVOICE = "Invoice"
    OTHER = "Other"          # 题目写"等单据"，无法归类时使用


class PaymentTerm(str, Enum):
    PP = "PP"                # Prepaid 预付
    CC = "CC"                # Collect 到付


class Operation(str, Enum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    NO_CHANGE = "NO_CHANGE"
    PENDING_REVIEW = "PENDING_REVIEW"


class DCDirection(str, Enum):
    """费用行的借贷方向。样例 DN 有 DEBIT(+) / CREDIT(-) 两列，方向按行而非按单据。"""

    DEBIT = "DEBIT"          # 签发方向接收方收费
    CREDIT = "CREDIT"        # 签发方向接收方退/付费


class PostingType(str, Enum):
    AR = "AR"                # 应收账款
    AP = "AP"                # 应付账款


class MatchRule(int, Enum):
    """四.5 更新已有账单的匹配规则，数值即优先级。"""

    REF_NO_AND_CHARGE = 1             # 单据编号 + 费用名称
    BL_NO_CHARGE_CURRENCY = 2         # 提单号 + 费用名称 + 币种
    BOOKING_NO_CHARGE_CURRENCY = 3    # 订舱号 + 费用名称 + 币种
    CONTAINER_PARTIES_DATE_AMOUNT = 4 # 箱号 + 交易双方 + 日期 + 金额
