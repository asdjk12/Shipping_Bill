"""LLM 抽取：分组、转换核验、带反馈重试、golden 对比。

LLM 用固定输出的替身，检验的是代码逻辑；真实模型的效果用
python -m bill_agent extract-eval 跑（调用 DeepSeek）。
"""

from pathlib import Path

from langchain_core.runnables import RunnableLambda

from bill_agent.extract import Extractor, group_documents
from bill_agent.extract.compare import compare
from bill_agent.extract.extractor import (EVIDENCE_REQUIRED, LLMExtraction, LLMPatch, anchored_evidence,
                                           apply_patch, convert, has_support, payment_term_problem)
from bill_agent.models import DocStatus, ExtractedDocument, FileType, RawDocument

SAMPLES = Path(__file__).parents[1] / "fixtures" / "samples"

DN_TEXT = """DEBIT NOTE
D/C No. : DC-108478
D/C DATE  10-24-2026        TOTAL AMOUNT 100.00  CURRENCY USD
MASTER B/L NO. : MAEU275752922
DESCRIPTION              UNIT    RATE   QTY P/C     DEBIT(+)
HANDLING CHARGE           -    65.00  1.00  C      65.00
HANDLING CHARGE           -    35.00  1.00  C      35.00"""

MAIL_TEXT = "Subject: PRE-ALERT\nMBL NO.:OOLU4055331281\nSHA pay TAR：H/C $50"


def doc(sid, text, file_type=FileType.PDF, parent=None, path=None) -> RawDocument:
    status = DocStatus.PARSED if file_type in (FileType.EML, FileType.EXCEL) else DocStatus.OCR_DONE
    return RawDocument(source_id=sid, source_path=path or f"{sid}.dat", parent_id=parent, file_type=file_type,
                       status=status, text=text, ocr_engine=None if status is DocStatus.PARSED else "pdf-text-layer")


def dn_output(**over) -> dict:
    d = {
        "source_doc": 1, "document_type_raw": "DEBIT NOTE", "reference_no": "DC-108478",
        "document_date_raw": "10-24-2026", "bl_nos": ["MAEU275752922"], "currency_raw": "USD",
        "charges": [
            {"line_no": 1, "charge_name_raw": "HANDLING CHARGE", "quantity": "1.00", "unit_price": "65.00",
             "amount": "65.00", "payment_term_raw": "C", "evidence": [{"field": "amount", "quote": "C      65.00"}]},
            {"line_no": 2, "charge_name_raw": "HANDLING CHARGE", "quantity": "1.00", "unit_price": "35.00",
             "amount": "35.00", "payment_term_raw": "C"},
        ],
        "totals": [{"currency_raw": "USD", "total_amount": "100.00"}],
        "evidence": [{"field": "reference_no", "quote": "D/C No. : DC-108478"},
                     {"field": "document_type_raw", "quote": "DEBIT NOTE"},
                     {"field": "document_date_raw", "quote": "D/C DATE  10-24-2026"},
                     {"field": "currency_raw", "quote": "CURRENCY USD"}],
    }
    d.update(over)
    return {"documents": [d]}


class FakeLLM:
    """按顺序返回预设输出（整份抽取和补问题共用一个序列），记录每次收到的消息和输出结构。"""

    def __init__(self, *outputs: dict):
        self.outputs = list(outputs)
        self.calls = []
        self.schemas = []

    def with_structured_output(self, schema, include_raw=False, **kw):
        assert schema in (LLMExtraction, LLMPatch) and include_raw

        def run(messages):
            self.calls.append(messages)
            self.schemas.append(schema)
            out = self.outputs[min(len(self.calls), len(self.outputs)) - 1]
            if out is None:                 # 模拟模型没有按结构输出
                return {"raw": "随便说点什么", "parsed": None, "parsing_error": ValueError("not json")}
            return {"raw": None, "parsed": schema.model_validate(out), "parsing_error": None}
        return RunnableLambda(run)


# ---------- 分组 ----------

def test_group_documents_by_upload():
    mail = doc("m", MAIL_TEXT, FileType.EML)
    att = doc("a", "BILL OF LADING", parent="m")
    pending = RawDocument(source_id="p", source_path="p.png", file_type=FileType.IMAGE, parent_id="m",
                          status=DocStatus.PENDING_OCR, file_ref="x")
    dn = doc("d", DN_TEXT)
    groups = group_documents([mail, att, pending, dn])
    assert [[d.source_id for d in g] for g in groups] == [["m", "a"], ["d"]]   # 未完成 OCR 的不参与


