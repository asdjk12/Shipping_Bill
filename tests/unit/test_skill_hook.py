"""结构化抽取 skill + 数据校验 hook：hook 识别 skill 调用、校验、带问题只补字段重抽、重试上限、在 agent 中生效。

LLM 用固定输出的替身（ScriptedLLM），检验的是流程逻辑。
"""

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from bill_agent.extract import GateStatus
from bill_agent.extract.extractor import LLMExtraction, LLMPatch
from bill_agent.extract.reviewer import LLMReview
from bill_agent.skills import SKILL_NAME, ValidationHook, build_extraction_skill, run_skill

from .test_extract import DN_TEXT, dn_output, doc
from .test_review import ScriptedLLM, all_ok, gate

GOOD, BAD = "MRSU2571577", "MRSU2571578"      # BAD 的校验位错误
TEXT = DN_TEXT + f"\\nCONTAINER NO. {GOOD}\\nOLD CNTR {BAD}"
FIX = {"fields": [{"document": 1, "field": "container_nos", "values": [GOOD], "quote": f"CONTAINER NO. {GOOD}"}]}


def skill(llm):
    return build_extraction_skill(gate(llm), lambda paths: [doc("d", TEXT)])


def run(llm, max_retries=3):
    msg = run_skill(skill(llm), {"paths": ["d.dat"]}, [ValidationHook(max_retries=max_retries)])
    [g] = msg.artifact
    return msg, g


def test_passes_without_retry():
    good = dn_output(container_nos=[GOOD])
    llm = ScriptedLLM([good], [all_ok(good)])
    msg, g = run(llm)
    assert g.status is GateStatus.PASSED and len(g.rounds) == 1
    assert g.rounds[0].validation.issues == []
    assert '"status": "passed"' in msg.content and len(llm.calls[LLMPatch]) == 0


def test_validation_error_repairs_only_the_failed_field():
    bad = dn_output(container_nos=[BAD])
    llm = ScriptedLLM([bad], [all_ok(bad)], patches=[FIX])
    msg, g = run(llm)
    assert g.status is GateStatus.PASSED and g.documents[0].container_nos == [GOOD]
    # 第一次整份抽取，校验退回后只补字段，不再整份重抽；重抽结果重新经过审核
    assert len(llm.calls[LLMExtraction]) == 1 and len(llm.calls[LLMPatch]) == 1 and len(llm.calls[LLMReview]) == 2
    prompt = llm.calls[LLMPatch][0][-1].content
    assert "数据校验问题" in prompt and BAD in prompt and "不要自行纠正" in prompt
    # 轮次记录保留：第 1 轮校验失败，第 2 轮通过
    assert len(g.rounds) == 2 and g.rounds[0].validation.errors and not g.rounds[1].validation.issues
    assert GOOD in msg.content


def test_gives_up_after_max_retries():
    bad = dn_output(container_nos=[BAD])
    llm = ScriptedLLM([bad], [all_ok(bad)])          # 补丁什么都不改
    msg, g = run(llm)
    assert g.status is GateStatus.NEEDS_REVIEW and len(llm.calls[LLMPatch]) == 3 and len(g.rounds) == 4
    assert g.reasons == [f"数据校验未通过（已重试 3 次）: [V1] 单据 1 container_nos: "
                         f"箱号 '{BAD}' 校验位应为 7，实际为 8，可能是 OCR 或原文错误"]
    assert '"status": "needs_review"' in msg.content

    llm = ScriptedLLM([bad], [all_ok(bad)])
    _, g = run(llm, max_retries=0)
    assert g.status is GateStatus.NEEDS_REVIEW and llm.calls[LLMPatch] == []


def test_structural_problem_triggers_full_reextraction():
    empty = dn_output(charges=[], totals=[])
    good = dn_output()
    llm = ScriptedLLM([empty, good], [all_ok(good)])
    _, g = run(llm)
    assert g.status is GateStatus.PASSED and len(g.documents[0].charges) == 2
    assert len(llm.calls[LLMExtraction]) == 2 and llm.calls[LLMPatch] == []
    assert "[V2]" in llm.calls[LLMExtraction][1][-1].content


def test_gate_failure_is_not_validated():
    bad = dn_output(reference_no="DC-000000")          # 代码核验不通过
    llm = ScriptedLLM([bad], [{}])
    _, g = run(llm)
    assert g.status is GateStatus.NEEDS_REVIEW and g.rounds[-1].validation is None


def test_warnings_are_kept_on_pass():
    good = dn_output()
    d = good["documents"][0]
    d["charges"][1].update(amount="65.00", unit_price="65.00")      # 两行 HANDLING CHARGE 65.00
    d["totals"] = [{"currency_raw": "USD", "total_amount": "130.00"}]
    text = DN_TEXT.replace("35.00", "65.00").replace("100.00", "130.00")
    sk = build_extraction_skill(gate(ScriptedLLM([good], [all_ok(good)])), lambda paths: [doc("d", text)])
    [g] = run_skill(sk, {"paths": ["d.dat"]}, [ValidationHook()]).artifact
    assert g.status is GateStatus.PASSED and len(g.warnings) == 1 and g.warnings[0].startswith("[V6]")


def test_hook_ignores_other_tools():
    @tool
    def echo(text: str) -> str:
        """回显。"""
        return text

    msg = run_skill(echo, {"text": "hi"}, [ValidationHook()])
    assert msg.content == "hi"


def test_repair_arg_is_hidden_from_the_model():
    assert set(skill(ScriptedLLM([{}], [{}])).tool_call_schema.model_json_schema()["properties"]) == {"paths"}


class ToolCallingFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


def test_hook_runs_inside_agent():
    bad = dn_output(container_nos=[BAD])
    llm = ScriptedLLM([bad], [all_ok(bad)], patches=[FIX])
    model = ToolCallingFake(messages=iter([
        AIMessage("", tool_calls=[{"name": SKILL_NAME, "args": {"paths": ["d.dat"]}, "id": "c1"}]),
        AIMessage("完成"),
    ]))
    agent = create_agent(model, tools=[skill(llm)], middleware=[ValidationHook(max_retries=3)])
    out = agent.invoke({"messages": [{"role": "user", "content": "抽取 d.dat"}]})
    [tm] = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert '"status": "passed"' in tm.content and GOOD in tm.content and BAD not in tm.content
    assert len(llm.calls[LLMPatch]) == 1
