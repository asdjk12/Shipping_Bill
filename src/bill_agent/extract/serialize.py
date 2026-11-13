"""抽取 / 审核 / 校验结果转 JSON（CLI 输出和编排的审计记录共用）。"""

from __future__ import annotations


def extraction_json(r) -> dict:
    return {"source_paths": r.source_paths, "attempts": r.attempts, "errors": r.errors,
            "attempt_errors": r.attempt_errors, "attempt_modes": r.attempt_modes, "cleared": r.cleared,
            "patches": r.patches,
            "documents": [d.model_dump(mode="json") for d in r.documents], "raw_outputs": r.raw_outputs}


def validation_json(validation) -> dict:
    return {"issues": [{"code": i.code, "severity": i.severity.value, "doc": i.doc, "field": i.field,
                        "message": i.message} for i in validation.issues]}


def review_json(review) -> dict:
    return {"issues": [vars(i) for i in review.issues], "dismissed": [vars(i) for i in review.dismissed],
            "incomplete": review.incomplete, "attempts": review.attempts, "raw_outputs": review.raw_outputs}


def gate_json(g) -> dict:
    """extract.GateResult，含每一轮的抽取、审核、校验记录。"""
    return {"source_paths": g.source_paths, "status": g.status.value, "reasons": g.reasons,
            "warnings": g.warnings, "documents": [d.model_dump(mode="json") for d in g.documents],
            "rounds": [{"extraction": extraction_json(r.extraction),
                        "review": r.review and review_json(r.review),
                        "validation": r.validation and validation_json(r.validation)} for r in g.rounds]}
