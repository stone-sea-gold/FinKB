"""统一 CLI（``finkb ask/search/graph/eval/status``）单测。

全部离线：ask 走本地 DuckDB + 抽取式答案；search 用 --no-dense 只读 FTS；
graph/status 只读本地库。eval 只测参数解析，不实际跑评测。
"""

from __future__ import annotations

import json

import pytest

from finkb.cli import main


def test_ask_metric(capsys):
    code = main(["ask", "比亚迪 2025 年营业收入是多少亿元？", "--tier", "metric"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Tier 1" in out
    assert "8039.65" in out
    assert "校验：通过" in out


def test_ask_json(capsys):
    code = main(["ask", "比亚迪 2025 年营业收入是多少亿元？", "--tier", "metric", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["tier"] == "metric"
    assert payload["refused"] is False
    assert payload["ledger"]


def test_search_offline(capsys):
    code = main(
        ["search", "海外 布局", "--company", "002594", "--period", "2025", "--limit", "2", "--no-dense"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "002594_2025_年报" in out
    assert "fts" in out


def test_graph_entity(capsys):
    code = main(["graph", "比亚迪", "--limit", "3"])
    out = capsys.readouterr().out
    assert code == 0
    assert "关系边" in out
    assert "subsidiary_of" in out


def test_graph_overview(capsys):
    code = main(["graph", "--overview"])
    out = capsys.readouterr().out
    assert code == 0
    assert "本体与关系图概览" in out
    assert "概念" in out
    assert "公司关系边" in out


def test_graph_concept(capsys):
    code = main(["graph", "--concept", "self:gross_margin"])
    out = capsys.readouterr().out
    assert code == 0
    assert "gross_margin" in out


def test_status(capsys):
    code = main(["status"])
    out = capsys.readouterr().out
    assert code == 0
    assert "事实" in out
    assert "切块" in out


def test_unknown_command_exits():
    with pytest.raises(SystemExit):
        main([])
