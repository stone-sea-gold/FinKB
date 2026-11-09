"""视觉解读页接入检索的离线测试（不调模型、不读真实语料）。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from finkb.qa import answer, verifier
from finkb.search import vision_index


def _write_doc(root: Path, doc_id: str, pages: list[dict]) -> None:
    doc_dir = root / doc_id
    doc_dir.mkdir(parents=True)
    (doc_dir / "vision_pages.json").write_text(
        json.dumps({"doc_id": doc_id, "pages": pages}, ensure_ascii=False), encoding="utf-8"
    )


def _page(page: int, kind: str, description: str, tables: list[dict] | None = None) -> dict:
    return {
        "page": page,
        "kind": kind,
        "description": description,
        "tables": tables or [],
        "skipped": False,
        "error": "",
    }


def test_load_notes_keeps_only_valuable_kinds(tmp_path: Path) -> None:
    _write_doc(tmp_path, "601668_2025_年报", [
        _page(1, "cover", "封面"),
        _page(4, "decorative", "山水画"),
        _page(26, "chart", "主要经营数据柱状图"),
        _page(27, "infographic", "分部占比"),
    ])
    notes = vision_index.load_notes(tmp_path)
    assert [(n["page"], n["kind"]) for n in notes] == [(26, "chart"), (27, "infographic")]
    assert notes[0]["company"] == "601668"
    assert notes[0]["period"] == "2025"


def test_search_scores_description_higher_and_applies_filters(tmp_path: Path) -> None:
    _write_doc(tmp_path, "601668_2025_年报", [
        _page(26, "chart", "营业收入与净利润趋势", [
            {"caption": "主要经营数据", "columns": ["指标", "2025"], "rows": [["营业收入（亿元）", "20,821"]]},
        ]),
        _page(27, "infographic", "海外业务区域分布"),
    ])
    _write_doc(tmp_path, "601899_2025_年报", [_page(10, "infographic", "海外业务与产量计划")])

    hits = vision_index.search("营业收入", parsed_root=tmp_path)
    assert hits and hits[0]["page"] == 26
    assert hits[0]["score"] > 0

    filtered = vision_index.search("海外业务", filters={"company": "601899"}, parsed_root=tmp_path)
    assert [hit["doc_id"] for hit in filtered] == ["601899_2025_年报"]

    assert vision_index.search("完全不相关的词", parsed_root=tmp_path) == []


def test_to_evidence_shape(tmp_path: Path) -> None:
    note = {
        "doc_id": "601668_2025_年报", "page": 26, "kind": "chart",
        "description": "营业收入趋势", "text": "营业收入（亿元） 20,821",
    }
    item = vision_index.to_evidence(note)
    assert item["chunk_id"] == "vision:601668_2025_年报#P26"
    assert item["block_type"] == "vision" and item["vision"] is True
    assert item["snippet"].startswith("【视觉解读】")


def test_answer_text_appends_vision_evidence(monkeypatch) -> None:
    monkeypatch.setattr("finkb.search.hybrid.search", lambda *a, **k: [])
    monkeypatch.setattr(
        vision_index, "search",
        lambda *a, **k: [{
            "doc_id": "601668_2025_年报", "page": 26, "kind": "chart",
            "description": "营业收入趋势", "text": "营业收入（亿元） 20,821",
        }],
    )
    payload = answer.answer_text("中国建筑 2025 年海外业务布局")
    assert payload["data"]["vision"] is True
    assert payload["data"]["evidence"][0]["vision"] is True
    assert payload["citations"] == ["[Ref: 601668_2025_年报#P26]"]
    assert "视觉模型解读" in payload["text"]


def test_answer_text_keeps_text_evidence_and_adds_vision(monkeypatch) -> None:
    text_item = {
        "chunk_id": "c1", "doc_id": "601668_2025_年报", "page_num": 29,
        "section": "全球化", "text": "公司持续推进海外业务布局。" * 3, "block_type": "text",
    }
    monkeypatch.setattr("finkb.search.hybrid.search", lambda *a, **k: [text_item])
    monkeypatch.setattr(
        vision_index, "search",
        lambda *a, **k: [{
            "doc_id": "601668_2025_年报", "page": 26, "kind": "chart",
            "description": "海外业务区域分布", "text": "海外业务区域分布 亚太 欧洲",
        }],
    )
    payload = answer.answer_text("中国建筑 2025 年海外业务布局")
    kinds = [item.get("vision", False) for item in payload["data"]["evidence"]]
    assert kinds == [False, True]
    assert len(payload["citations"]) == 2


def test_verifier_accepts_vision_page_citation(tmp_path: Path, monkeypatch) -> None:
    doc_id = "601668_2025_年报"
    _write_doc(tmp_path, doc_id, [_page(26, "chart", "主要经营数据")])
    (tmp_path / doc_id / "report.json").write_text(json.dumps({"pages": 370}), encoding="utf-8")
    monkeypatch.setattr(verifier, "PARSED_DIR", tmp_path)
    verifier._page_texts.cache_clear()
    verifier._report_pages.cache_clear()
    try:
        result = verifier.verify({
            "text": "公司营业收入 20,821 亿元。[Ref: 601668_2025_年报#P26]",
            "citations": ["[Ref: 601668_2025_年报#P26]"],
            "ledger": [{"value": 20821.42}],
        })
    finally:
        verifier._page_texts.cache_clear()
        verifier._report_pages.cache_clear()
    assert result["ok"] is True, result["issues"]


def test_parse_parser_vision_defaults_on_and_can_disable() -> None:
    from finkb.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["parse", "x.pdf"]).vision is True
    assert parser.parse_args(["parse", "x.pdf", "--no-vision"]).vision is False
    assert parser.parse_args(["parse", "x.pdf", "--vision"]).vision is True


def test_library_run_parse_adds_vision_flag(monkeypatch) -> None:
    from finkb import library

    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(library.subprocess, "run", fake_run)
    library._run_parse(Path("x.pdf"), "601668_2025_年报", "mineru", [])
    assert "--vision" in captured["command"]

    captured.clear()
    library._run_parse(Path("x.pdf"), "601668_2025_年报", "mineru", [], vision=False)
    assert "--vision" not in captured["command"]
