"""把节点组装成 LangGraph 状态机（Runtime 层的 LangGraph 实现）。

    parse ─(有待 OCR)→ ocr → import_bills → extract → normalize → score → reconcile
          └──────────────────↗                                      ↑        │有待人工
                                                                     └ human_review
                                                              reconcile ─→ output

    graph = build_graph(deps, checkpointer=SqliteSaver(...))
    graph.invoke({"inputs": [...], "run_id": rid}, {"configurable": {"thread_id": rid}})
    # 停在 human_review 时，结果里有 "__interrupt__"；人工处理后：
    graph.invoke(Command(resume=[{"ticket_id": "T3", "action": "approve"}]), {"configurable": {"thread_id": rid}})

节点函数返回 (update, summary)；这里统一包装：计时、统计 LLM token、生成 TraceEvent 追加到状态，
并写到审计目录的 trace.jsonl。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from langchain_core.callbacks import get_usage_metadata_callback
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph

from . import nodes as N
from .state import AgentState, TraceEvent

NODES: dict[str, Callable] = {
    "parse": N.parse,
    "ocr": N.ocr,
    "import_bills": N.import_bills,
    "extract": N.extract,
    "normalize": N.normalize,
    "score": N.score,
    "reconcile": N.reconcile,
    "human_review": N.human_review,
    "output": N.output,
}


class TraceSink:
    """trace.jsonl：每个节点一行。没有审计目录时只保留在状态里。"""

    def __init__(self, audit_dir: str | None):
        self.path = Path(audit_dir) / "trace.jsonl" if audit_dir else None

    def write(self, ev: TraceEvent) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(ev.model_dump_json() + "\n")


def traced(name: str, fn: Callable, deps: N.NodeDeps, sink: TraceSink) -> Callable[[AgentState], dict]:
    def run(state: AgentState) -> dict:
        started, t0 = datetime.now(), time.perf_counter()
        with get_usage_metadata_callback() as usage:
            try:
                update, summary = fn(state, deps)
            except GraphBubbleUp:          # interrupt：不是错误，恢复时节点会重跑
                raise
            except Exception as e:
                sink.write(TraceEvent(node=name, started_at=started, summary="失败",
                                      duration_ms=int((time.perf_counter() - t0) * 1000),
                                      error=f"{type(e).__name__}: {e}"))
                raise
        tokens = sum(u.get("total_tokens", 0) for u in usage.usage_metadata.values()) or None
        ev = TraceEvent(node=name, started_at=started, duration_ms=int((time.perf_counter() - t0) * 1000),
                        summary=summary, tokens=tokens)
        sink.write(ev)
        return {**update, "trace": [ev]}

    run.__name__ = name
    return run


def state_types() -> list[tuple[str, str]]:
    """状态里会出现的自定义类型（Pydantic 模型、枚举），登记为 checkpoint 允许反序列化的类型。

    LangGraph 默认对未登记的类型只给警告，将来会直接拦截。从模块里自动收集，新增模型不用手工维护。
    """
    import enum
    import importlib

    from pydantic import BaseModel

    out = []
    for name in ("bill_agent.models.common", "bill_agent.models.enums", "bill_agent.models.raw",
                 "bill_agent.models.extraction", "bill_agent.models.record", "bill_agent.models.output",
                 "bill_agent.graph.state"):
        mod = importlib.import_module(name)
        for obj in vars(mod).values():
            if (isinstance(obj, type) and obj.__module__ == name
                    and issubclass(obj, (BaseModel, enum.Enum))):
                out.append((name, obj.__name__))
    return out


def sqlite_checkpointer(conn):
    """SqliteSaver，只允许反序列化本项目状态里的类型。"""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite import SqliteSaver

    return SqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=state_types()))


def build_graph(deps: N.NodeDeps, checkpointer=None):
    sink = TraceSink(deps.audit_dir)
    g = StateGraph(AgentState)
    for name, fn in NODES.items():
        g.add_node(name, traced(name, fn, deps, sink))

    g.add_edge(START, "parse")
    g.add_conditional_edges("parse", lambda s: N.after_parse(s, deps), ["ocr", "import_bills"])
    g.add_edge("ocr", "import_bills")
    g.add_edge("import_bills", "extract")
    g.add_edge("extract", "normalize")
    g.add_edge("normalize", "score")
    g.add_edge("score", "reconcile")
    g.add_conditional_edges("reconcile", lambda s: N.after_reconcile(s, deps), ["human_review", "output"])
    g.add_edge("human_review", "reconcile")
    g.add_edge("output", END)
    return g.compile(checkpointer=checkpointer)
