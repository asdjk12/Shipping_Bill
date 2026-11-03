"""命令行入口。

    python -m bill_agent parse <文件或目录>... [--blob-dir data/blobs] [--out parsed.json]
                               [--ocr [--ocr-device cpu|gpu]]
    python -m bill_agent ask <文件或目录>... [-q 问题]... [--model deepseek-v4-pro]
    python -m bill_agent qa-eval <评测集文件或目录>... --samples-dir <样例目录> [--out report.json]

parse：把输入文件集解析成 RawDocument，打印摘要并（可选）输出完整 JSON，用于检查解析层效果。
       PDF / 图片登记为 pending_ocr，原文件存档到 --blob-dir；加 --ocr 时接着用本地 OCR 识别。
ask：解析 + OCR 后基于文件内容向 LLM 提问；不给 -q 时进入交互模式。
qa-eval：按评测集逐题提问并判分，检验解析 / OCR 结果能否支撑 LLM 正确回答。
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

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
