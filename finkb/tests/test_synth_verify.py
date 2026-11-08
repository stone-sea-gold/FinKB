"""合成校验（V1–V7）与失败矩阵单测（P3）。

全部离线；V3 的页数据用 monkeypatch 伪造，不读开发语料。
"""

from __future__ import annotations

from finkb.qa import verifier
from finkb.qa.evidence import build_pack
from finkb.qa.synth import FakeBackend, SynthesisResult
from finkb.qa.synth_verify import compose_verified, verify_synthesis


def _metric_pack():
    return build_pack(
        "万科A 2025 年营业收入是多少？",
        {
            "tier": "metric",
            "route": "fact",
            "ledger": [
                {
                    "id": "c1",
                    "value": 2334.33,
                    "unit": "亿元",
                    "fact_id": 1,
                    "doc": "000002_2025_年报",
                    "page": 11,
                    "metric_id": "revenue",
                    "company": "000002",
                    "period": "2025",
                }
            ],
            "citations": ["[Ref: 000002_2025_年报#P11]"],
        },
    )


def _document_pack():
    return build_pack(
        "比亚迪 2025 年报里海外布局如何？",
        {
            "tier": "document",
            "route": "text",
            "data": {
                "evidence": [
                    {
                        "chunk_id": "002594_2025_年报:p18:2",
                        "doc_id": "002594_2025_年报",
                        "page_num": 18,
                        "section": "海外布局",
                        "snippet": "公司持续推进海外市场布局。",
                    },
                    {
                        "chunk_id": "002594_2025_年报:p220:4",
                        "doc_id": "002594_2025_年报",
                        "page_num": 220,
                        "section": "现金及现金等价物",
                        "snippet": "| 项目 | 2025年 |\n| 现金 | 100 |",
                    },
                ]
            },
            "citations": [
                "[Ref: 002594_2025_年报#P18]",
                "[Ref: 002594_2025_年报#P220]",
            ],
        },
    )


def _metric_result(answer: str) -> SynthesisResult:
    return SynthesisResult(answer=answer, used_ids=["c1"])


def test_valid_metric_passes():
    result = _metric_result("万科A 2025 年营业收入为 {{c1}} 亿元。[Ref: 000002_2025_年报#P11]")
    verification = verify_synthesis(result, _metric_pack(), check_pages=False)
    assert verification.ok, verification.errors
    assert all(verification.checks.values())


def test_v1_unknown_placeholder():
    result = _metric_result("营业收入为 {{c9}} 亿元。[Ref: 000002_2025_年报#P11]")
    verification = verify_synthesis(result, _metric_pack(), check_pages=False)
    assert not verification.ok
    assert verification.checks["V1"] is False


def test_v2_unbound_number():
    result = _metric_result("营业收入为 {{c1}} 亿元，增长 99%。[Ref: 000002_2025_年报#P11]")
    verification = verify_synthesis(result, _metric_pack(), check_pages=False)
    assert not verification.ok
    assert verification.checks["V2"] is False


def test_v3_page_out_of_range(monkeypatch):
    monkeypatch.setattr(verifier, "_report_pages", lambda: {"000002_2025_年报": 100})
    monkeypatch.setattr(
        verifier,
        "_page_texts",
        lambda: {("000002_2025_年报", 11): "营业收入", ("000002_2025_年报", 999): "x"},
    )
    bad = _metric_result("营业收入为 {{c1}} 亿元。[Ref: 000002_2025_年报#P999]")
    verification = verify_synthesis(bad, _metric_pack())
    assert not verification.ok
    assert verification.checks["V3"] is False


def test_v4_document_sentence_without_citation():
    result = SynthesisResult(
        answer="公司持续推进海外市场布局并取得了明显进展。海外业务覆盖多个国家和地区。[Ref: 002594_2025_年报#P18]",
        used_ids=[],
    )
    verification = verify_synthesis(result, _document_pack(), check_pages=False)
    assert not verification.ok
    assert verification.checks["V4"] is False


