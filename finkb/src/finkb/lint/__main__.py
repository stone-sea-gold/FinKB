"""``python -m finkb.lint [--fix]``：跑 9 条规则并写 reports/lint-*.md。"""

from __future__ import annotations

import argparse
import datetime as _dt
from pathlib import Path

from finkb.common import paths
from finkb.lint.rules import lint_vault, render_report
from finkb.wiki.vault import DEFAULT_SCOPE, DEFAULT_VAULT

FINKB_ROOT = paths.HOME
DATA = paths.data_dir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m finkb.lint")
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--data-root", type=Path, default=DATA)
    parser.add_argument("--claims-db", type=Path, default=DATA / "wiki.duckdb")
    parser.add_argument("--facts-db", type=Path, default=DATA / "facts.duckdb")
    parser.add_argument("--scope", type=Path, default=DEFAULT_SCOPE)
    parser.add_argument("--queries", type=Path, default=paths.config_dir() / "wiki_queries.yaml")
    parser.add_argument("--fix", action="store_true", help="修复断链（降级成纯文本）")
    args = parser.parse_args(argv)

    issues = lint_vault(
        args.vault,
        data_root=args.data_root,
        claims_db=args.claims_db,
        facts_db=args.facts_db,
        scope_path=args.scope,
        queries_path=args.queries,
        fix=args.fix,
    )
    text = render_report(issues)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    report = args.vault / "reports" / f"lint-{stamp}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(text, encoding="utf-8")
    print(text)
    print(f"报告：{report}")
    return 1 if any(i.severity == "error" for i in issues) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