# ---------- 转换与核验 ----------

def test_convert_ok():
    docs, errors = convert(LLMExtraction.model_validate(dn_output()), [doc("d", DN_TEXT)])
    assert errors == []
    [d] = docs
    assert d.source_id == "d" and d.reference_no == "DC-108478"
    assert [str(c.amount) for c in d.charges] == ["65.00", "35.00"]
    assert str(d.totals[0].total_amount) == "100.00"
    assert d.evidence["reference_no"].source_id == "d"


def test_convert_evidence_source_resolved_across_group():
    mail = doc("m", MAIL_TEXT, FileType.EML)
    att = doc("a", "B/L NO: OOLU4055331281 CONTAINER TIIU8975975", parent="m")
    out = {"documents": [{
        "source_doc": 1, "payer": "SHA", "payee": "TAR", "bl_nos": ["OOLU4055331281"],
        "container_nos": ["TIIU8975975"],
        "charges": [{"line_no": 1, "charge_name_raw": "H/C", "amount": "50", "currency_raw": "$"}],
        "evidence": [{"field": "container_nos", "quote": "CONTAINER TIIU8975975"},
                     {"field": "payee", "quote": "SHA pay TAR"}],
    }]}
    [d], errors = convert(LLMExtraction.model_validate(out), [mail, att])
    assert errors == []
    assert d.source_id == "m"
    assert d.evidence["container_nos"].source_id == "a"      # 证据来自附件
    assert d.evidence["payee"].source_id == "m"


def test_convert_dedupes_numbers_keeping_order():
    text = DN_TEXT + "\nB/L CMDUCHN4053601 / CMDU CHN4053601"
    out = dn_output(bl_nos=["MAEU275752922", "CMDUCHN4053601", "CMDU CHN4053601", "maeu275752922"])
    [d], errors = convert(LLMExtraction.model_validate(out), [doc("d", text)])
    assert errors == [] and d.bl_nos == ["MAEU275752922", "CMDUCHN4053601"]


def test_convert_reports_fabrication():
    bad = dn_output(reference_no="DC-999999", evidence=[{"field": "reference_no", "quote": "Invoice No. 1"}])
    bad["documents"][0]["charges"][0] |= {"charge_name_raw": "THC", "amount": "120.00"}
    docs, errors = convert(LLMExtraction.model_validate(bad), [doc("d", DN_TEXT)])
    text = "\n".join(errors)
    assert "reference_no='DC-999999' 在原文中找不到" in text
    assert "证据在原文中找不到" in text
    assert "费用名 'THC'" in text and "amount='120.00'" in text
    assert len(docs) == 1          # 能通过模型校验的单据仍然保留，问题记入 errors


def test_convert_rejects_ocr_confusion_and_bad_source_doc():
    text = DN_TEXT.replace("65.00  1.00  C      65.00", "65.00  1.00  C      6S.00")
    bad = dn_output()
    bad["documents"][0]["charges"][0]["amount"] = "6S.00"
    docs, errors = convert(LLMExtraction.model_validate(bad), [doc("d", text)])
    assert docs == [] and any("charges.0.amount" in e for e in errors)   # 金额含字母，模型校验拒绝

    _, errors = convert(LLMExtraction.model_validate(dn_output(source_doc=3)), [doc("d", DN_TEXT)])
    assert "超出文档编号范围" in errors[0]


def test_convert_checks_every_verbatim_field():
    bad = dn_output(issuer="ACME LOGISTICS", currency_raw="EUR")
    bad["documents"][0]["charges"][0]["payment_term_raw"] = "PP"
    bad["documents"][0]["totals"][0]["subtotal"] = "999.00"
    _, errors = convert(LLMExtraction.model_validate(bad), [doc("d", DN_TEXT)])
    text = "\n".join(errors)
    assert "issuer='ACME LOGISTICS'" in text and "currency_raw='EUR'" in text
    assert "payment_term_raw='PP'" in text and "subtotal='999.00'" in text


def test_convert_number_lookup_needs_the_whole_number():
    bad = dn_output()
    bad["documents"][0]["charges"][0]["amount"] = "5.00"      # 只是 "65.00" 的一部分
    _, errors = convert(LLMExtraction.model_validate(bad), [doc("d", DN_TEXT)])
    assert any("amount='5.00' 在文档 1 中找不到" in e for e in errors)


