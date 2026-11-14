"""命令行入口。

    python -m bill_agent parse <文件或目录>... [--blob-dir data/blobs] [--out parsed.json]
                               [--ocr [--ocr-device cpu|gpu]]
    python -m bill_agent ask <文件或目录>... [-q 问题]... [--model deepseek-v4-pro]
    python -m bill_agent qa-eval <评测集文件或目录>... --samples-dir <样例目录> [--out report.json]
    python -m bill_agent extract <文件或目录>... [--out extracted.json] [--max-retries 3]
                                 [--max-review-retries 1] [--max-validation-retries 3] [--no-review]
    python -m bill_agent validate <抽取结果 JSON 或目录>... [--out report.json]
    python -m bill_agent normalize <抽取结果 JSON 或目录>... [--out records.json]
    python -m bill_agent db-import <账单 JSON 或目录>... [--db data/bills.db]
    python -m bill_agent apply <抽取结果 JSON 或目录>... [--db data/bills.db] [--entity PAT] [--out result.json]
    python -m bill_agent run <文件或目录>... [--db data/bills.db] [--entity PAT] [--out result.json] [--no-hitl]
    python -m bill_agent resume <run_id> [--decisions decisions.json]
    python -m bill_agent extract-eval --samples-dir <样例目录> [--out report.json]
    python -m bill_agent review-eval --samples-dir <样例目录> [--out report.json]

parse：把输入文件集解析成 RawDocument，打印摘要并（可选）输出完整 JSON，用于检查解析层效果。
       PDF / 图片登记为 pending_ocr，原文件存档到 --blob-dir；加 --ocr 时接着用本地 OCR 识别。
ask：解析 + OCR 后基于文件内容向 LLM 提问；不给 -q 时进入交互模式。
qa-eval：按评测集逐题提问并判分，检验解析 / OCR 结果能否支撑 LLM 正确回答。
extract：结构化抽取 skill（抽取 + 代码核验 + LLM 审核），调用经过数据校验 hook；超过重试上限标为待人工复核。
validate：对已保存的抽取结果做数据校验，不调用 LLM。
normalize：把已保存的抽取结果标准化为费用行记录（ChargeRecord），不调用 LLM。
db-import：把已有账单 JSON 导入 SQLite 账单库（按 record_id upsert，重复导入结果不变）。
apply：抽取结果 -> 标准化 -> AR/AP -> 4 级匹配 -> 操作判定，CREATE / UPDATE 写入账单库，输出标准格式 JSON。
run：端到端编排（LangGraph）：解析 -> OCR -> 抽取 -> 标准化 -> 置信度 -> 入账。有待人工复核时暂停，
     写出 output/runs/<run_id>/tickets.json 和待填写的 decisions.json。
resume：读入人工处理结果，从暂停处继续（不会重新抽取）。
extract-eval：抽取 golden 标注对应的样例，逐字段对比（只评测抽取，不经过审核）。
review-eval：评测审核模型：golden 不应被误报，故意改坏的字段应被发现。
OCR 结果按文件哈希缓存在 --ocr-cache（默认 data/ocr_cache），同一文件不重复识别。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bill_agent.extract.serialize import extraction_json as _extraction_json
from bill_agent.extract.serialize import gate_json as _gate_json
from bill_agent.extract.serialize import validation_json as _validation_json
from bill_agent.parsers import RouterConfig, build_default_router


def _parse_and_ocr(paths, args, ocr: bool = True):
    router = build_default_router(RouterConfig(blob_dir=args.blob_dir))
    result = router.route_paths(paths)
    if ocr:
        from bill_agent.ocr import LocalOCR, run_ocr
        from bill_agent.ocr.cache import CachedOCR

        backend = LocalOCR(device=args.ocr_device)
        if args.ocr_cache:
            backend = CachedOCR(backend, args.ocr_cache)
        result.documents = run_ocr(result.documents, backend)
    return result


def cmd_parse(args: argparse.Namespace) -> int:
    result = _parse_and_ocr(args.paths, args, ocr=args.ocr)

    pending = sum(1 for d in result.documents if not d.ready_for_extraction)
    print(f"\n解析得到 {len(result.documents)} 份文档（其中 {pending} 份待 OCR），"
          f"{len(result.bill_jsons)} 个账单 JSON，跳过 {len(result.skipped)} 项\n")
    for d in result.documents:
        print(f"[{d.file_type.value:5}] {d.source_path}  ({d.status.value})")
        if d.ocr_engine:
            conf = "-" if d.ocr_confidence is None else f"{d.ocr_confidence:.2f}"
            print(f"        OCR {d.ocr_engine} | 置信度 {conf}" + (f" | 错误 {d.ocr_error}" if d.ocr_error else ""))
        if d.ready_for_extraction:
            tables = ", ".join(f"{t.name}({len(t.rows)}行{'' if t.header else ', 无表头'})" for t in d.tables) or "-"
            print(f"        文本 {len(d.text)} 字 | 表格 {tables}")
        else:
            print(f"        {d.mime_type}, {d.file_size} bytes -> {d.file_ref}")
        for w in d.warnings:
            print(f"        ! {w}")
    for b in result.bill_jsons:
        print(f"[json ] {b.source_path}  ({len(b.records)} 条账单)")
    for s in result.skipped:
        print(f"[skip ] {s.source_path}  -- {s.reason}")

    if args.out:
        out = {
            "documents": [d.model_dump(mode="json") for d in result.documents],
            "bill_jsons": [{"source_path": b.source_path, "source_id": b.source_id, "records": b.records}
                           for b in result.bill_jsons],
            "skipped": [{"source_path": s.source_path, "reason": s.reason} for s in result.skipped],
        }
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n完整结果已写入 {args.out}")
    return 0


def _print_result(r) -> None:
    print(f"\n答：{r.answer.answer}" + ("" if r.answer.found else "  [文件中没有]"))
    for e, ok in zip(r.answer.evidence, r.evidence_ok):
        print(f"   {'✓' if ok else '✗ 原文中找不到'} 「{e}」")


def cmd_ask(args: argparse.Namespace) -> int:
    from bill_agent.llm import get_chat_model
    from bill_agent.qa import DocumentQA

    docs = _parse_and_ocr(args.paths, args).documents
    print(f"已载入 {sum(d.ready_for_extraction for d in docs)} 份文档：")
    for d in docs:
        print(f"  - {d.source_path}  ({d.status.value})")
    qa = DocumentQA(docs, get_chat_model(args.model))

    if args.question:
        for q in args.question:
            print(f"\n问：{q}")
            _print_result(qa.ask(q))
        return 0
    print("\n输入问题后回车；空行或 exit 退出。")
    while True:
        try:
            q = input("\n问：").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q or q.lower() in ("exit", "quit"):
            break
        _print_result(qa.ask(q))
    return 0


def cmd_qa_eval(args: argparse.Namespace) -> int:
    from bill_agent.llm import get_chat_model
    from bill_agent.qa import DocumentQA, load_qa_set, score

    files: list[Path] = []
    for p in map(Path, args.qa_sets):
        files += sorted(p.glob("*.qa.json")) if p.is_dir() else [p]

    llm = get_chat_model(args.model)
    report, passed, total = [], 0, 0
    for f in files:
        qa_set = load_qa_set(f)
        src = Path(args.samples_dir) / qa_set["file"]
        print(f"\n=== {f.name}  ->  {src.name}")
        if not src.exists():
            print(f"  ! 找不到样例文件 {src}，跳过")
            continue
        qa = DocumentQA(_parse_and_ocr([src], args).documents, llm)
        for item in qa_set["questions"]:
            r = qa.ask(item["q"])
            ok, reason = score(item, r)
            passed += ok
            total += 1
            print(f"  [{'PASS' if ok else 'FAIL'}] {item['id']} {item['q']}")
            print(f"         答：{r.answer.answer}" + (f"\n         原因：{reason}" if reason else ""))
            report.append({"qa_set": f.name, **item, "passed": ok, "reason": reason,
                           "found": r.answer.found, "answer": r.answer.answer,
                           "evidence": r.answer.evidence, "evidence_ok": r.evidence_ok})

    print(f"\n通过 {passed} / {total}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细已写入 {args.out}")
    return 0 if passed == total else 1


def _print_extraction(r) -> None:
    print(f"\n=== {' + '.join(r.source_paths)}  （第 {r.attempts} 次通过）" if not r.errors
          else f"\n=== {' + '.join(r.source_paths)}  （{r.attempts} 次后仍有 {len(r.errors)} 个问题）")
    modes = {"full": "整份抽取", "patch": "只补问题"}
    for i, errs in enumerate(r.attempt_errors[:-1], start=1):
        print(f"  第 {i} 次（{modes.get(r.attempt_modes[i - 1], '')}）校验问题，已反馈给模型重试：")
        for e in errs:
            print(f"    - {e}")
    if not r.documents:
        print("  没有收费单据")
    for d in r.documents:
        print(f"  [{d.document_type_raw}] {d.reference_no or '-'}  日期 {d.document_date_raw or '-'}")
        print(f"    签发 {d.issuer or '-'} -> 接收 {d.recipient or '-'} | 收款 {d.payee or '-'} | 付款 {d.payer or '-'}")
        print(f"    提单 {d.bl_nos or '-'} | 订舱 {d.booking_no or '-'} | 箱号 {d.container_nos or '-'} | 币种 {d.currency_raw or '-'}")
        for c in d.charges:
            print(f"    {c.line_no}. {c.charge_name_raw} | 数量 {c.quantity} | 单价 {c.unit_price} | 金额 {c.amount} "
                  f"{c.currency_raw or ''} | 付款方式 {c.payment_term_raw}")
        for t in d.totals:
            print(f"    合计[{t.currency_raw}] 小计 {t.subtotal} | 税额 {t.tax_amount} | 总计 {t.total_amount}")
        for ci in d.change_instructions:
            print(f"    改账: {ci.target_charge_name_raw}.{ci.field} -> {ci.new_value_raw}")
    for c in r.cleared:
        print(f"  已置空（重试后仍没有证据）: {c}")
    for e in r.errors:
        print(f"  ! {e}")


def _print_review(review) -> None:
    if review.incomplete:
        print(f"  审核未完成：{review.incomplete}")
    elif not review.issues:
        print("  审核通过")
    for i in review.issues:
        print(f"  审核问题 - {i}")
    for i in review.dismissed:
        print(f"  已忽略（依据在原文中找不到）- {i}")


def _print_gate(g) -> None:
    for n, round_ in enumerate(g.rounds, start=1):
        print(f"\n----- 第 {n} 轮 -----", end="")
        _print_extraction(round_.extraction)
        if round_.review:
            _print_review(round_.review)
        if round_.validation:
            _print_validation(round_.validation)
    print(f"\n结论：{'通过，可进入下一步' if g.status.value == 'passed' else '待人工复核'}")
    for reason in g.reasons:
        print(f"  - {reason}")
    for w in g.warnings:
        print(f"  提示 - {w}")


def _print_validation(validation) -> None:
    if not validation.issues:
        print("  数据校验通过")
    for i in validation.issues:
        print(f"  校验{'问题' if i.severity.value == 'error' else '提示'} - {i}")


def _write_json(path: str, data) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_extract(args: argparse.Namespace) -> int:
    from bill_agent.extract import Extractor, ReviewGate, Reviewer
    from bill_agent.llm import get_chat_model

    docs = _parse_and_ocr(args.paths, args).documents
    llm = get_chat_model(args.model)
    extractor = Extractor(llm, max_retries=args.max_retries)
    if args.no_review:
        results = extractor.extract(docs)
        for r in results:
            _print_extraction(r)
        data = [_extraction_json(r) for r in results]
    else:
        # 与 agent 中调用一样：结构化抽取 skill 的调用经过数据校验 hook
        from bill_agent.skills import ValidationHook, build_extraction_skill, run_skill

        skill = build_extraction_skill(ReviewGate(extractor, Reviewer(llm), args.max_review_retries),
                                       lambda paths: docs)
        results = run_skill(skill, {"paths": args.paths},
                            [ValidationHook(max_retries=args.max_validation_retries)]).artifact
        for g in results:
            _print_gate(g)
        data = [_gate_json(g) for g in results]
    if args.out:
        _write_json(args.out, data)
        print(f"\n完整结果已写入 {args.out}")
    return 0


def _saved_groups(path: Path) -> list[tuple[str, list]]:
    """读取已保存的抽取结果：extract 的输出（每组带 documents），或单份 ExtractedDocument（golden）。"""
    from bill_agent.models import ExtractedDocument

    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "documents" not in data:
        return [(path.name, [ExtractedDocument.model_validate(data)])]
    groups = data if isinstance(data, list) else [data]
    return [((g.get("source_paths") or [path.name])[0],
             [ExtractedDocument.model_validate(d) for d in g["documents"]]) for g in groups]


def cmd_validate(args: argparse.Namespace) -> int:
    from bill_agent.validate import Validator

    files = [f for p in map(Path, args.paths)
             for f in (sorted(p.glob("*.json")) if p.is_dir() else [p]) if f.name != "manifest.json"]
    validator, report, failed = Validator(), [], 0
    for f in files:
        for name, documents in _saved_groups(f):
            result = validator.validate(documents)
            failed += bool(result.errors)
            print(f"\n=== {f.name}: {name}")
            _print_validation(result)
            report.append({"file": str(f), "group": name, **_validation_json(result)})
    print(f"\n{len(report)} 组，其中 {failed} 组有 error")
    if args.out:
        _write_json(args.out, report)
        print(f"明细已写入 {args.out}")
    return 1 if failed else 0


def cmd_normalize(args: argparse.Namespace) -> int:
    from bill_agent.models import ExtractedDocument
    from bill_agent.normalize import Normalizer
    from bill_agent.rules import EntityMatcher, apply_posting

    files = [f for p in map(Path, args.paths)
             for f in (sorted(p.glob("*.json")) if p.is_dir() else [p]) if f.name != "manifest.json"]
    normalizer, records = Normalizer(), []
    entity = EntityMatcher.load(args.entity)
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "documents" not in data:      # 单份 ExtractedDocument（golden）
            groups = [{"documents": [data]}]
        else:
            groups = data if isinstance(data, list) else [data]
        for g in groups:
            # extract 的输出带 status / reasons；待人工复核的组也标准化，原因带到每条记录
            upstream = g.get("reasons", []) if g.get("status", "passed") != "passed" else []
            name = (g.get("source_paths") or [f.name])[0]
            print(f"\n=== {f.name}: {name}")
            for d in g["documents"]:
                recs = normalizer.normalize_document(ExtractedDocument.model_validate(d), upstream)
                for rec in apply_posting(recs, entity):
                    records.append(rec)
                    print(f"  [{rec.line_no}] {rec.original_charge_item!r} -> {rec.charge_item!r}  "
                          f"{rec.amount} {rec.currency} {rec.payment_term and rec.payment_term.value}  "
                          f"{rec.document_type and rec.document_type.value} {rec.document_date}  "
                          f"{rec.accounting_entity}:{rec.posting_type and rec.posting_type.value}")
                    for r in rec.posting_reasoning:
                        print(f"      AR/AP - {r}")
                    for r in rec.review_reasons:
                        print(f"      待人工 - {r}")
    flagged = sum(bool(r.review_reasons) for r in records)
    print(f"\n{len(records)} 条费用记录，其中 {flagged} 条需要人工确认")
    if args.out:
        _write_json(args.out, [r.model_dump(mode="json") for r in records])
        print(f"明细已写入 {args.out}")
    return 0


def cmd_db_import(args: argparse.Namespace) -> int:
    from bill_agent.billing import BillRepository

    result = build_default_router(RouterConfig(blob_dir=args.blob_dir)).route_paths(args.paths)
    for s in result.skipped:
        print(f"[skip ] {s.source_path}  -- {s.reason}")
    if result.documents:
        print(f"忽略 {len(result.documents)} 份非账单 JSON 文件")
    failed = 0
    with BillRepository(args.db) as repo:
        for b in result.bill_jsons:
            r = repo.import_bills(b)
            failed += bool(r.errors)
            print(f"\n=== {r.source_path}: 新增 {len(r.created)} | 更新 {len(r.updated)} | "
                  f"无变化 {len(r.unchanged)} | 错误 {len(r.errors)}")
            for w in r.warnings:
                print(f"  提示 - {w}")
            for e in r.errors:
                print(f"  ! {e}")
        print(f"\n账单库 {args.db} 共 {repo.count()} 条记录")
    return 1 if failed else 0


def cmd_apply(args: argparse.Namespace) -> int:
    from bill_agent.billing import BillProcessor, BillRepository, Matcher, PartyNormalizer
    from bill_agent.extract.serialize import load_extractions
    from bill_agent.models import to_json
    from bill_agent.normalize import Normalizer
    from bill_agent.rules import EntityMatcher

    normalizer = Normalizer()
    items = [(doc, normalizer.normalize_document(doc, upstream)) for doc, upstream in load_extractions(args.paths)]

    with BillRepository(args.db) as repo:
        processor = BillProcessor(repo, Matcher(repo, PartyNormalizer.load()), EntityMatcher.load(args.entity),
                                  normalizer)
        results = processor.process_batch(items)
        for r in results:
            rec = r.record
            print(f"\n[{r.operation.value:14}] {rec.reference_no or '-'} 第 {rec.line_no or '-'} 行 "
                  f"{rec.original_charge_item!r}  {rec.amount} {rec.currency}  -> {r.record_id or '-'}")
            for line in r.reasoning:
                print(f"    {line}")
            for line in r.review_reasons:
                print(f"    待人工 - {line}")
        counts: dict[str, int] = {}
        for r in results:
            counts[r.operation.value] = counts.get(r.operation.value, 0) + 1
        print(f"\n{len(results)} 条：" + " | ".join(f"{k} {v}" for k, v in counts.items())
              + f"；账单库 {args.db} 共 {repo.count()} 条记录")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(to_json([r.to_output() for r in results], strict=args.strict_schema),
                                  encoding="utf-8")
        print(f"输出已写入 {args.out}")
    return 0


def cmd_extract_eval(args: argparse.Namespace) -> int:
    from bill_agent.extract import Extractor
    from bill_agent.extract.compare import compare
    from bill_agent.llm import get_chat_model
    from bill_agent.models import ExtractedDocument

    golden_dir = Path(args.golden_dir)
    manifest = json.loads((golden_dir / "manifest.json").read_text(encoding="utf-8"))
    extractor = Extractor(get_chat_model(args.model), max_retries=args.max_retries)
    report, total_n, total_diff = [], 0, 0
    for golden_name, file_name in manifest.items():
        if golden_name.startswith("_"):
            continue
        src = Path(args.samples_dir) / file_name
        if not src.exists():
            print(f"\n! 找不到样例文件 {src}，跳过")
            continue
        expected = ExtractedDocument.model_validate_json((golden_dir / golden_name).read_text(encoding="utf-8"))
        results = extractor.extract(_parse_and_ocr([src], args).documents)
        for r in results:
            _print_extraction(r)
        actual = [d for r in results for d in r.documents]
        if not actual:
            print(f"  [FAIL] {golden_name}: 没有抽取到单据")
            report.append({"golden": golden_name, "fields": 0, "diffs": ["没有抽取到单据"]})
            continue
        n, diffs = compare(expected, actual[0])
        total_n += n
        total_diff += len(diffs)
        print(f"  对比 {golden_name}: {n - len(diffs)} / {n} 个字段一致")
        for d in diffs:
            print(f"    ✗ {d.field}: 期望 {d.expected!r}，实际 {d.actual!r}")
        report.append({"golden": golden_name, "fields": n,
                       "diffs": [{"field": d.field, "expected": str(d.expected), "actual": str(d.actual)} for d in diffs],
                       "extraction": [_extraction_json(r) for r in results]})

    print(f"\n总计 {total_n - total_diff} / {total_n} 个字段一致")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细已写入 {args.out}")
    return 0 if total_diff == 0 else 1


def cmd_review_eval(args: argparse.Namespace) -> int:
    from bill_agent.extract import Reviewer, group_documents
    from bill_agent.extract.review_eval import detected, mutations
    from bill_agent.llm import get_chat_model
    from bill_agent.models import ExtractedDocument

    golden_dir = Path(args.golden_dir)
    manifest = json.loads((golden_dir / "manifest.json").read_text(encoding="utf-8"))
    reviewer = Reviewer(get_chat_model(args.model))
    report, false_alarms, found, total = [], 0, 0, 0
    for golden_name, file_name in manifest.items():
        if golden_name.startswith("_"):
            continue
        src = Path(args.samples_dir) / file_name
        if not src.exists():
            print(f"\n! 找不到样例文件 {src}，跳过")
            continue
        golden = ExtractedDocument.model_validate_json((golden_dir / golden_name).read_text(encoding="utf-8"))
        [group] = group_documents(_parse_and_ocr([src], args).documents)

        print(f"\n=== {golden_name}")
        clean = reviewer.review(group, [golden])
        false_alarms += len(clean.issues)
        print(f"  golden 误报 {len(clean.issues)} 处")
        _print_review(clean)
        entry = {"golden": golden_name, "clean": _review_json(clean), "mutations": []}

        for m in mutations(golden):
            r = reviewer.review(group, [m.document])
            hit = detected(r, m)
            found += hit
            total += 1
            print(f"  [{'发现' if hit else '漏过'}] {m.name}")
            if not hit:
                _print_review(r)
            entry["mutations"].append({"name": m.name, "detected": hit, "review": _review_json(r)})
        report.append(entry)

    print(f"\ngolden 误报 {false_alarms} 处；改坏的字段发现 {found} / {total}")
    if args.out:
        _write_json(args.out, report)
        print(f"明细已写入 {args.out}")
    return 0 if false_alarms == 0 and found == total else 1


_RUN_KEYS = ("db", "entity", "model", "blob_dir", "ocr", "ocr_device", "ocr_cache", "out", "no_hitl",
             "confidence_threshold", "max_retries", "max_review_retries", "max_validation_retries")


def _run_graph(run: dict, payload, checkpoint: str) -> int:
    """按 run.json 的配置建图并执行；停在人工复核时写出 tickets.json 和待填写的 decisions.json。"""
    import sqlite3

    from bill_agent.graph import build_graph
    from bill_agent.graph.build import sqlite_checkpointer
    from bill_agent.graph.deps import make_deps

    audit = Path(run["audit_dir"])
    deps = make_deps(db_path=run["db"], entity=run["entity"] or None, model=run["model"], blob_dir=run["blob_dir"],
                     ocr=run["ocr"], ocr_device=run["ocr_device"], ocr_cache=run["ocr_cache"] or None,
                     audit_dir=str(audit), out_path=run["out"], hitl=not run["no_hitl"],
                     confidence_threshold=run["confidence_threshold"], max_retries=run["max_retries"],
                     max_review_retries=run["max_review_retries"],
                     max_validation_retries=run["max_validation_retries"])
    Path(checkpoint).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(checkpoint, check_same_thread=False)
    try:
        graph = build_graph(deps, checkpointer=sqlite_checkpointer(conn))
        config = {"configurable": {"thread_id": run["run_id"]}}
        result = graph.invoke(payload, config)
    finally:
        conn.close()

    for ev in result.get("trace", [])[-12:]:
        tok = f" | {ev.tokens} tokens" if ev.tokens else ""
        print(f"[{ev.node:12}] {ev.duration_ms:>7} ms{tok}  {ev.summary}")
    if "__interrupt__" in result:
        tickets = result["__interrupt__"][0].value["tickets"]
        _write_json(str(audit / "tickets.json"), tickets)
        template = audit / "decisions.json"
        _write_json(str(template), [{"ticket_id": t["ticket_id"], "action": "", "changes": {}, "note": None}
                                    for t in tickets])
        print(f"\n{len(tickets)} 条待人工复核：")
        for t in tickets:
            rec = t["record"]
            print(f"  {t['ticket_id']}: {rec.get('reference_no') or '-'} 第 {rec.get('line_no') or '-'} 行 "
                  f"{rec.get('original_charge_item')!r} {rec.get('amount')} {rec.get('currency')}")
            for r in t["reasons"]:
                print(f"      - {r}")
        print(f"\n明细见 {audit / 'tickets.json'}。在 {template} 中为每条填写 action（approve / edit / reject），"
              f"然后运行：\n  python -m bill_agent resume {run['run_id']}")
        return 2
    print(f"\n完成：审计记录在 {audit}" + (f"，结果写入 {run['out']}" if run["out"] else ""))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from datetime import datetime
    from uuid import uuid4

    run_id = args.run_id or f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
    audit = Path(args.audit_root) / run_id
    run = {"run_id": run_id, "audit_dir": str(audit), "inputs": args.paths,
           **{k: getattr(args, k) for k in _RUN_KEYS}}
    _write_json(str(audit / "run.json"), run)
    print(f"run_id: {run_id}\n")
    return _run_graph(run, {"inputs": args.paths, "run_id": run_id}, args.checkpoint)


def cmd_resume(args: argparse.Namespace) -> int:
    from langgraph.types import Command

    from bill_agent.graph import ReviewDecision

    audit = Path(args.audit_root) / args.run_id
    run = json.loads((audit / "run.json").read_text(encoding="utf-8"))
    path = Path(args.decisions) if args.decisions else audit / "decisions.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    try:
        decisions = [ReviewDecision.model_validate(d).model_dump(mode="json") for d in raw]
    except Exception as e:
        print(f"{path} 格式不对（action 只能是 approve / edit / reject）：{e}", file=sys.stderr)
        return 1
    return _run_graph(run, Command(resume=decisions), args.checkpoint)


def cmd_golden_eval(args: argparse.Namespace) -> int:
    from bill_agent.extract.serialize import load_extractions
    from bill_agent.harness.golden import Comparison, load_scenarios, run_scenario, write_expected

    scenarios = load_scenarios(Path(args.golden_dir))
    if args.scenario:
        scenarios = [s for s in scenarios if any(k in s.name for k in args.scenario)]
    first_batch = None
    if args.extracted:
        # 真实抽取结果只替换第一个场景的第一个批次（空库首次导入），其余场景依赖人工改写的 golden，不适用
        first_batch = load_extractions(args.extracted)
        scenarios = scenarios[:1]
    total = Comparison()
    for sc in scenarios:
        results = run_scenario(sc, Path(args.samples_dir), first_batch)
        print(f"\n== {sc.name}：{sc.description}")
        for i, r in enumerate(results, 1):
            ops = " ".join(a["operation"] for a in r.actual)
            if r.comparison is None:
                print(f"  批次 {i}（准备）：{ops}")
                continue
            c = r.comparison
            total.merge(c)
            print(f"  批次 {i}：{ops}  ->  {c.fields - len(c.diffs)} / {c.fields} 个字段一致"
                  + ("" if c.ok else "  ✗"))
            for d in c.diffs:
                print(f"    ✗ {d.key} {d.field}: 期望 {d.expected!r}，实际 {d.actual!r}")
            for m in c.missing:
                print(f"    ✗ 缺少记录 {m['reference_no'] or m['bl_nos']} 第 {m['line_no']} 行 {m['original_charge_item']!r}")
            for x in c.extra:
                print(f"    ? 多出记录 {x['reference_no'] or x['bl_nos']} 第 {x['line_no']} 行 {x['original_charge_item']!r}")
        if args.update and not args.extracted:
            write_expected(sc, results)
            print(f"  已把实际输出写为期望：{sc.path}（提交前人工核对 diff）")

    print("\n按字段：")
    for name, (good, n) in total.per_field.items():
        print(f"  {name:22} {good:3} / {n:<3} {good / n:6.1%}")
    print(f"\n总计 {total.fields - len(total.diffs)} / {total.fields} 个字段一致；"
          f"缺少 {len(total.missing)} 条，多出 {len(total.extra)} 条")
    if args.out:
        _write_json(args.out, {"fields": total.fields, "per_field": total.per_field,
                               "diffs": [vars(d) for d in total.diffs],
                               "missing": total.missing, "extra": total.extra})
    # --extracted 时多出的记录（没有 golden 的单据，如 EML）只提示，不算失败
    failed = total.diffs or total.missing or (total.extra and not args.extracted)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bill_agent")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--blob-dir", default="data/blobs", help="PDF / 图片原文件存档目录")
    common.add_argument("--ocr-device", default="cpu", choices=["cpu", "gpu"], help="PaddleOCR 运行设备")
    common.add_argument("--ocr-cache", default="data/ocr_cache", help="OCR 结果缓存目录；传空字符串关闭缓存")

    p = sub.add_parser("parse", parents=[common], help="解析输入文件，检查解析层输出")
    p.add_argument("paths", nargs="+")
    p.add_argument("--out", help="输出完整 JSON 的路径")
    p.add_argument("--ocr", action="store_true", help="解析后用本地 OCR（PDF 文字层 + PaddleOCR）识别")
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("ask", parents=[common], help="基于文件内容向 LLM 提问")
    p.add_argument("paths", nargs="+")
    p.add_argument("-q", "--question", action="append", help="问题，可重复；不给则进入交互模式")
    p.add_argument("--model", help="模型名，默认 deepseek-v4-pro（或环境变量 BILL_AGENT_MODEL）")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("qa-eval", parents=[common], help="按问答评测集检验文件内容能否支撑 LLM 回答")
    p.add_argument("qa_sets", nargs="+", help="*.qa.json 文件或所在目录")
    p.add_argument("--samples-dir", required=True, help="评测集中 file 字段所在的目录")
    p.add_argument("--model", help="模型名")
    p.add_argument("--out", help="输出评测明细 JSON 的路径")
    p.set_defaults(func=cmd_qa_eval)

    p = sub.add_parser("extract", parents=[common], help="用 LLM 从文件中抽取结构化单据信息")
    p.add_argument("paths", nargs="+")
    p.add_argument("--model", help="模型名")
    p.add_argument("--max-retries", type=int, default=3, help="代码核验失败后的最大重抽次数")
    p.add_argument("--max-review-retries", type=int, default=1, help="审核退回后的最大重抽次数")
    p.add_argument("--max-validation-retries", type=int, default=3, help="数据校验 hook 退回后的最大重抽次数")
    p.add_argument("--no-review", action="store_true", help="跳过 LLM 审核和数据校验，只做抽取和代码核验")
    p.add_argument("--out", help="输出抽取结果 JSON 的路径")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("validate", help="对已保存的抽取结果做数据校验（不调用 LLM）")
    p.add_argument("paths", nargs="+", help="extract --out 的输出、golden 标注文件或所在目录")
    p.add_argument("--out", help="输出校验明细 JSON 的路径")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("normalize", help="把已保存的抽取结果标准化为费用行记录（不调用 LLM）")
    p.add_argument("paths", nargs="+", help="extract --out 的输出、golden 标注文件或所在目录")
    p.add_argument("--out", help="输出标准化记录 JSON 的路径")
    p.add_argument("--entity", default="PAT", help="记账主体（config/entity_alias.yaml 中的名称或别名）")
    p.set_defaults(func=cmd_normalize)

    p = sub.add_parser("db-import", help="把已有账单 JSON 导入 SQLite 账单库")
    p.add_argument("paths", nargs="+", help="账单 JSON 文件或所在目录")
    p.add_argument("--db", default="data/bills.db", help="账单库路径")
    p.add_argument("--blob-dir", default="data/blobs", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_db_import)

    p = sub.add_parser("apply", help="把抽取结果标准化、匹配账单库并判定 CREATE/UPDATE/NO_CHANGE/PENDING_REVIEW")
    p.add_argument("paths", nargs="+", help="extract --out 的输出、golden 标注文件或所在目录（一次调用为一个批次）")
    p.add_argument("--db", default="data/bills.db", help="账单库路径")
    p.add_argument("--entity", default="PAT", help="记账主体")
    p.add_argument("--out", help="输出标准格式 JSON（6.9）的路径")
    p.add_argument("--strict-schema", action="store_true", help="只输出题目样例中的核心字段")
    p.set_defaults(func=cmd_apply)

    run_common = argparse.ArgumentParser(add_help=False)
    run_common.add_argument("--audit-root", default="output/runs", help="审计记录根目录，每次运行一个子目录")
    run_common.add_argument("--checkpoint", default="data/checkpoints.db", help="LangGraph checkpoint 数据库")

    p = sub.add_parser("run", parents=[common, run_common],
                       help="端到端处理：解析 -> OCR -> 抽取 -> 标准化 -> 置信度 -> 入账，待人工时暂停")
    p.add_argument("paths", nargs="+")
    p.add_argument("--db", default="data/bills.db", help="账单库路径")
    p.add_argument("--entity", default="PAT", help="记账主体；传空字符串则不判断 AR/AP")
    p.add_argument("--model", help="模型名")
    p.add_argument("--no-ocr", dest="ocr", action="store_false", help="不做 OCR（扫描件和图片不抽取）")
    p.add_argument("--out", help="输出标准格式 JSON 的路径")
    p.add_argument("--no-hitl", action="store_true", help="待人工不暂停，直接输出 PENDING_REVIEW")
    p.add_argument("--confidence-threshold", type=float, default=0.8)
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--max-review-retries", type=int, default=1)
    p.add_argument("--max-validation-retries", type=int, default=3)
    p.add_argument("--run-id", help="默认按时间生成")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("resume", parents=[run_common], help="填写人工复核结果后继续 run")
    p.add_argument("run_id")
    p.add_argument("--decisions", help="人工处理 JSON，默认为该次运行审计目录下的 decisions.json")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("extract-eval", parents=[common], help="抽取样例并与 golden 标注逐字段对比")
    p.add_argument("--samples-dir", required=True, help="题目测试集原件所在目录")
    p.add_argument("--golden-dir", default="tests/fixtures/samples", help="golden 标注与 manifest.json 所在目录")
    p.add_argument("--model", help="模型名")
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--out", help="输出对比明细 JSON 的路径")
    p.set_defaults(func=cmd_extract_eval)

    p = sub.add_parser("golden-eval", help="Golden 回归：场景输入 -> 期望输出逐字段对比（默认不调用 LLM）")
    p.add_argument("--golden-dir", default="tests/fixtures/golden", help="scenarios/ 所在目录")
    p.add_argument("--samples-dir", default="tests/fixtures/samples", help="golden 抽取标注所在目录")
    p.add_argument("--scenario", action="append", help="只跑名称包含该字符串的场景，可重复")
    p.add_argument("--extracted", nargs="+",
                   help="用 extract --out 的真实抽取结果替换第一个场景的输入，评测端到端准确率")
    p.add_argument("--update", action="store_true", help="把实际输出写成期望（需人工核对后提交）")
    p.add_argument("--out", help="输出对比明细 JSON 的路径")
    p.set_defaults(func=cmd_golden_eval)

    p = sub.add_parser("review-eval", parents=[common], help="评测审核模型的误报和发现能力")
    p.add_argument("--samples-dir", required=True, help="题目测试集原件所在目录")
    p.add_argument("--golden-dir", default="tests/fixtures/samples", help="golden 标注与 manifest.json 所在目录")
    p.add_argument("--model", help="模型名")
    p.add_argument("--out", help="输出评测明细 JSON 的路径")
    p.set_defaults(func=cmd_review_eval)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
