"""本地知识库管理（library）单测：校验、目录、注册表、导入/删除、MCP 工具。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from finkb import library
from finkb.common import paths


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "HOME", tmp_path)
    paths.ensure_home()
    return tmp_path


@pytest.mark.parametrize(
    "period,report_type",
    [
        ("2025", "年报"),
        ("2025H1", "半年报"),
        ("2025Q1", "一季报"),
        ("2025Q3", "三季报"),
    ],
)
def test_meta_accepts_standard_periods(period, report_type):
    meta = library._meta("600519", period, report_type)
    assert meta["doc_id"] == f"600519_2025_{report_type}"
    assert meta["year"] == "2025"


def test_meta_rejects_bad_input():
    with pytest.raises(ValueError):
        library._meta("60051", "2025", "年报")
    with pytest.raises(ValueError):
        library._meta("600519", "2025", "月报")
    with pytest.raises(ValueError):
        library._meta("600519", "2025Q1", "年报")
    with pytest.raises(ValueError):
        library._meta("600519", "2025H1", "一季报")


def test_ensure_home_creates_layout_and_templates(home):
    for directory in (home / "data" / "parsed", home / "data" / "_search", home / "config", home / "raw", home / "wiki_vault"):
        assert directory.is_dir()
    assert (home / "config" / "metric_dict.yaml").exists()
    assert (home / "config" / ".env.example").exists()
    scope = library._read_scope()
    assert scope["docs"] == []


def test_package_config_dir_falls_back_to_install_prefix(tmp_path, monkeypatch):
    share = tmp_path / "share" / "finkb" / "config"
    share.mkdir(parents=True)
    (share / "metric_dict.yaml").write_text("version: 3\n", encoding="utf-8")
    monkeypatch.setattr(paths, "PACKAGE_ROOT", tmp_path / "no-config-here")
    monkeypatch.setattr(paths.sys, "prefix", str(tmp_path))
    assert paths.package_config_dir() == share


def test_scope_upsert_and_remove(home):
    entry = {"doc_id": "600519_2025_年报", "company": "600519", "company_name": "贵州茅台",
             "period": "2025", "report_type": "年报"}
    library._upsert_scope(entry)
    assert library._read_scope()["docs"] == [entry]
    library._upsert_scope({**entry, "company_name": "茅台"})
    assert library._read_scope()["docs"][0]["company_name"] == "茅台"
    library._remove_scope(entry["doc_id"])
    assert library._read_scope()["docs"] == []


def test_registry_roundtrip(home):
    library._write_registry({"version": 1, "documents": {"600519_2025_年报": {"doc_id": "600519_2025_年报"}}})
    docs = library._documents()
    assert "600519_2025_年报" in docs
    assert json.loads(library._registry_path().read_text(encoding="utf-8"))["documents"]


def test_list_documents_falls_back_to_scope(home):
    entry = {"doc_id": "600519_2025_年报", "company": "600519", "company_name": "贵州茅台",
             "period": "2025", "report_type": "年报"}
    library._upsert_scope(entry)
    docs = library.list_documents()
    assert [d["doc_id"] for d in docs] == [entry["doc_id"]]
    assert docs[0]["source"] == "existing"


def test_add_and_remove_document_from_parsed(home):
    source = Path(__file__).resolve().parents[1] / "data" / "parsed" / "601127_2025_一季报"
    if not (source / "report.json").exists():
        pytest.skip("缺少已解析样例")
    result = library.add_document(
        company="601127", period="2025Q1", report_type="一季报",
        parsed_dir=source, company_name="赛力斯", skip_facts=True, skip_wiki=True,
    )
    assert result["ok"] is True
    doc_id = "601127_2025_一季报"
    assert result["doc_id"] == doc_id
    assert (home / "data" / "parsed" / doc_id / "report.json").exists()
    assert result["chunks"]["chunks"] > 0
    assert library._read_registry()["documents"][doc_id]["company_name"] == "赛力斯"
    assert [d["doc_id"] for d in library.list_documents()] == [doc_id]

    removed = library.remove_document(doc_id)
    assert removed["ok"] is True
    assert not (home / "data" / "parsed" / doc_id).exists()
    assert library.list_documents() == []
    assert library._read_registry()["documents"] == {}


def test_add_document_accepts_parsed_dir_inside_library(home):
    """--parsed 直接指向库内目录时不能先删后拷（会把源删掉）。"""
    source = Path(__file__).resolve().parents[1] / "data" / "parsed" / "601127_2025_一季报"
    if not (source / "report.json").exists():
        pytest.skip("缺少已解析样例")
    target = home / "data" / "parsed" / "601127_2025_一季报"
    shutil.copytree(source, target)
    result = library.add_document(
        company="601127", period="2025Q1", report_type="一季报",
        parsed_dir=target, company_name="赛力斯", skip_facts=True, skip_wiki=True,
    )
    assert result["ok"] is True
    assert (target / "report.json").exists()
    assert result["chunks"]["chunks"] > 0
    library.remove_document("601127_2025_一季报")


def test_status_reports_home(home):
    info = library.status()
    assert info["home"] == str(home)
    assert info["initialized"] is True
    assert info["custom_home"] is True


def test_mcp_library_tools_are_callable():
    from finkb.qa import mcp_server

    response = mcp_server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                  "params": {"name": "library_status", "arguments": {}}})
    payload = json.loads(response["result"]["content"][0]["text"])
    assert "home" in payload and "documents" in payload
    assert response["result"]["isError"] is False
