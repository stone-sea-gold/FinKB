"""MCP stdio server（P2）单测：协议握手、工具清单、5 个工具调用。

只调用协议处理函数，不启 stdio 进程；全部离线。
"""

from __future__ import annotations

import json

from finkb.qa import mcp_server


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


def test_tools_list_five():
    response = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert names == [
        "ask",
        "query_metric",
        "search_documents",
        "describe_ontology",
        "traverse_relations",
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
