"""基于文件内容的问答：检验解析 / OCR 结果能否支撑 LLM 理解单据。

    qa = DocumentQA(docs)                  # docs: 解析 + OCR 后的 RawDocument
    r = qa.ask("这张单据的总金额是多少？")
    r.answer.answer, r.answer.evidence, r.evidence_ok

- 上下文只包含文件内容（解析 / OCR 文本 + 元信息 + 低置信度提示），模型只能据此回答
- 结构化输出：found / answer / evidence（逐字摘录的原文）
- evidence 由代码回到上下文中核验，摘录不到原文的视为编造（与 M2 抽取的证据校验同一思路）

评测集（tests/fixtures/qa/*.qa.json）格式：

    {"file": "PDF 示例账单2.pdf",
     "questions": [{"id": "dn-01", "q": "...", "expect": ["DC-108478"]},     # 答案须包含全部片段
                   {"id": "dn-09", "q": "...", "expect_found": false}]}     # 文件中没有答案的陷阱题
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from bill_agent.context import build_context, verify_evidence
from bill_agent.models import RawDocument

SYSTEM_PROMPT = """你是航运账单助手，只根据下面提供的文件内容回答问题。

规则：
- 只使用文件内容，不使用外部知识，不推测。文件中找不到答案时 found=false，answer 写"文件中没有该信息"。
- evidence 逐字摘录支撑答案的原文片段：每条必须是原文同一行里连续的文字，不要改写、翻译或补全。
  表格式排版中标签和值常在不同的行（如上一行 "PORT OF LOADING"，下一行 "YANTIAN"），
  此时把标签和值分成两条 evidence，不要拼在一起。
- 编号、金额、日期保持原文写法；如果问题问的是多项，逐项列出。
- 部分文件来自 OCR，可能有识别错误；答案依赖低置信度文本时，在 answer 里说明。
- 用中文回答。

文件内容：
{context}"""


class Answer(BaseModel):
    found: bool = Field(description="文件中是否有答案")
    answer: str = Field(description="答案；找不到时说明文件中没有")
    evidence: list[str] = Field(default_factory=list, description="逐字摘录的原文片段")


@dataclass
class QAResult:
    question: str
    answer: Answer
    evidence_ok: list[bool] = field(default_factory=list)   # 每条 evidence 是否能在原文中找到

    @property
    def all_evidence_ok(self) -> bool:
        return all(self.evidence_ok)


class DocumentQA:
    def __init__(self, docs: list[RawDocument], llm=None):
        from langchain_core.prompts import ChatPromptTemplate

        if llm is None:
            from bill_agent.llm import get_chat_model

            llm = get_chat_model()
        self.context = build_context(docs)
        if not self.context:
            raise ValueError("没有可用于问答的文档内容（OCR 是否已完成？）")
        prompt = ChatPromptTemplate.from_messages([("system", SYSTEM_PROMPT), ("human", "{question}")])
        self.chain = prompt | llm.with_structured_output(Answer)

    def ask(self, question: str) -> QAResult:
        a = self.chain.invoke({"context": self.context, "question": question})
        return QAResult(question, a, verify_evidence(a.evidence, self.context))


# ---------- 评测 ----------

def load_qa_set(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _norm_answer(s: str) -> str:
    """比较答案时忽略大小写、空白和千分位逗号（1,410.40 == 1410.40）。"""
    return re.sub(r"(?<=\d),(?=\d{3})", "", re.sub(r"\s+", "", s)).lower()


def score(item: dict, r: QAResult) -> tuple[bool, str]:
    """返回 (是否通过, 原因)。"""
    if item.get("expect_found") is False:
        return (not r.answer.found, "" if not r.answer.found else "文件中没有答案，模型却给出了答案")
    if not r.answer.found:
        return False, "模型认为文件中没有答案"
    ans = _norm_answer(r.answer.answer)
    missing = [e for e in item.get("expect", []) if _norm_answer(e) not in ans]
    if missing:
        return False, f"答案缺少 {missing}"
    if not r.all_evidence_ok:
        bad = [e for e, ok in zip(r.answer.evidence, r.evidence_ok) if not ok]
        return False, f"证据在原文中找不到 {bad}"
    return True, ""