def test_convert_reports_arithmetic_mismatch():
    bad = dn_output()
    bad["documents"][0]["totals"] = [{"currency_raw": "USD", "total_amount": "65.00"}]
    [d], errors = convert(LLMExtraction.model_validate(bad), [doc("d", DN_TEXT)])
    assert errors == ["单据 1: 合计[USD]: 加总 100.00 + 税额 0 与总计 65.00 不一致"]
    assert d.reference_no == "DC-108478"      # 单据保留，问题记入 errors


def test_convert_checks_cargo_parties():
    bad = dn_output(shipper="NOT IN TEXT", carrier="DC-108478")
    _, errors = convert(LLMExtraction.model_validate(bad), [doc("d", DN_TEXT)])
    assert any("shipper='NOT IN TEXT'" in e for e in errors)
    assert not any("carrier" in e for e in errors)


def test_payment_term_rejects_non_terms():
    assert "不是付款方式" in payment_term_problem("TT", "P/C")
    for term in ("C", "p", "PP", "cc", "Prepaid", "COLLECT", "到付", "Freight Prepaid"):
        assert payment_term_problem(term, "P/C") is None


def test_payment_term_single_letter_needs_no_basis_column_conflict():
    zim = "Base | Rate | Curr | Amount\nWHARFAGE | C | 5.00 | USD | 5.00"
    assert "计费基准列" in payment_term_problem("C", zim)
    assert payment_term_problem("C", "UNIT RATE QTY P/C DEBIT") is None       # DN：有付款方式列
    assert payment_term_problem("C", "CUR AMOUNT TYPE\nUSD 245.0000 C") is None   # CMA：列名不固定也不拦
    assert payment_term_problem("Prepaid", zim) is None                       # 写全的付款方式不受影响


def test_convert_rejects_basis_column_as_payment_term():
    text = DN_TEXT.replace("P/C", "BASE")
    _, errors = convert(LLMExtraction.model_validate(dn_output()), [doc("d", text)])
    assert any("计费基准列" in e for e in errors)


def test_has_support_allows_values_spanning_lines():
    assert has_support("ACME LLC", "BILL TO: ACME LLC")
    assert has_support("ACME LLC As Agents for BETA", "ACME LLC")     # 值跨两行，摘录是其中一行
    assert not has_support("ACME LLC", "CONSIGNEE :")
    assert not has_support("ACME LLC", "")


def test_convert_clears_every_document_field_without_evidence():
    values = {name: "DC-108478" for name in EVIDENCE_REQUIRED if name != "recipient"}
    _, errors = convert(LLMExtraction.model_validate(dn_output(**values, evidence=[])), [doc("d", DN_TEXT)],
                        clear_unsupported=False)
    assert len(errors) == len(values)                  # 重试期间退回，不清空
    [d], errors = convert(LLMExtraction.model_validate(dn_output(**values, evidence=[])), [doc("d", DN_TEXT)])
    assert errors == []
    assert all(getattr(d, name) is None for name in EVIDENCE_REQUIRED)

    kept = [{"field": name, "quote": "D/C No. : DC-108478"} for name in values]
    [d], _ = convert(LLMExtraction.model_validate(dn_output(**values, evidence=kept)), [doc("d", DN_TEXT)])
    assert all(getattr(d, name) == "DC-108478" for name in values)


def test_missing_evidence_is_retried_before_clearing():
    no_evidence = dn_output(issuer="MAEU275752922")
    fix = {"fields": [{"document": 1, "field": "issuer", "value": "MAEU275752922",
                       "quote": "MASTER B/L NO. : MAEU275752922"}]}

    llm = FakeLLM(no_evidence, fix)
    r = Extractor(llm, max_retries=2).extract_group([doc("d", DN_TEXT)])
    assert r.attempts == 2 and r.errors == [] and r.cleared == []
    assert r.attempt_modes == ["full", "patch"]       # 第二次只补证据，不重新整份抽取
    assert r.documents[0].issuer == "MAEU275752922"    # 模型补了证据，值保留
    assert "没有包含该值的证据摘录" in llm.calls[1][-1].content

    llm = FakeLLM(no_evidence, {})
    r = Extractor(llm, max_retries=1).extract_group([doc("d", DN_TEXT)])
    assert r.attempts == 2 and r.errors == []          # 重试用完才清空
    assert r.documents[0].issuer is None and r.cleared == ["单据 1 issuer='MAEU275752922'"]


