"""抽取质量把关：代码核验 + LLM 审核 + 重试上限，决定结果能否进入下一步。

    results = ReviewGate(extractor, reviewer, max_review_retries=1).extract(docs)

一组文件的流程：
1. 抽取。代码核验不通过时在 Extractor 内部带反馈重试，最多 max_retries 次
2. 仍有问题 -> NEEDS_REVIEW，不再调用审核模型
3. LLM 审核。发现问题时把意见和本次输出退回抽取，最多 max_review_retries 次
4. 超过上限，或审核本身没有完成 -> NEEDS_REVIEW，附上理由
5. 审核通过，但有字段因为没有证据被清空（ExtractionResult.cleared）-> NEEDS_REVIEW

一组文件最多的模型调用次数：
    抽取 (max_retries + 1) × (max_review_retries + 1)，审核 (max_review_retries + 1) × 2
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from bill_agent.models import ExtractedDocument, RawDocument

from .extractor import ExtractionResult, Extractor, group_documents
from .reviewer import ReviewIssue, Reviewer, ReviewResult

log = logging.getLogger(__name__)

REVIEW_FEEDBACK_PREFIX = "[审核意见，请对照原文核实：确实有误就修改，原结果正确则保持不变]"


class GateStatus(str, Enum):
    PASSED = "passed"
    NEEDS_REVIEW = "needs_review"


@dataclass
class ReviewRound:
    extraction: ExtractionResult
    review: ReviewResult | None = None       # 代码核验未通过时没有审核


@dataclass
class GateResult:
    source_paths: list[str]
    status: GateStatus
    documents: list[ExtractedDocument]
    reasons: list[str] = field(default_factory=list)      # NEEDS_REVIEW 的理由，给人工看
    rounds: list[ReviewRound] = field(default_factory=list)


class ReviewGate:
    def __init__(self, extractor: Extractor, reviewer: Reviewer, max_review_retries: int = 1):
        self.extractor = extractor
        self.reviewer = reviewer
        self.max_review_retries = max_review_retries

    def run_group(self, group: list[RawDocument]) -> GateResult:
        paths = [d.source_path for d in group]
        rounds: list[ReviewRound] = []
        feedback: list[str] = []
        previous: dict | None = None
        issues: list[ReviewIssue] = []

        def done(status: GateStatus, reasons: list[str]) -> GateResult:
            return GateResult(paths, status, rounds[-1].extraction.documents, reasons, rounds)

        for _ in range(self.max_review_retries + 1):
            extraction = self.extractor.extract_group(group, feedback, previous)
            rounds.append(ReviewRound(extraction))
            if extraction.errors:
                return done(GateStatus.NEEDS_REVIEW, extraction.errors)

            review = self.reviewer.review(group, extraction.documents)
            rounds[-1].review = review
            if review.incomplete:
                return done(GateStatus.NEEDS_REVIEW, [f"审核未完成: {review.incomplete}"])
            if not review.issues:
                if extraction.cleared:      # 值因为没有证据被清空，可能丢了正确的信息，要人工确认
                    return done(GateStatus.NEEDS_REVIEW,
                                [f"证据不足被清空，需人工确认: {c}" for c in extraction.cleared])
                return done(GateStatus.PASSED, [])

            log.info("审核退回: %s", review.issues)
            issues = review.issues
            feedback = [f"{REVIEW_FEEDBACK_PREFIX} {i}" for i in issues]
            previous = extraction.raw_outputs[-1]
        return done(GateStatus.NEEDS_REVIEW, [str(i) for i in issues])

    def extract(self, docs: list[RawDocument]) -> list[GateResult]:
        return [self.run_group(g) for g in group_documents(docs)]
