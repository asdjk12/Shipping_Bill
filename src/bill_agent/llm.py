"""LLM 接入（LangChain + DeepSeek）。

需要环境变量 DEEPSEEK_API_KEY；模型名可用 BILL_AGENT_MODEL 覆盖（可选 deepseek-v4-pro / deepseek-flash）。
"""

from __future__ import annotations

import os

DEFAULT_MODEL = "deepseek-v4-pro"


def get_chat_model(model: str | None = None, temperature: float = 0):
    from langchain_deepseek import ChatDeepSeek

    return ChatDeepSeek(
        model=model or os.environ.get("BILL_AGENT_MODEL") or DEFAULT_MODEL,
        temperature=temperature,
        # DeepSeek 默认开启思考模式，思考模式不支持结构化输出所需的强制 tool_choice，因此关闭
        extra_body={"thinking": {"type": "disabled"}},
    )
