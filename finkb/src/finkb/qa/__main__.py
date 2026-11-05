"""CLI：python -m finkb.qa "问题" [--json] [--tier auto|metric|ontology|document] [--explain]

与 ``finkb ask`` 共用 ``finkb.qa.console.run`` 的渲染逻辑。
"""

from __future__ import annotations

import argparse

from finkb.qa.console import run


def main() -> None:
    parser = argparse.ArgumentParser(description="FinKB 三档问答（抽取式，离线）")
    parser.add_argument("question")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--tier",
        choices=("auto", "metric", "ontology", "document"),
        default="auto",
        help="只跑指定档位；默认 auto（按契约逐级回落）",
    )
    parser.add_argument("--explain", action="store_true", help="打印档位选择与剪枝过程")
    parser.add_argument(
        "--audit-log",
        action="store_true",
        help="把本次问答追加到 data/qa_audit.jsonl（可用 FINKB_QA_AUDIT 指定路径）",
    )
    args = parser.parse_args()
    text, code = run(
        args.question,
        tier=args.tier,
        explain=args.explain,
        as_json=args.json,
        audit=args.audit_log,
    )
    print(text)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
