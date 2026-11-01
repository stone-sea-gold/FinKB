"""L2 lint 规则的单测（不依赖真实语料）。"""

from __future__ import annotations

import duckdb
import pytest

from finkb.lint import rules
from finkb.wiki import frontmatter, vault
from finkb.wiki.claims import Claim, ClaimStore


def _meta(**overrides):
    meta = frontmatter.build(
        page_type="summary",
        title="示例",
        description="摘要",
        sources=["l0:doc", "l1:facts#000001"],
    )
    meta.update(overrides)
    return meta


def _vault(tmp_path):
    return vault.init_vault(tmp_path / "vault")


def _data_root(tmp_path, pages=5, anchors=(1, 2)):
    root = tmp_path / "data"
    doc = root / "parsed" / "doc"
    doc.mkdir(parents=True)
    (doc / "report.json").write_text(f'{{"pages": {pages}}}', encoding="utf-8")
    text = "\n".join(f'<!-- META: {{"doc": "doc", "page": {n}}} -->\n正文' for n in anchors)
    (doc / "page_anchored.md").write_text(text, encoding="utf-8")
    return root


def _facts_db(tmp_path, rows=((1, 10.0, "亿元"),)):
    path = tmp_path / "facts.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE facts (fact_id BIGINT, value_norm DOUBLE, norm_unit VARCHAR)")
    conn.executemany("INSERT INTO facts VALUES (?, ?, ?)", rows)
    conn.close()
    return path


def test_rule1_and_fix_broken_links(tmp_path):
    root = _vault(tmp_path)
    vault.write_page(root, "summary", "a", _meta(), "见 [[concepts/missing|缺失页]]。")
    issues = rules.rule1_broken_links(root)
    assert any(i.rule == "R1" for i in issues)
    assert rules.fix_broken_links(root) == 1
    assert "缺失页" in (root / "summaries" / "a.md").read_text(encoding="utf-8")
    assert rules.rule1_broken_links(root) == []


def test_rule4_missing_entries(tmp_path):
    root = _vault(tmp_path)
    scope = tmp_path / "scope.yaml"
    scope.write_text("version: 1\ndocs:\n  - doc_id: doc\n", encoding="utf-8")
    assert any(i.rule == "R4" for i in rules.rule4_missing_entries(root, scope))
    vault.write_page(root, "summary", "doc", _meta(), "正文")
    assert rules.rule4_missing_entries(root, scope) == []


def test_rule5_index_sync(tmp_path):
    root = _vault(tmp_path)
    vault.write_page(root, "summary", "a", _meta(), "正文")
    assert any(i.rule == "R5" for i in rules.rule5_index_sync(root))
    vault.rebuild_index(root)
    assert rules.rule5_index_sync(root) == []


def test_rule8_anchor_resolvable(tmp_path):
    root = _vault(tmp_path)
    data = _data_root(tmp_path, pages=5, anchors=(1, 2))
    vault.write_page(root, "summary", "a", _meta(), "见 [Ref: doc#P2]。")
    assert rules.rule8_anchors(root, data) == []
    vault.write_page(root, "summary", "b", _meta(), "见 [Ref: doc#P9]。")
    assert any(i.rule == "R8" for i in rules.rule8_anchors(root, data))


def test_rule9_fact_claim_ok_and_mismatch(tmp_path):
    root = _vault(tmp_path)
    data = _data_root(tmp_path)
    facts = _facts_db(tmp_path)
    claims_path = tmp_path / "wiki.duckdb"
    page = "summaries/a.md"
    body = "营业收入 10.00[^c1] 亿元。\n\n[^c1]: 数值来源：facts #1。\n"
    vault.write_page(root, "summary", "a", _meta(), body)
    with ClaimStore(claims_path) as store:
        store.insert_many([Claim(page=page, ordinal=1, kind="fact", fact_id=1, display_value="10.00", unit="亿元")])
    kwargs = dict(
        claims_db=claims_path,
        facts_db=facts,
        data_root=data,
        queries_path=tmp_path / "queries.yaml",
    )
    assert rules.rule9_claims(root, **kwargs) == []

    with ClaimStore(claims_path) as store:
        store.delete_page(page)
        store.insert_many([Claim(page=page, ordinal=1, kind="fact", fact_id=1, display_value="11.00", unit="亿元")])
    issues = rules.rule9_claims(root, **kwargs)
    assert any("显示值与 facts 不符" in i.message for i in issues)