def test_v5_citation_outside_whitelist():
    result = _metric_result("营业收入为 {{c1}} 亿元。[Ref: 999999_2025_年报#P1]")
    verification = verify_synthesis(result, _metric_pack(), check_pages=False)
    assert not verification.ok
    assert verification.checks["V5"] is False


def test_v6_refusal_without_missing():
    result = SynthesisResult(answer="", refused=True, missing=[])
    verification = verify_synthesis(result, _metric_pack(), check_pages=False)
    assert not verification.ok
    assert verification.checks["V6"] is False
    ok = SynthesisResult(answer="", refused=True, missing=["知识库没有 2027 年数据"])
    assert verify_synthesis(ok, _metric_pack(), check_pages=False).ok


def test_v7_warns_when_table_not_cited():
    result = SynthesisResult(
        answer="公司持续推进海外市场布局。[Ref: 002594_2025_年报#P18]",
        used_ids=[],
    )
    verification = verify_synthesis(result, _document_pack(), check_pages=False)
    assert verification.checks["V7"] is True
    assert any("表格" in warning for warning in verification.warnings)


def test_compose_verified_retries_after_verify_failure():
    pack = _metric_pack()
    backend = FakeBackend(
        [
            {"answer": "营业收入为 {{c1}} 亿元，增长 99%。[Ref: 000002_2025_年报#P11]", "used_ids": ["c1"]},
            {"answer": "营业收入为 {{c1}} 亿元。[Ref: 000002_2025_年报#P11]", "used_ids": ["c1"]},
        ]
    )
    result, verification = compose_verified("万科A 2025 年营业收入是多少？", pack, backend, check_pages=False)
    assert result is not None
    assert verification.ok and verification.attempts == 2
    assert len(backend.calls) == 2


def test_compose_verified_falls_back_after_two_failures():
    pack = _metric_pack()
    backend = FakeBackend(
        [
            {"answer": "营业收入为 {{c1}} 亿元，增长 99%。[Ref: 000002_2025_年报#P11]", "used_ids": ["c1"]},
            {"answer": "营业收入为 {{c1}} 亿元，增长 88%。[Ref: 000002_2025_年报#P11]", "used_ids": ["c1"]},
        ]
    )
    result, verification = compose_verified("万科A 2025 年营业收入是多少？", pack, backend, check_pages=False)
    assert result is None
    assert not verification.ok
    assert verification.attempts == 2


def test_compose_verified_without_backend():
    result, verification = compose_verified("问题", _metric_pack(), None)
    assert result is None
    assert not verification.ok


def _paragraph_pack():
    return build_pack(
        "中国神华 2025 年报里，公司如何描述煤炭产销情况？",
        {
            "tier": "document",
            "route": "text",
            "data": {
                "evidence": [
                    {
                        "chunk_id": "601088_2025_年报:p17:1",
                        "doc_id": "601088_2025_年报",
                        "page_num": 17,
                        "section": "煤炭业务",
                        "snippet": "本集团生产及销售的煤炭产品主要为动力煤，本集团拥有独立运营的铁路集疏运通道。2025 年商品煤产量保持稳定。",
                    }
                ]
            },
            "citations": ["[Ref: 601088_2025_年报#P17]"],
        },
    )


def test_v4_allows_paragraph_citation_inheritance_when_text_matches():
    result = SynthesisResult(
        answer="本集团生产及销售的煤炭产品主要为动力煤。2025 年商品煤产量保持稳定。[Ref: 601088_2025_年报#P17]",
        used_ids=[],
    )
    verification = verify_synthesis(result, _paragraph_pack(), check_pages=False)
    assert verification.ok, verification.errors
    assert verification.checks["V4"] is True
    assert any("段落引用继承" in warning for warning in verification.warnings)


def test_v4_rejects_inherited_sentence_not_in_source():
    result = SynthesisResult(
        answer="公司未来将大幅扩大海外投资并进入更多新的国家市场。本集团生产及销售的煤炭产品主要为动力煤。[Ref: 601088_2025_年报#P17]",
        used_ids=[],
    )
    verification = verify_synthesis(result, _paragraph_pack(), check_pages=False)
    assert not verification.ok
    assert verification.checks["V4"] is False
