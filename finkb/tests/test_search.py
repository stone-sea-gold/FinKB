"""L2 主检索：FTS5 建索引、bm25 排序、别名扩展与过滤（ADR-002）。"""

from __future__ import annotations

import pytest

from finkb.search import build_index, expand_query, search
from finkb.wiki import frontmatter, vault


def _meta(page_type, title, *, company=None, period=None, tags=None, status="active"):
    return frontmatter.build(
        page_type=page_type,
        title=title,
        description=f"{title} 的一句话摘要",
        sources=["l0:002594_2025_年报", "l1:facts#000001"],
        company=company,
        period=period,
        tags=tags,
        status=status,
    )


@pytest.fixture()
def vault_root(tmp_path):
    """造 4 个 active 页 + 1 个 draft 页；每个词只出现在想验证的字段里。"""
    root = vault.init_vault(tmp_path / "wiki_vault")
    # 甲：正文含「营业收入」，标题不含（用来验证别名扩展与正文命中）
    vault.write_page(
        root,
        "summary",
        "jia",
        _meta("summary", "甲页总览", company="002594", period="2025", tags=["财务"]),
        "本页正文只写营业收入 8,039.65 亿元。",
    )
    # 乙：「现金流」只在标题里（用来验证标题命中）
    vault.write_page(
        root,
        "concept",
        "yi",
        _meta("concept", "现金流结构", tags=["现金流"]),
        "本页讲经营周转与资金安排。",
    )
    # 丙：「现金流」与「资本开支」只在正文里（用来验证标题加权与正文命中）
    vault.write_page(
        root,
        "concept",
        "bing",
        _meta("concept", "成本结构"),
        "本页也提到现金流；资本开支只在正文里出现一次。",
    )
    # 丁：draft 页，标题含「现金流」（用来验证 draft 降权但不丢弃）
    vault.write_page(
        root,
        "summary",
        "ding",
        _meta("summary", "现金流草稿", status="draft"),
        "草稿页面也讲现金流。",
    )
    # 戊：仅标题含「产业链」
    vault.write_page(
        root,
        "concept",
        "chan",
        _meta("concept", "产业链布局"),
        "本页讲整车与电池。",
    )
    return root


@pytest.fixture()
def index_db(tmp_path, vault_root):
    db = tmp_path / "search.db"
    build_index(vault_root, db)
    return db


def test_标题命中(index_db):
    rows = search("产业链", db_path=index_db)
    assert rows
    path, title, snippet, score = rows[0]
    assert path == "concepts/chan.md"
    assert title == "产业链布局"
    assert "<<产业链>>" in snippet
    assert score < 0  # FTS5 bm25 原值：越小越相关


def test_正文命中(index_db):
    rows = search("资本开支", db_path=index_db)
    assert [row[0] for row in rows] == ["concepts/bing.md"]
    assert "<<资本开支>>" in rows[0][2]


def test_别名扩展把口语词映射成全称(index_db):
    # 字典里 revenue 的 alias 有「营收」，页面正文写的是「营业收入」
    assert "营业收入" in expand_query("营收")
    rows = search("营收", db_path=index_db)
    assert rows[0][0] == "summaries/jia.md"


def test_标题命中排在正文命中之前(index_db):
    rows = search("现金流", db_path=index_db)
    paths = [row[0] for row in rows]
    assert paths[0] == "concepts/yi.md"  # 标题命中
    assert "concepts/bing.md" in paths  # 正文命中也被召回
    assert paths.index("concepts/yi.md") < paths.index("concepts/bing.md")


def test_draft_降权但不丢弃(index_db):
    rows = search("现金流", db_path=index_db)
    paths = [row[0] for row in rows]
    assert "_draft/summaries/ding.md" in paths  # draft 也建了索引
    assert paths[-1] == "_draft/summaries/ding.md"  # 但排在 active 之后
    drafts = search("现金流", filters={"status": "draft"}, db_path=index_db)
    assert [row[0] for row in drafts] == ["_draft/summaries/ding.md"]


def test_filters按类型公司与标签过滤(index_db):
    assert [r[0] for r in search("现金流", filters={"type": "concept"}, db_path=index_db)] == [
        "concepts/yi.md",
        "concepts/bing.md",
    ]
    assert [r[0] for r in search("营收", filters={"company": "002594"}, db_path=index_db)] == [
        "summaries/jia.md"
    ]
    assert [r[0] for r in search("营收", filters={"tag": "财务"}, db_path=index_db)] == [
        "summaries/jia.md"
    ]
    with pytest.raises(ValueError):
        search("营收", filters={"unknown": "x"}, db_path=index_db)


def test_重建幂等且能反映新页面(tmp_path, vault_root, index_db):
    first = search("现金流", db_path=index_db)
    assert build_index(vault_root, index_db) == 5
    assert search("现金流", db_path=index_db) == first  # 重建后逐行一致

    vault.write_page(
        vault_root, "concept", "liu", _meta("concept", "量子计算进展"), "量子计算与量子芯片。"
    )
    assert build_index(vault_root, index_db) == 6
    assert [r[0] for r in search("量子计算", db_path=index_db)] == ["concepts/liu.md"]


def test_无结果与空查询返回空列表(index_db):
    assert search("量子计算", db_path=index_db) == []
    assert search("", db_path=index_db) == []
    assert search("   ", db_path=index_db) == []


def test_查询含数字标点不报语法错(index_db):
    # trigram 建索引、查询串整体加引号：8,039.65 里的逗号不能当 FTS5 语法
    rows = search("8,039.65", db_path=index_db)
    assert [row[0] for row in rows] == ["summaries/jia.md"]


def test_索引不存在时给出明确错误(tmp_path):
    with pytest.raises(FileNotFoundError):
        search("现金流", db_path=tmp_path / "nope.db")
