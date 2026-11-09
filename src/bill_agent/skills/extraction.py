"""结构化抽取 skill：给 agent 调用的工具。输入文件路径，输出每组文件（一个上传文件及其附件）的抽取结果。

    skill = build_extraction_skill(gate, load)          # load: 路径 -> 解析 + OCR 后的 RawDocument
    agent = create_agent(model, tools=[skill], middleware=[ValidationHook()])

skill 内部是 ReviewGate（抽取 + 代码核验 + LLM 审核）。数据校验不在 skill 里，由 ValidationHook
在 skill 返回后运行：hook 按工具名识别这次调用是不是结构化抽取，调用时机就是校验时机。

工具返回 content_and_artifact：
- content：给 agent 看的 JSON（每组的状态、理由、校验提示和抽取出的单据）
- artifact：list[GateResult]，hook 和 CLI 用它拿到完整的轮次记录

重抽要求（repair）不放在工具参数里：agent 的 ToolNode 不让模型调用携带隐藏参数，模型也不该看到它。
hook 在重试前用 repairing(repair) 把它放进 contextvar，skill 执行时读取：
    {组的第一个 source_path: {"problems": [{"text", "kind", "doc", "field"}], "previous": 上一次的完整输出}}
有 repair 时只处理其中列出的组，并且第一轮只让模型补这些问题。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from langchain_core.tools import StructuredTool

from bill_agent.extract import GateResult, ReviewGate, group_documents
from bill_agent.extract.extractor import Problem
from bill_agent.models import RawDocument

SKILL_NAME = "structured_extraction"

SKILL_DESCRIPTION = (
    "结构化抽取：从航运账单文件（EML / PDF / 图片 / Excel / Word）中抽取单据信息和费用明细，"
    "包括单据类型、编号、日期、交易双方、提单号、订舱号、箱号、费用行和合计。"
    "每个值都附原文证据。返回每组文件的状态：passed 表示可以进入下一步，needs_review 表示需要人工复核。"
)


_REPAIR: ContextVar[dict | None] = ContextVar("extraction_repair", default=None)


@contextmanager
def repairing(repair: dict) -> Iterator[None]:
    """在这个范围内调用 skill 时，只重抽 repair 中列出的组。"""
    token = _REPAIR.set(repair)
    try:
        yield
    finally:
        _REPAIR.reset(token)


def group_key(source_paths: list[str]) -> str:
    """一组文件的标识：最顶层文件的 source_path。"""
    return source_paths[0]


def render(results: list[GateResult]) -> str:
    """给 agent 看的结果。"""
    return json.dumps([{
        "source_paths": g.source_paths,
        "status": g.status.value,
        "reasons": [str(r) for r in g.reasons],
        "warnings": g.warnings,
        "documents": [d.model_dump(mode="json") for d in g.documents],
    } for g in results], ensure_ascii=False)


def build_extraction_skill(gate: ReviewGate, load: Callable[[list[str]], list[RawDocument]]) -> StructuredTool:
    loaded: dict[tuple[str, ...], list[RawDocument]] = {}   # hook 重试时不重复解析和 OCR

    def structured_extraction(paths: list[str]) -> tuple[str, list[GateResult]]:
        """paths：要抽取的文件路径。"""
        repair = _REPAIR.get()
        key = tuple(paths)
        if key not in loaded:
            loaded[key] = load(list(paths))
        results = []
        for group in group_documents(loaded[key]):
            gk = group_key([d.source_path for d in group])
            if repair is None:
                results.append(gate.run_group(group))
            elif gk in repair:
                fix = repair[gk]
                problems = [Problem(p["text"], p["kind"], p.get("doc"), p.get("field")) for p in fix["problems"]]
                results.append(gate.run_group(group, problems, fix["previous"]))
        return render(results), results

    return StructuredTool.from_function(
        structured_extraction,
        name=SKILL_NAME,
        description=SKILL_DESCRIPTION,
        response_format="content_and_artifact",
    )
