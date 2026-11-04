"""CLI：python -m finkb.qa "问题" [--json]"""

from __future__ import annotations

import argparse
import json

from finkb.qa.answer import answer
from finkb.qa.synthesizer import render
from finkb.qa.verifier import verify


def main() -> None:
    parser = argparse.ArgumentParser(description="FinKB 三路问答（抽取式，离线）")
    parser.add_argument("question")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = answer(args.question)
    rendered, used = render(payload.get("text", ""), payload.get("ledger", []))
    check = verify({**payload, "text": rendered})
    if args.json:
        print(json.dumps({**payload, "text": rendered, "used_ledger": used, "verify": check}, ensure_ascii=False, indent=2))
        return
    print(f"路由：{payload['route']}　{'（拒答）' if payload.get('refused') else ''}")
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
