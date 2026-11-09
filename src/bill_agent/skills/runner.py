"""不经过 agent 模型，直接按 agent 的方式执行一次 skill：工具调用依次经过 middleware 的 wrap_tool_call。

CLI 的 extract 命令用它，保证和 agent 中调用 skill 走同一套 hook。
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool


def run_skill(tool: BaseTool, args: dict, middleware: Sequence[AgentMiddleware] = ()) -> ToolMessage:
    """middleware 的顺序与 create_agent 相同：第一个在最外层。"""

    def execute(request: ToolCallRequest) -> ToolMessage:
        return request.tool.invoke(request.tool_call)

    handler = execute
    for m in reversed(middleware):
        handler = (lambda inner, mw: lambda req: mw.wrap_tool_call(req, inner))(handler, m)

    call = {"name": tool.name, "args": args, "id": f"call_{uuid4().hex[:12]}", "type": "tool_call"}
    return handler(ToolCallRequest(tool_call=call, tool=tool, state={}, runtime=None))