# 邮件附件 MBL 的多栏排版：标签和值分在上下两行
MBL_TEXT = """SHIPPER                                       BOOKING NUMBER   B/L NO:
SUN TRACK EXPRESS (CHINA) CO., LTD.           4055331281       HZMHK331281
ROOM 1201, SHENZHEN
CONSIGNEE
ACME LOGISTICS LLC
FREIGHT COLLECT                               SHIPPED ON BOARD"""


def test_anchored_evidence_label_above_value():
    g = [doc("m", MBL_TEXT)]
    # 模型把两行拼成一句：原文里没有这句，但标签在值的上一行
    ev = anchored_evidence("4055331281", "BOOKING NUMBER 4055331281", g)
    assert ev and ev["source_id"] == "m"
    assert ev["quote"].startswith("SHIPPER") and "4055331281" in ev["quote"]   # 证据是原文的连续两行
    # 只引用了标签行
    assert anchored_evidence("4055331281", "BOOKING NUMBER", g)
    assert anchored_evidence("ACME LOGISTICS LLC", "CONSIGNEE", g)


def test_anchored_evidence_rejects_label_not_near_value():
    g = [doc("m", MBL_TEXT)]
    assert anchored_evidence("4055331281", "VESSEL NAME 4055331281", g) is None      # 标签原文没有
    assert anchored_evidence("4055331281", "SHIPPED ON BOARD", g) is None            # 标签在值下面
    assert anchored_evidence("ACME LOGISTICS LLC", "BOOKING NUMBER", g) is None      # 隔了 4 行
    assert anchored_evidence("9999999999", "BOOKING NUMBER 9999999999", g) is None   # 值不在原文
    assert anchored_evidence("4055331281", "4055331281", g) is None                  # 没有标签


def test_convert_accepts_stitched_quote_without_retry():
    mail = doc("m", MAIL_TEXT, FileType.EML)
    mbl = doc("a", MBL_TEXT, parent="m")
    out = {"documents": [{
        "source_doc": 1, "payer": "SHA", "payee": "TAR", "booking_no": "4055331281", "consignee": "ACME LOGISTICS LLC",
        "charges": [{"line_no": 1, "charge_name_raw": "H/C", "amount": "50", "currency_raw": "$"}],
        "evidence": [{"field": "payee", "quote": "SHA pay TAR"}, {"field": "payer", "quote": "SHA pay TAR"},
                     {"field": "booking_no", "quote": "BOOKING NUMBER 4055331281"},   # 拼接
                     {"field": "consignee", "quote": "CONSIGNEE"}],                   # 只有标签
    }]}
    [d], errors = convert(LLMExtraction.model_validate(out), [mail, mbl], clear_unsupported=False)
    assert errors == []                              # 不再退回模型
    assert d.booking_no == "4055331281" and d.consignee == "ACME LOGISTICS LLC"
    assert d.evidence["booking_no"].source_id == "a"
    assert "4055331281" in d.evidence["booking_no"].quote


def test_convert_clears_cargo_parties_without_evidence():
    text = DN_TEXT + "\nSHIPPER : YIWU CO\nCONSIGNEE : AEBO LLC"
    out = dn_output(shipper="YIWU CO", consignee="AEBO LLC")
    out["documents"][0]["evidence"] += [{"field": "shipper", "quote": "SHIPPER : YIWU CO"},
                                        {"field": "consignee", "quote": "D/C No. : DC-108478"}]
    [d], errors = convert(LLMExtraction.model_validate(out), [doc("d", text)])
    assert errors == []
    assert d.shipper == "YIWU CO"     # 证据包含值
    assert d.consignee is None        # 证据不含值，值附近也没有这段文字，等于没有证据

    out = dn_output(shipper="YIWU CO", consignee="AEBO LLC")
    [d], errors = convert(LLMExtraction.model_validate(out), [doc("d", text)])
    assert errors == [] and d.shipper is None and d.consignee is None

    out = dn_output(carrier="MAEU275752922")         # 送货人同样要有证据
    [d], _ = convert(LLMExtraction.model_validate(out), [doc("d", text)])
    assert d.carrier is None
    out["documents"][0]["evidence"].append({"field": "carrier", "quote": "MASTER B/L NO. : MAEU275752922"})
    [d], _ = convert(LLMExtraction.model_validate(out), [doc("d", text)])
    assert d.carrier == "MAEU275752922"


