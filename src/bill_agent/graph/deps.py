"""生产环境的 NodeDeps：DeepSeek + 结构化抽取 skill + 数据校验 hook + 本地 OCR。"""

from __future__ import annotations

from bill_agent.harness.confidence import DEFAULT_THRESHOLD
from bill_agent.models import RawDocument
from bill_agent.normalize import Normalizer
from bill_agent.parsers import RouterConfig
from bill_agent.rules import EntityMatcher

from .nodes import NodeDeps


def skill_extract_fn(model: str | None = None, max_retries: int = 3, max_review_retries: int = 1,
                     max_validation_retries: int = 3):
    """返回 extract_fn：与 agent 中调用一样，结构化抽取 skill 的调用经过数据校验 hook。"""
    from bill_agent.extract import Extractor, ReviewGate, Reviewer
    from bill_agent.llm import get_chat_model
    from bill_agent.skills import ValidationHook, build_extraction_skill, run_skill

    llm = get_chat_model(model)

    def extract_fn(paths: list[str], docs: list[RawDocument]):
        gate = ReviewGate(Extractor(llm, max_retries=max_retries), Reviewer(llm), max_review_retries)
        skill = build_extraction_skill(gate, lambda _paths: docs)
        return run_skill(skill, {"paths": paths}, [ValidationHook(max_retries=max_validation_retries)]).artifact

    return extract_fn


def make_deps(*, db_path: str = "data/bills.db", entity: str | None = "PAT", model: str | None = None,
              blob_dir: str = "data/blobs", ocr: bool = True, ocr_device: str = "cpu",
              ocr_cache: str | None = "data/ocr_cache", audit_dir: str | None = None, out_path: str | None = None,
              hitl: bool = True, confidence_threshold: float = DEFAULT_THRESHOLD,
              max_retries: int = 3, max_review_retries: int = 1, max_validation_retries: int = 3) -> NodeDeps:
    backend = None
    if ocr:
        from bill_agent.ocr import LocalOCR
        from bill_agent.ocr.cache import CachedOCR

        backend = LocalOCR(device=ocr_device)
        if ocr_cache:
            backend = CachedOCR(backend, ocr_cache)
    return NodeDeps(
        router_config=RouterConfig(blob_dir=blob_dir),
        ocr_backend=backend,
        extract_fn=skill_extract_fn(model, max_retries, max_review_retries, max_validation_retries),
        normalizer=Normalizer(),
        db_path=db_path,
        entity=EntityMatcher.load(entity) if entity else None,
        confidence_threshold=confidence_threshold,
        hitl=hitl,
        audit_dir=audit_dir,
        out_path=out_path,
    )
