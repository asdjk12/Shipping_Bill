"""命令行入口。

    python -m bill_agent parse <文件或目录>... [--blob-dir data/blobs] [--out parsed.json]

把输入文件集解析成 RawDocument，打印摘要并（可选）输出完整 JSON，用于检查解析层效果。
PDF / 图片登记为 pending_ocr，原文件存档到 --blob-dir。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bill_agent.parsers import RouterConfig, build_default_router


def cmd_parse(args: argparse.Namespace) -> int:
    router = build_default_router(RouterConfig(blob_dir=args.blob_dir))
    result = router.route_paths(args.paths)

    pending = sum(1 for d in result.documents if not d.ready_for_extraction)
    print(f"\n解析得到 {len(result.documents)} 份文档（其中 {pending} 份待 OCR），"
          f"{len(result.bill_jsons)} 个账单 JSON，跳过 {len(result.skipped)} 项\n")
    for d in result.documents:
        print(f"[{d.file_type.value:5}] {d.source_path}  ({d.status.value})")
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bill_agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse", help="解析输入文件，检查解析层输出")
    p.add_argument("paths", nargs="+")
    p.add_argument("--blob-dir", default="data/blobs", help="PDF / 图片原文件存档目录")
    p.add_argument("--out", help="输出完整 JSON 的路径")
    p.set_defaults(func=cmd_parse)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