def test_convert_reuses_evidence_of_another_field():
    # carrier 的摘录只有公司名的一部分，但 issuer 的证据已经包含完整的公司名
    text = DN_TEXT + "\nREMIT TO\nACME SHIPPING (AMERICA) LLC\ncharges are payable to ACME SHIPPING"
    out = dn_output(issuer="ACME SHIPPING (AMERICA) LLC", carrier="ACME SHIPPING (AMERICA) LLC")
    out["documents"][0]["evidence"] += [{"field": "issuer", "quote": "ACME SHIPPING (AMERICA) LLC"},
                                        {"field": "carrier", "quote": "charges are payable to ACME SHIPPING"}]
    [d], errors = convert(LLMExtraction.model_validate(out), [doc("d", text)], clear_unsupported=False)
    assert errors == [] and d.carrier == "ACME SHIPPING (AMERICA) LLC"
    assert d.evidence["carrier"].quote == "ACME SHIPPING (AMERICA) LLC"


def test_company_name_punctuation_ignored():
    # "CO.LLC" 与 "Co., LLC" 视为相同
    text = DN_TEXT + "\nZIMAMERICAN SERVICESCO.LLC AS AGENTS"
    assert has_support("ZIM AMERICAN SERVICES Co., LLC", "ZIMAMERICAN SERVICESCO.LLC AS AGENTS")
    out = dn_output(issuer="ZIM AMERICAN SERVICES Co., LLC")
    out["documents"][0]["evidence"].append({"field": "issuer", "quote": "ZIMAMERICAN SERVICESCO.LLC AS AGENTS"})
    [d], errors = convert(LLMExtraction.model_validate(out), [doc("d", text)], clear_unsupported=False)
    assert errors == [] and d.issuer == "ZIM AMERICAN SERVICES Co., LLC"
    assert compare(d, d.model_copy(update={"issuer": "ZIM AMERICAN SERVICES CO.LLC"}))[1] == []


def test_convert_flags_booking_label_without_value():
    mail = doc("m", MAIL_TEXT, FileType.EML)
    mbl = doc("a", MBL_TEXT, parent="m")
    out = {"documents": [{
        "source_doc": 1, "payer": "SHA", "payee": "TAR",
        "charges": [{"line_no": 1, "charge_name_raw": "H/C", "amount": "50", "currency_raw": "$"}],
        "evidence": [{"field": "payee", "quote": "SHA pay TAR"}, {"field": "payer", "quote": "SHA pay TAR"}],
    }]}
    _, errors = convert(LLMExtraction.model_validate(out), [mail, mbl])
    assert len(errors) == 1 and errors[0].kind == "field" and errors[0].field == "booking_no"
    assert "BOOKING NUMBER" in errors[0]
    # 补问题的轮次不再检查：模型核实后仍填 null 就接受
    assert convert(LLMExtraction.model_validate(out), [mail, mbl], check_omissions=False)[1] == []
    # 原文没有订舱号标签时不报
    assert convert(LLMExtraction.model_validate(dn_output()), [doc("d", DN_TEXT)])[1] == []


def test_convert_rejects_same_issuer_and_recipient():
    same = dn_output(issuer="DC-108478", recipient="dc-108478")
    same["documents"][0]["evidence"] += [{"field": "issuer", "quote": "DC-108478"},
                                         {"field": "recipient", "quote": "DC-108478"}]
    _, errors = convert(LLMExtraction.model_validate(same), [doc("d", DN_TEXT)])
    assert any("单据不会开给自己" in e for e in errors)


# ---------- 重试 ----------

def test_retry_with_feedback_then_pass():
    bad = dn_output(reference_no="DC-000000")
    fix = {"fields": [{"document": 1, "field": "reference_no", "value": "DC-108478",
                       "quote": "D/C No. : DC-108478"}]}
    llm = FakeLLM(bad, fix)
    r = Extractor(llm, max_retries=2).extract_group([doc("d", DN_TEXT)])
    assert r.attempts == 2 and r.errors == []
    assert llm.schemas == [LLMExtraction, LLMPatch] and r.documents[0].reference_no == "DC-108478"
    assert len(r.raw_outputs) == 2
    assert len(r.attempt_errors) == 2 and r.attempt_errors[1] == []
    assert any("DC-000000" in e for e in r.attempt_errors[0])
    retry_msg = llm.calls[1][-1].content
    assert "DC-000000" in retry_msg and "在原文中找不到" in retry_msg


def test_unparsed_output_is_retried():
    llm = FakeLLM(None, dn_output())
    r = Extractor(llm).extract_group([doc("d", DN_TEXT)])
    assert r.attempts == 2 and r.errors == [] and len(r.documents) == 1
    assert "没有按要求的结构输出" in r.attempt_errors[0][0]
    assert r.raw_outputs[0] == {"unparsed": "随便说点什么"}