def test_rule9_flags_number_without_claim(tmp_path):
    root = _vault(tmp_path)
    data = _data_root(tmp_path)
    facts = _facts_db(tmp_path)
    claims_path = tmp_path / "wiki.duckdb"
    vault.write_page(root, "summary", "a", _meta(), "营业收入 10.00 亿元。")
    ClaimStore(claims_path).close()
    issues = rules.rule9_claims(
        root,
        claims_db=claims_path,
        facts_db=facts,
        data_root=data,
        queries_path=tmp_path / "queries.yaml",
    )
    assert any("没有 claim 覆盖" in i.message for i in issues)


def test_rule9_quote_missing_from_page(tmp_path):
    root = _vault(tmp_path)
    data = _data_root(tmp_path)
    facts = _facts_db(tmp_path)
    claims_path = tmp_path / "wiki.duckdb"
    page = "summaries/a.md"
    vault.write_page(root, "summary", "a", _meta(), "销量 3,400 万辆[^c1]。\n\n[^c1]: 原文：doc#P2。\n")
    with ClaimStore(claims_path) as store:
        store.insert_many(
            [Claim(page=page, ordinal=1, kind="quote", anchor="doc#P2", display_value="3,400")]
        )
    issues = rules.rule9_claims(
        root,
        claims_db=claims_path,
        facts_db=facts,
        data_root=data,
        queries_path=tmp_path / "queries.yaml",
    )
    assert any("引文数字" in i.message for i in issues)


def test_rule9_stock_code_is_not_a_number() -> None:
    """「（000625）」是股票代码，不应被当成正文数字要求 claim。"""
    assert rules._sentence_numbers("长安汽车（000625）2026 年半年度报告") == []


def test_rule10_flags_paragraph_without_anchor(tmp_path):
    root = _vault(tmp_path)
    vault.write_page(root, "exploration", "a", _meta(type="exploration"), "这是一段没有页锚的定性陈述。\n")
    issues = rules.rule10_narrative_anchors(root)
    assert any(i.rule == "R10" and "定性段落缺少页锚" in i.message for i in issues)


def test_rule10_accepts_anchor_table_and_navigation(tmp_path):
    root = _vault(tmp_path)
    body = (
        "公司主营业务保持稳定[Ref: doc#P2]。\n\n"
        "| 指标 | 数值 |\n|---|---|\n| 营业收入 | 10 亿元 |\n\n"
        "## 延伸阅读\n- [[explorations/b|另一个页面]]\n"
    )
    vault.write_page(root, "exploration", "a", _meta(type="exploration"), body)
    assert rules.rule10_narrative_anchors(root) == []


def test_rule10_skips_page_meta_paragraph(tmp_path):
    """「本页……」是页面说明，不算缺页锚的定性段落。"""
    root = _vault(tmp_path)
    meta = _meta(type="concept", page_type="concept")
    vault.write_page(root, "concept", "demo", meta, "本页比较三家公司 2025 年营业收入规模，按金额从高到低组织。")
    issues = rules.rule10_narrative_anchors(root)
    assert not [i for i in issues if "本页比较" in i.message]


def test_rule10_skips_entity_pages(tmp_path):
    """实体页是关系索引，不参与 R10 定性页锚检查。"""
    root = _vault(tmp_path)
    meta = _meta(type="entity", page_type="entity")
    vault.write_page(root, "entity", "demo", meta, "本公司在已解析年报里没有可提取的关系表。")
    assert not [i for i in rules.rule10_narrative_anchors(root) if i.page == "entities/demo.md"]


def test_rule10_skips_table_lead_in(tmp_path):
    """「……如下：」是表格引导语，不算定性段落。"""
    root = _vault(tmp_path)
    vault.write_page(root, "exploration", "demo", _meta(page_type="exploration"),
                     "公司核心财务指标如下：\n\n| 指标 | 数值 |")
    assert not [i for i in rules.rule10_narrative_anchors(root) if "如下" in i.message]
