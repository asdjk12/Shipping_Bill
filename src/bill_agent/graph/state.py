"""编排的状态。

设计约束：
- 状态里只放可序列化的数据（Pydantic 模型 / 基本类型），checkpointer 才能保存、暂停后恢复；
  以后换成自研 harness 的 State/Memory 层时，也只需要能存取这份数据。
- 每个字段只由一个节点写（trace 例外，用 reducer 追加），出错时能直接看出是哪一步的产物。
- 不重复已有模块的类型：RawDocument / ExtractedDocument / ChargeRecord / OutputRecord 原样使用。
"""

from __future__ import annotations

import operator
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

from bill_agent.models import ChargeRecord, ExtractedDocument, OutputRecord, RawDocument


class SkippedInput(BaseModel):
    """parse 跳过的输入（parsers.SkippedItem 的可序列化版本）。"""
    source_path: str
    reason: str


class BillJsonInput(BaseModel):
    """输入里夹带的已有账单 JSON（parsers.BillJSON 的可序列化版本），由 import_bills 写入账单库。"""
    source_path: str
    source_id: str
    records: list[dict[str, Any]]


class ExtractionGroup(BaseModel):
    """一组文件（一个上传文件及其附件）的抽取结果，对应 extract.GateResult。

    GateResult.rounds 体积大，完整轮次记录写到审计目录的 extraction.json，状态里只留轮数。
    """
    source_paths: list[str]
    status: Literal["passed", "needs_review"]
    documents: list[ExtractedDocument]
    reasons: list[str] = Field(default_factory=list)      # needs_review 的原因
    warnings: list[str] = Field(default_factory=list)     # 数据校验 warning，不阻止入账
    rounds: int = 1                                       # 抽取 + 审核共跑了几轮


class NormalizedDoc(BaseModel):
    """一张单据标准化后的费用行。doc 用来读改账指令（BillProcessor 需要）。"""
    group: int                                              # 所属 ExtractionGroup 的下标
    doc: ExtractedDocument
    records: list[ChargeRecord]
    confidence: list[float] = Field(default_factory=list)   # 与 records 一一对应，由 score 写
    confidence_detail: list[str] = Field(default_factory=list)


class ReviewTicket(BaseModel):
    """一条待人工复核的输出。"""
    ticket_id: str
    output_index: int                # 对应 outputs 的下标，人工处理后替换这一条
    source_paths: list[str]
    record: ChargeRecord
    reasons: list[str]
    confidence: float | None = None
    instruction: bool = False        # 无法自动执行的改账指令：只能 reject，或在账单库里手工处理


class ReviewDecision(BaseModel):
    """人工对一个 ticket 的处理，作为 interrupt 的恢复值传回。"""
    ticket_id: str
    action: Literal["approve", "edit", "reject"]
    changes: dict[str, Any] = Field(default_factory=dict)   # action=edit 时要改的业务字段
    note: str | None = None


class TraceEvent(BaseModel):
    """可观测性（Trace/Eval 层）：每个节点一条，追加到状态，同时写本地 JSONL。"""
    node: str
    started_at: datetime
    duration_ms: int
    summary: str
    tokens: int | None = None        # 本节点内 LLM 调用的 token 总数
    error: str | None = None


class Stage(str, Enum):
    DONE = "done"


class AgentState(TypedDict, total=False):
    # 输入（调用方提供）
    inputs: list[str]                       # 文件或目录路径
    run_id: str

    # parse / ocr / import_bills
    raw_docs: list[RawDocument]
    bill_jsons: list[BillJsonInput]
    skipped: list[SkippedInput]

    # extract：结构化抽取 skill（含 ReviewGate + ValidationHook 重试）
    groups: list[ExtractionGroup]

    # normalize / score
    normalized: list[NormalizedDoc]

    # reconcile：AR/AP + 4 级匹配 + 操作判定 + 写库（一个事务）
    outputs: list[OutputRecord]             # 最终输出（CREATE / UPDATE / NO_CHANGE / PENDING_REVIEW）
    tickets: list[ReviewTicket]             # 本轮产生的待人工
    decisions: list[ReviewDecision]         # human_review 收到的人工处理
    review_round: int                       # 已经过几轮人工复核

    stage: Stage
    trace: Annotated[list[TraceEvent], operator.add]
