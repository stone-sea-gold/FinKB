"""问答审计日志（P2）：可选 JSONL 落盘，便于复盘档位/拒答/引用。

默认路径 ``data/qa_audit.jsonl``（不进 git）；环境变量 ``FINKB_QA_AUDIT`` 可覆盖。
只追加，不改问答结果。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from finkb.fact_engine.selector import FINKB_ROOT

DEFAULT_PATH = FINKB_ROOT / "data" / "qa_audit.jsonl"


def log(payload: dict, path: Path | str | None = None) -> Path | None:
    """把一条问答结果追加到 JSONL；``enabled=False`` 的调用方自行跳过。"""
    target = Path(path or os.environ.get("FINKB_QA_AUDIT") or DEFAULT_PATH)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "question": payload.get("question"),
        "tier": payload.get("tier"),
        "confidence": payload.get("confidence"),
        "refused": payload.get("refused", False),
        "route": payload.get("route"),
        "citations": payload.get("citations", []),
        "trace": payload.get("trace", []),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return target
