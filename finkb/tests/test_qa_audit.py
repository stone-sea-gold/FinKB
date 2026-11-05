"""问答审计日志（P2）单测：JSONL 追加、字段、路径覆盖。

全部离线，只写临时目录。
"""

from __future__ import annotations

import json

from finkb.qa import audit_log

PAYLOAD = {
    "question": "比亚迪 2025 年的营业收入是多少？",
    "tier": "metric",
    "confidence": 0.95,
    "refused": False,
    "route": "fact",
    "citations": ["002594_2025_年报#P11"],
    "trace": [{"step": "route.select", "tier": "metric"}],
}


def test_append_writes_jsonl(tmp_path):
    path = tmp_path / "audit.jsonl"
    written = audit_log.log(PAYLOAD, path=path)
    assert written == path
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["question"] == PAYLOAD["question"]
    assert record["tier"] == "metric"
    assert record["refused"] is False
    assert record["citations"] == ["002594_2025_年报#P11"]
    assert record["trace"][0]["step"] == "route.select"
    assert "ts" in record


def test_append_keeps_history(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit_log.log(PAYLOAD, path=path)
    audit_log.log({**PAYLOAD, "refused": True, "tier": "document"}, path=path)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_env_override(tmp_path, monkeypatch):
    path = tmp_path / "env.jsonl"
    monkeypatch.setenv("FINKB_QA_AUDIT", str(path))
    assert audit_log.log(PAYLOAD) == path
    assert path.exists()
