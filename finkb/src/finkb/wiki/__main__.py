"""``python -m finkb.wiki init|index|compile``：建骨架、重算索引、编译页面。"""

from __future__ import annotations

import argparse
from pathlib import Path

from finkb.wiki.vault import DEFAULT_VAULT, init_vault, rebuild_index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m finkb.wiki")
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="建目录骨架与空索引")
    sub.add_parser("index", help="重算 index.md")
    compile_cmd = sub.add_parser("compile", help="编译一份文档的 wiki 页面")
    compile_cmd.add_argument("--doc", required=True, help="文档 id，如 002594_2025_年报")
    compile_cmd.add_argument("--company", default=None)
    compile_cmd.add_argument("--period", default=None)
    compile_cmd.add_argument("--db", type=Path, default=None)
    compile_cmd.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "init":
        root = init_vault(args.vault)
        print(f"已初始化 {root}")
        return 0
    if args.command == "index":
        root = init_vault(args.vault)
        text = rebuild_index(root)
        print(f"已重算 {root / 'index.md'}（{text.count('- [[')} 条）")
        return 0

    from finkb.fact_engine.extractor import LLMClient, LLMConfig
    from finkb.fact_engine.selector import FINKB_ROOT, doc_period, load_manifest
    from finkb.fact_engine.store import FactStore
    from finkb.wiki.compiler import compile_document

    doc_id = args.doc
    code, year, kind = doc_id.split("_", 2)
    company = args.company or (load_manifest().get((code, year, kind)) or {}).get("公司名称") or code
    period = args.period or doc_period(doc_id)
    db = args.db or (FINKB_ROOT / "data" / "facts.duckdb")
    with FactStore(db) as store, LLMClient(LLMConfig.from_env()) as client:
        results = compile_document(
            client,
            store,
            doc_id,
            company=company,
            period=period,
            vault_root=args.vault,
            data_root=FINKB_ROOT / "data",
            dry_run=args.dry_run,
        )
    for item in results:
        print(f"{item['slug']}: {item['status']}" + (f" — {item.get('reason')}" if item.get("reason") else ""))
    if not args.dry_run:
        root = init_vault(args.vault)
        text = rebuild_index(root)
        print(f"已重算索引（{text.count('- [[')} 条）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
