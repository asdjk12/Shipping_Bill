"""数据校验 hook：agent 调用结构化抽取 skill 后，对结果做最后一道校验（validate.Validator）。

    agent = create_agent(model, tools=[extraction_skill], middleware=[ValidationHook(max_retries=3)])

实现为 LangChain 的 AgentMiddleware.wrap_tool_call：agent 每次调用工具都会经过这里。
- 工具名不是结构化抽取：直接放行，不做任何事
- 是结构化抽取：先执行 skill，再校验 skill 判为 passed 的每组结果
  - 没有 error：通过，warning 写进 GateResult.warnings
  - 有 error：把问题（kind = field / price / other）和上一次的完整输出交回 skill（extraction.repairing），
    只重抽有问题的组；问题都落在具体字段或价格上时，模型只补这些字段。最多重试 max_retries 次
  - 重试用完仍有 error：该组转 needs_review，理由写明校验问题
skill 判为 needs_review 的组（代码核验或审核没通过）不校验，原样返回。

所以 hook 不关心 skill 是被谁、在哪一步调用的：只要 agent 用了这个 skill，校验就一定在它返回时发生。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from bill_agent.extract import GateResult, GateStatus
from bill_agent.validate import ValidationIssue, Validator

from .extraction import SKILL_NAME, group_key, render, repairing

log = logging.getLogger(__name__)

VALIDATION_FEEDBACK_PREFIX = "[数据校验问题，请对照原文核实：抽取有误就修改；原文本身就是这样则保持原样照抄，不要自行纠正]"

# 校验问题 -> 重抽方式（与 extractor.Problem.kind 一致）：field 只补单据级字段，price 只复核价格，other 整份重抽
REPAIR_KIND = {"V1": "field", "V2": "other", "V3": "price", "V4": "field", "V5": "field"}


def to_problem(issue: ValidationIssue) -> dict:
    return {"text": f"{VALIDATION_FEEDBACK_PREFIX} {issue}", "kind": REPAIR_KIND.get(issue.code, "other"),
            "doc": issue.doc, "field": issue.field}


class ValidationHook(AgentMiddleware):
    def __init__(self, validator: Validator | None = None, max_retries: int = 3, skill_name: str = SKILL_NAME):
        super().__init__()
        self.validator = validator or Validator()
        self.max_retries = max_retries
        self.skill_name = skill_name

    def wrap_tool_call(self, request, handler: Callable[[Any], ToolMessage | Any]):
        if request.tool_call["name"] != self.skill_name:
            return handler(request)

        first = handler(request)
        results: list[GateResult] | None = getattr(first, "artifact", None)
        if not isinstance(first, ToolMessage) or results is None:    # skill 本身出错，交给 agent 处理
            return first
        results = list(results)

        for attempt in range(self.max_retries + 1):
            repair = self._validate(results, last=attempt == self.max_retries)
            if not repair:
                break
            log.info("数据校验第 %d 次退回: %s", attempt + 1, list(repair))
            with repairing(repair):
                retry = handler(request)
            fixed = {group_key(g.source_paths): g for g in (getattr(retry, "artifact", None) or [])}
            for i, g in enumerate(results):
                key = group_key(g.source_paths)
                if key not in repair:
                    continue
                if key not in fixed:     # 重抽调用失败：保留原结果，转人工
                    g.status = GateStatus.NEEDS_REVIEW
                    g.reasons = [f"数据校验退回后重抽失败: {getattr(retry, 'content', retry)}"]
                    continue
                new = fixed[key]
                new.rounds = g.rounds + new.rounds     # 保留每一轮的记录，便于审计
                results[i] = new

        return ToolMessage(content=render(results), artifact=results,
                           tool_call_id=first.tool_call_id, name=first.name)

    def _validate(self, results: list[GateResult], last: bool) -> dict:
        """校验还没校验过的 passed 组；返回需要重抽的组。last 为 True 时不再重抽，直接转人工。"""
        repair = {}
        for g in results:
            if g.status is not GateStatus.PASSED or g.rounds[-1].validation is not None:
                continue
            v = self.validator.validate(g.documents)
            g.rounds[-1].validation = v
            g.warnings = [str(w) for w in v.warnings]
            if not v.errors:
                continue
            if last:
                g.status = GateStatus.NEEDS_REVIEW
                g.reasons = [f"数据校验未通过（已重试 {self.max_retries} 次）: {e}" for e in v.errors]
            else:
                repair[group_key(g.source_paths)] = {
                    "problems": [to_problem(e) for e in v.errors],
                    "previous": g.rounds[-1].extraction.raw_outputs[-1],
                }
        return repair
