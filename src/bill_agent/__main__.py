"""命令行入口。

    python -m bill_agent parse <文件或目录>... [--blob-dir data/blobs] [--out parsed.json]
                               [--ocr [--ocr-device cpu|gpu]]
    python -m bill_agent ask <文件或目录>... [-q 问题]... [--model deepseek-v4-pro]
    python -m bill_agent qa-eval <评测集文件或目录>... --samples-dir <样例目录> [--out report.json]
    python -m bill_agent extract <文件或目录>... [--out extracted.json] [--max-retries 3]
                                 [--max-review-retries 1] [--max-validation-retries 3] [--no-review]
    python -m bill_agent validate <抽取结果 JSON 或目录>... [--out report.json]
    python -m bill_agent extract-eval --samples-dir <样例目录> [--out report.json]
    python -m bill_agent review-eval --samples-dir <样例目录> [--out report.json]

parse：把输入文件集解析成 RawDocument，打印摘要并（可选）输出完整 JSON，用于检查解析层效果。
       PDF / 图片登记为 pending_ocr，原文件存档到 --blob-dir；加 --ocr 时接着用本地 OCR 识别。
ask：解析 + OCR 后基于文件内容向 LLM 提问；不给 -q 时进入交互模式。
qa-eval：按评测集逐题提问并判分，检验解析 / OCR 结果能否支撑 LLM 正确回答。
extract：结构化抽取 skill（抽取 + 代码核验 + LLM 审核），调用经过数据校验 hook；超过重试上限标为待人工复核。
validate：对已保存的抽取结果做数据校验，不调用 LLM。
extract-eval：抽取 golden 标注对应的样例，逐字段对比（只评测抽取，不经过审核）。
review-eval：评测审核模型：golden 不应被误报，故意改坏的字段应被发现。
OCR 结果按文件哈希缓存在 --ocr-cache（默认 data/ocr_cache），同一文件不重复识别。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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


def _extraction_json(r) -> dict:
    return {"source_paths": r.source_paths, "attempts": r.attempts, "errors": r.errors,
            "attempt_errors": r.attempt_errors, "attempt_modes": r.attempt_modes, "cleared": r.cleared,
            "patches": r.patches,
            "documents": [d.model_dump(mode="json") for d in r.documents], "raw_outputs": r.raw_outputs}


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


def _validation_json(validation) -> dict:
    return {"issues": [{"code": i.code, "severity": i.severity.value, "doc": i.doc, "field": i.field,
                        "message": i.message} for i in validation.issues]}


def _review_json(review) -> dict:
    return {"issues": [vars(i) for i in review.issues], "dismissed": [vars(i) for i in review.dismissed],
            "incomplete": review.incomplete, "attempts": review.attempts, "raw_outputs": review.raw_outputs}


def _gate_json(g) -> dict:
    return {"source_paths": g.source_paths, "status": g.status.value, "reasons": g.reasons,
            "warnings": g.warnings, "documents": [d.model_dump(mode="json") for d in g.documents],
            "rounds": [{"extraction": _extraction_json(r.extraction),
                        "review": r.review and _review_json(r.review),
                        "validation": r.validation and _validation_json(r.validation)} for r in g.rounds]}


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

    p = sub.add_parser("extract-eval", parents=[common], help="抽取样例并与 golden 标注逐字段对比")
    p.add_argument("--samples-dir", required=True, help="题目测试集原件所在目录")
    p.add_argument("--golden-dir", default="tests/fixtures/samples", help="golden 标注与 manifest.json 所在目录")
    p.add_argument("--model", help="模型名")
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--out", help="输出对比明细 JSON 的路径")
    p.set_defaults(func=cmd_extract_eval)

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