def test_retry_exhausted_keeps_errors():
    bad = dn_output(reference_no="DC-000000")
    r = Extractor(FakeLLM(bad), max_retries=1).extract_group([doc("d", DN_TEXT)])
    assert r.attempts == 2 and any("DC-000000" in e for e in r.errors)


def test_feedback_stays_through_inner_retries():
    bad = dn_output(source_doc=5)        # 结构性问题，整份重新抽取
    llm = FakeLLM(bad, dn_output())
    Extractor(llm).extract_group([doc("d", DN_TEXT)], ["审核意见 A"], {"documents": []})
    assert llm.schemas == [LLMExtraction, LLMExtraction]
    assert "审核意见 A" in llm.calls[0][-1].content
    assert "审核意见 A" in llm.calls[1][-1].content and "超出文档编号范围" in llm.calls[1][-1].content


def test_price_mismatch_only_rechecks_prices():
    # 费用行加总与合计不一致：只让模型复核价格，费用名等其他内容不动
    bad = dn_output()
    bad["documents"][0]["charges"][1]["amount"] = "65.00"
    bad["documents"][0]["charges"][1]["unit_price"] = "65.00"
    fix = {"prices": [{"document": 1, "charges": [
        {"line_no": 1, "quantity": "1.00", "unit_price": "65.00", "amount": "65.00"},
        {"line_no": 2, "quantity": "1.00", "unit_price": "35.00", "amount": "35.00"}]}]}
    llm = FakeLLM(bad, fix)
    r = Extractor(llm).extract_group([doc("d", DN_TEXT)])
    assert r.errors == [] and r.attempt_modes == ["full", "patch"]
    assert all(e.kind == "price" for e in r.attempt_errors[0])
    assert [str(c.amount) for c in r.documents[0].charges] == ["65.00", "35.00"]
    assert r.documents[0].charges[0].evidence["amount"].quote == "C      65.00"   # 没给新证据时保留原证据


def test_apply_patch_only_touches_listed_fields():
    prev = dn_output(issuer="X")
    patched = apply_patch(prev, LLMPatch.model_validate({"fields": [
        {"document": 1, "field": "issuer", "value": None},
        {"document": 1, "field": "bl_nos", "values": ["A1", "B2"], "quote": "A1"},
        {"document": 1, "field": "charges", "value": "忽略"},          # 不允许改的字段
        {"document": 9, "field": "issuer", "value": "忽略"}]}))        # 不存在的单据
    d = patched["documents"][0]
    assert d["issuer"] is None and d["bl_nos"] == ["A1", "B2"]
    assert {"field": "bl_nos", "quote": "A1"} in d["evidence"]
    assert d["charges"] == prev["documents"][0]["charges"] and prev["documents"][0]["issuer"] == "X"


def test_totals_evidence_can_be_patched():
    bad = dn_output()
    bad["documents"][0]["evidence"].append({"field": "totals", "quote": "TOTAL USD 100.00"})   # 原文没有这句
    fix = {"fields": [{"document": 1, "field": "totals", "quote": "TOTAL AMOUNT 100.00"}]}
    llm = FakeLLM(bad, fix)
    r = Extractor(llm).extract_group([doc("d", DN_TEXT)])
    assert r.errors == [] and r.attempt_modes == ["full", "patch"]
    d = r.documents[0]
    assert d.evidence["totals"].quote == "TOTAL AMOUNT 100.00" and str(d.totals[0].total_amount) == "100.00"


def test_extract_no_bills():
    r = Extractor(FakeLLM({"documents": []})).extract([doc("a", "BILL OF LADING")])
    assert len(r) == 1 and r[0].documents == [] and r[0].errors == []


# ---------- golden 对比 ----------

def test_compare_with_golden():
    golden = ExtractedDocument.model_validate_json(
        (SAMPLES / "DN_DC-108478.expected_extraction.json").read_text(encoding="utf-8"))
    n, diffs = compare(golden, golden)
    assert n > 20 and diffs == []

    data = golden.model_dump(mode="json")
    data["charges"][2]["amount"] = 36
    data["bl_nos"] = list(reversed(data["bl_nos"]))       # 顺序不同不算差异
    data["issuer"] = "  neptune shipping limited "     # 空白和大小写不算差异
    _, diffs = compare(golden, ExtractedDocument.model_validate(data))
    assert [d.field for d in diffs] == ["charges[3].amount"]
