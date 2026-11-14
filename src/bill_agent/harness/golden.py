"""Golden 回归（development.md 第 8 节第 9 条）：输入 → 期望输出 JSON，按字段统计准确率。

场景文件 tests/fixtures/golden/scenarios/*.json 描述一串批次，每个场景用一个全新的内存账单库：

    {
      "description": "...",
      "entity": "PAT",
      "batches": [
        {"inputs": [{"golden": "DN_DC-108478"},
                    {"golden": "DN_DC-108478", "set": {"charges[1].amount": 1400}}],
         "expected": [ {...投影后的输出记录...} ]}
      ]
    }

- inputs 引用 tests/fixtures/samples/<name>.expected_extraction.json（人工标注的抽取结果），
  "set" 按路径改写个别字段，用来构造修订单、记账主体是交易一方等题目样例里没有的情况；
- expected 省略时该批次只用来准备账单库，不比较；
- 比较的是输出记录的投影（project）：操作、核心字段、扩展字段、匹配规则、变更字段名、是否待人工。
  ai_reasoning 的措辞、记录 ID、置信度不比较，改说明文字不会让回归失败。

不调用 LLM，检验的是标准化 → AR/AP → 匹配 → 操作判定这一段确定性代码。
golden-eval --extracted 把第一个场景第一个批次的输入换成真实的 LLM 抽取结果，即端到端准确率。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bill_agent.context import loose, squash
from bill_agent.models import ExtractedDocument, OutputRecord

GOLDEN_DIR = Path(__file__).parents[3] / "tests" / "fixtures" / "golden"
SAMPLES_DIR = Path(__file__).parents[3] / "tests" / "fixtures" / "samples"

# 投影后参与比较的字段；顺序即报告顺序
FIELDS = ["operation", "document_type", "reference_no", "document_date", "bl_nos", "booking_no", "container_nos",
          "issuer", "recipient", "payee", "payer",
          "charge_item", "original_charge_item", "line_no", "quantity", "unit_price", "amount", "currency",
          "payment_term", "accounting_entity", "posting_type", "matched_rule", "changed_fields", "needs_review"]
PARTY_FIELDS = {"issuer", "recipient", "payee", "payer", "accounting_entity"}
LIST_FIELDS = {"bl_nos", "container_nos", "changed_fields"}
NUMBER_FIELDS = {"quantity", "unit_price", "amount"}


def project(rec: OutputRecord) -> dict[str, Any]:
    """输出记录 → 扁平的可比较字段。"""
    o = rec.model_dump(mode="json")
    d, p, c, f, a = (o["document_info"], o["transaction_party"], o["charge_details"],
                     o["financial_attributes"], o["audit_trail"])
    return {"operation": o["operation"], **d, **p, **c, **f,
            "matched_rule": a["matched_rule"],
            "changed_fields": [x["field"] for x in a["changed_fields"]],
            "needs_review": bool(a["review_reasons"])}


def record_key(r: dict[str, Any]) -> tuple:
    """对齐期望和实际记录：单据号（没有时用第一个提单号）+ 行号 + 原始费用名。"""
    doc = r.get("reference_no") or (r.get("bl_nos") or [None])[0]
    return squash(doc or ""), r.get("line_no"), squash(r.get("original_charge_item") or "")


def same(name: str, e: Any, a: Any) -> bool:
    if name in NUMBER_FIELDS and e is not None and a is not None:
        return abs(float(e) - float(a)) < 0.005
    if name in LIST_FIELDS:
        return sorted(map(squash, e or [])) == sorted(map(squash, a or []))
    if isinstance(e, str) and isinstance(a, str):
        return (loose(e) == loose(a)) if name in PARTY_FIELDS else squash(e) == squash(a)
    return e == a


# ---------- 场景 ----------

@dataclass
class Batch:
    inputs: list[dict[str, Any]]
    expected: list[dict[str, Any]] | None


@dataclass
class Scenario:
    name: str
    description: str
    entity: str
    batches: list[Batch]
    path: Path | None = None


def load_scenarios(golden_dir: Path = GOLDEN_DIR) -> list[Scenario]:
    out = []
    for f in sorted((Path(golden_dir) / "scenarios").glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        out.append(Scenario(f.stem, data.get("description", ""), data.get("entity", "PAT"),
                            [Batch(b["inputs"], b.get("expected")) for b in data["batches"]], f))
    return out


_STEP = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def set_path(obj: Any, path: str, value: Any) -> None:
    """按 "charges[1].amount" 这样的路径改写字段。"""
    steps = [int(i) if i else k for k, i in _STEP.findall(path)]
    for s in steps[:-1]:
        obj = obj[s]
    obj[steps[-1]] = value


def build_document(spec: dict[str, Any], samples_dir: Path = SAMPLES_DIR) -> ExtractedDocument:
    data = json.loads((Path(samples_dir) / f"{spec['golden']}.expected_extraction.json").read_text(encoding="utf-8"))
    for path, value in spec.get("set", {}).items():
        set_path(data, path, value)
    return ExtractedDocument.model_validate(data)


# ---------- 对比 ----------

@dataclass
class FieldDiff:
    key: tuple
    field: str
    expected: Any
    actual: Any


@dataclass
class Comparison:
    fields: int = 0
    diffs: list[FieldDiff] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)     # 期望有、实际没有
    extra: list[dict] = field(default_factory=list)       # 实际有、期望没有
    per_field: dict[str, list[int]] = field(default_factory=dict)   # 字段 -> [一致数, 总数]

    @property
    def ok(self) -> bool:
        return not self.diffs and not self.missing and not self.extra

    def merge(self, other: "Comparison") -> None:
        self.fields += other.fields
        self.diffs += other.diffs
        self.missing += other.missing
        self.extra += other.extra
        for k, (good, n) in other.per_field.items():
            acc = self.per_field.setdefault(k, [0, 0])
            acc[0] += good
            acc[1] += n


def compare_records(expected: list[dict], actual: list[dict]) -> Comparison:
    """按 record_key 对齐后逐字段比较；同一个键出现多次时按出现顺序配对。"""
    cmp = Comparison()
    pool: dict[tuple, list[dict]] = {}
    for a in actual:
        pool.setdefault(record_key(a), []).append(a)
    for e in expected:
        key = record_key(e)
        if not pool.get(key):
            cmp.missing.append(e)
            continue
        a = pool[key].pop(0)
        for name in FIELDS:
            good = same(name, e.get(name), a.get(name))
            acc = cmp.per_field.setdefault(name, [0, 0])
            acc[0] += good
            acc[1] += 1
            cmp.fields += 1
            if not good:
                cmp.diffs.append(FieldDiff(key, name, e.get(name), a.get(name)))
    cmp.extra = [a for rest in pool.values() for a in rest]
    return cmp


# ---------- 运行 ----------

@dataclass
class BatchResult:
    actual: list[dict]
    expected: list[dict] | None
    comparison: Comparison | None


def run_scenario(sc: Scenario, samples_dir: Path = SAMPLES_DIR,
                 first_batch: list[tuple[ExtractedDocument, list[str]]] | None = None) -> list[BatchResult]:
    """在全新的内存账单库上依次处理各批次。first_batch 给定时替换第一个批次的输入（真实抽取结果）。"""
    from bill_agent.billing import BillProcessor, BillRepository, Matcher, PartyNormalizer
    from bill_agent.normalize import Normalizer
    from bill_agent.rules import EntityMatcher

    normalizer = Normalizer()
    results = []
    with BillRepository(":memory:") as repo:
        processor = BillProcessor(repo, Matcher(repo, PartyNormalizer.load()), EntityMatcher.load(sc.entity),
                                  normalizer)
        for i, batch in enumerate(sc.batches):
            docs = (first_batch if i == 0 and first_batch is not None
                    else [(build_document(s, samples_dir), []) for s in batch.inputs])
            ops = processor.process_batch([(d, normalizer.normalize_document(d, up)) for d, up in docs])
            actual = [project(r.to_output()) for r in ops]
            cmp = compare_records(batch.expected, actual) if batch.expected is not None else None
            results.append(BatchResult(actual, batch.expected, cmp))
    return results


def write_expected(sc: Scenario, results: list[BatchResult]) -> None:
    """--update：把实际输出写成期望（只改原本就有 expected 的批次）。写完必须人工核对 diff 再提交。"""
    data = json.loads(sc.path.read_text(encoding="utf-8"))
    for b, r in zip(data["batches"], results):
        if "expected" in b:
            b["expected"] = r.actual
    sc.path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
