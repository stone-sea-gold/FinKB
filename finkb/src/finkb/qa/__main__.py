"""CLI：python -m finkb.qa "问题" [--json] [--tier auto|metric|ontology|document] [--explain]"""

from __future__ import annotations

import argparse
import json

from finkb.qa.audit_log import log as audit_log
from finkb.qa.pipeline import TIER_LABELS, ask
from finkb.qa.synthesizer import render
from finkb.qa.verifier import verify


def main() -> None:
    parser = argparse.ArgumentParser(description="FinKB 三档问答（抽取式，离线）")
    parser.add_argument("question")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--tier", choices=("auto", "metric", "ontology", "document"), default="auto",
                        help="只跑指定档位；默认 auto（按契约逐级回落）")
    parser.add_argument("--explain", action="store_true", help="打印档位选择与剪枝过程")
    parser.add_argument("--audit-log", action="store_true",
                        help="把本次问答追加到 data/qa_audit.jsonl（可用 FINKB_QA_AUDIT 指定路径）")
    args = parser.parse_args()

    payload = ask(args.question, tier=args.tier)
    if args.audit_log:
        audit_log({**payload, "question": args.question})
    rendered, used = render(payload.get("text", ""), payload.get("ledger", []))
    check = verify({**payload, "text": rendered})
    if args.json:
        print(json.dumps({**payload, "text": rendered, "used_ledger": used, "verify": check},
                         ensure_ascii=False, indent=2))
        return
    tier = payload.get("tier", "?")
    print(f"档位：{TIER_LABELS.get(tier, tier)}（{tier}）　置信度：{payload.get('confidence', 0):.2f}"
          f"　{'（拒答）' if payload.get('refused') else ''}")
    if args.explain:
        for step in payload.get("trace", []):
            print("  " + json.dumps(step, ensure_ascii=False))
    print(f"答案：{rendered}")
    if payload.get("ledger"):
        print("账本：")
        for entry in payload["ledger"]:
            print(f"  {entry['id']} = {entry['value']:.4f} {entry.get('unit', '')}  [{entry.get('kind', 'fact')}]"
                  + (f"  ← {entry.get('doc')}#P{entry.get('page')}" if entry.get("doc") else ""))
    if payload.get("citations"):
        print("引用：" + " ".join(payload["citations"]))
    print(f"校验：{'通过' if check['ok'] else '未通过 ' + str(check['issues'])}")


if __name__ == "__main__":
    main()
