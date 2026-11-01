"""命令行检索入口：``python -m finkb.search "查询词"``。

只做三件事：必要时重建索引 → 调 ``search`` → 按相关度打印排名。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from finkb.search.index import DEFAULT_DB, build_index, search
from finkb.wiki.vault import DEFAULT_VAULT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m finkb.search",
        description="检索 FinKB 知识页（SQLite FTS5 + 中文别名扩展）",
    )
    parser.add_argument("query", help="查询词；2 字口语词（如「营收」）会自动扩展成指标全称")
    parser.add_argument("--rebuild", action="store_true", help="先全量重建索引")
    parser.add_argument(
        "--vault", type=Path, default=DEFAULT_VAULT, help=f"知识页目录（默认 {DEFAULT_VAULT}）"
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB, help=f"索引库路径（默认 {DEFAULT_DB}）"
    )
    parser.add_argument("--limit", type=int, default=10, help="最多返回几条（默认 10）")
    args = parser.parse_args(argv)

    # 索引库不存在（例如第一次跑、或 data/ 被清掉）时自动建，省得用户先记两条命令
    if args.rebuild or not args.db.exists():
        count = build_index(args.vault, args.db)
        print(f"已重建索引：{count} 页 → {args.db}")

    results = search(args.query, args.limit, db_path=args.db)
    if not results:
        print(f"没有命中：{args.query}")
        return 0

    print(f"命中 {len(results)} 条（相关度越小越相关）：")
    for rank, (path, title, snippet, score) in enumerate(results, start=1):
        print(f"{rank}. {path}  相关度 {score:.2e}")
        print(f"   标题：{title}")
        # snippet 里带原文换行，拍平成一行方便终端阅读
        print(f"   片段：{' '.join(snippet.split())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
