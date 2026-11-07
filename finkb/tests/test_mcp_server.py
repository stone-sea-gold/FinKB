"""MCP stdio server（P2）单测：协议握手、工具清单、问答/库管理工具调用。

只调用协议处理函数，不启 stdio 进程；全部离线。
"""

from __future__ import annotations

import json

import pytest

from finkb.qa import mcp_server
from finkb.qa.mcp_server import CHUNKS_PATH, FACT_DB

pytestmark = pytest.mark.skipif(
    not (FACT_DB.exists() and CHUNKS_PATH.exists()),
    reason="需要开发语料 facts.duckdb / chunks.jsonl（仓库不含数据）",
)


def _call(name: str, arguments: dict) -> tuple[dict, bool]:
    response = mcp_server.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert response["id"] == 7
    result = response["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def test_initialize():
    response = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    result = response["result"]
    assert result["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "finkb-qa"
    assert "tools" in result["capabilities"]


def test_tools_list():
    response = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert names == [
        "init_library",
        "library_status",
        "list_documents",
        "add_document",
        "remove_document",
        "rebuild_index",
        "ask",
        "compare",
        "query_metric",
        "search_documents",
        "describe_ontology",
        "traverse_relations",
        "evidence",
        "verify",
    ]


def test_notification_returns_none():
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_is_error():
    response = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "no/such"})
    assert response["error"]["code"] == -32601


def test_ask_metric_tier():
    payload, is_error = _call("ask", {"question": "比亚迪 2025 年的营业收入是多少？", "tier": "metric"})
    assert not is_error
    assert payload["tier"] == "metric"
    assert payload["refused"] is False
    assert payload["ledger"]


def test_query_metric_returns_exact_value():
    payload, is_error = _call(
        "query_metric", {"company": "002594", "period": "2025", "metric_id": "revenue"}
    )
    assert not is_error
    assert payload["found"] is True
    assert abs(payload["value"] - 8039.64958) < 1e-6
    assert payload["source_doc"] == "002594_2025_年报"


def test_describe_ontology_counts():
    payload, is_error = _call("describe_ontology", {})
    assert not is_error
    assert payload["counts"]["concepts"] == 62
    assert payload["counts"]["defined_classes"] == 8
    assert "L2" in payload["layers"]


def test_traverse_relations_subsidiary():
    payload, is_error = _call(
        "traverse_relations", {"entity": "比亚迪", "rel_type": "subsidiary_of", "limit": 5}
    )
    assert not is_error
    assert payload["found"] is True
    assert payload["edges"]
    assert all(edge["rel_type"] == "subsidiary_of" for edge in payload["edges"])


def test_search_documents_with_filter():
    payload, is_error = _call(
        "search_documents", {"query": "海外 收入", "limit": 3, "company": "002594"}
    )
    assert not is_error
    assert payload["results"]
    assert payload["results"][0]["company"] == "002594"


def test_library_tools_dispatch():
    payload, is_error = _call("list_documents", {})
    assert not is_error
    assert isinstance(payload["documents"], list)
    payload, is_error = _call("add_document", {"company": "bad", "period": "2025", "report_type": "年报"})
    assert is_error
    assert "error" in payload


def test_evidence_tool_returns_pack():
    payload, is_error = _call("evidence", {"question": "比亚迪 2025 年报里，公司如何描述海外市场的区域布局？"})
    assert not is_error
    assert payload["tier"] == "document"
    evidence = payload["evidence"]
    assert evidence["passages"] and evidence["citations"]


def test_verify_tool_accepts_host_answer():
    question = "比亚迪 2025 年报里，公司如何描述海外市场的区域布局？"
    pack_payload, _ = _call("evidence", {"question": question})
    ref = pack_payload["evidence"]["citations"][0]
    answer = {"answer": f"公司持续推进海外布局，海外业务覆盖多个国家。{ref}", "used_ids": [],
              "refused": False, "missing": [], "notes": []}
    payload, is_error = _call("verify", {"question": question, "answer": answer})
    assert not is_error
    assert payload["ok"] is True
    assert payload["checks"]["V5"] is True


def test_ask_accepts_context():
    first, _ = _call("ask", {"question": "比亚迪 2025 年的营业收入是多少？", "tier": "metric"})
    payload, is_error = _call(
        "ask",
        {
            "question": "那 2024 年呢？",
            "tier": "metric",
            "context": {"previous_slots": first["resolved_slots"]},
        },
    )
    assert not is_error
    assert payload["resolved_question"].startswith("比亚迪 2024 年")
    assert "company" in payload["inherited"] and "metric" in payload["inherited"]
