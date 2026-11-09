"""视觉解读试点（parser/vision.py）的离线测试：不调用真实模型。"""

from __future__ import annotations

import json

import pytest

from finkb.parser import vision


def _item(page_idx: int, item_type: str, text: str = "") -> dict:
    return {"page_idx": page_idx, "type": item_type, "text": text}


def test_candidate_pages_classifies_image_and_chart() -> None:
    content_list = [
        _item(0, "text", "封面文字"),
        _item(1, "image"),
        _item(1, "header", "页眉"),
        _item(2, "text", "正文"),
        _item(3, "chart"),
        _item(3, "text", "图表标签"),
        _item(4, "image"),
        _item(4, "image"),
    ]
    assert vision.candidate_pages(content_list) == {2: "image", 4: "chart", 5: "image"}


def test_candidate_pages_ignores_noise_only_pages() -> None:
    content_list = [_item(0, "header", "页眉"), _item(0, "page_number", "1")]
    assert vision.candidate_pages(content_list) == {}


def test_is_edge_page() -> None:
    assert vision.is_edge_page(1, 370)
    assert vision.is_edge_page(370, 370)
    assert not vision.is_edge_page(2, 370)
    assert not vision.is_edge_page(369, 370)


def test_normalize_result_clamps_and_filters() -> None:
    raw = {
        "kind": "CHART",
        "description": "  主要经营数据  ",
        "confidence": 1.7,
        "tables": [
            {"caption": "表1", "columns": ["项目", "2025"], "rows": [["营业收入", "123"]]},
            {"caption": "空表", "columns": [], "rows": []},
            "bad",
        ],
    }
    out = vision.normalize_result(raw)
    assert out["kind"] == "chart"
    assert out["description"] == "主要经营数据"
    assert out["confidence"] == 1.0
    assert len(out["tables"]) == 1
    assert out["tables"][0]["rows"] == [["营业收入", "123"]]


def test_normalize_result_unknown_kind() -> None:
    out = vision.normalize_result({"kind": "poster", "confidence": -3})
    assert out["kind"] == "other"
    assert out["confidence"] == 0.0


def _make_pdf(path, pages: int = 3) -> None:
    import pymupdf

    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"page {i + 1}")
    doc.save(str(path))
    doc.close()


class FakeBackend:
    def __init__(self, results: dict[int, dict] | None = None) -> None:
        self.results = results or {}
        self.calls: list[int] = []

    def classify(self, png: bytes, prompt: str) -> dict:
        assert png[:2] == b"\xff\xd8"  # JPEG
        page = len(self.calls) + 1
        self.calls.append(page)
        return self.results.get(page, {"kind": "decorative", "confidence": 0.9})


def test_vision_for_document_dry_run_skips_edge_cover(tmp_path) -> None:
    parsed = tmp_path / "doc_2025_年报"
    parsed.mkdir()
    content_list = [_item(0, "image"), _item(1, "image"), _item(2, "chart")]
    (parsed / "content_list.json").write_text(json.dumps(content_list), encoding="utf-8")
    _make_pdf(tmp_path / "doc.pdf", 3)

    summary = vision.vision_for_document(parsed, pdf_path=tmp_path / "doc.pdf", dry_run=True)
    assert summary["candidates"] == 3
    pages = {p["page"]: p for p in summary["pages"]}
    assert pages[1]["kind"] == "cover" and pages[1]["skipped"] is True
    assert pages[2]["kind"] == "unprocessed"
    assert pages[3]["kind"] == "unprocessed"


def test_vision_for_document_calls_backend_and_writes_json(tmp_path) -> None:
    parsed = tmp_path / "doc_2025_年报"
    parsed.mkdir()
    content_list = [_item(0, "image"), _item(1, "image"), _item(2, "chart")]
    (parsed / "content_list.json").write_text(json.dumps(content_list), encoding="utf-8")
    _make_pdf(tmp_path / "doc.pdf", 3)

    backend = FakeBackend({1: {"kind": "infographic", "description": "发展历程", "confidence": 0.8}})
    summary = vision.vision_for_document(parsed, pdf_path=tmp_path / "doc.pdf", backend=backend)

    # 第 1 页是首图，按封面跳过；只处理第 2、3 页
    assert len(backend.calls) == 2
    pages = {p["page"]: p for p in summary["pages"]}
    assert pages[1]["skipped"] is True
    assert pages[2]["kind"] == "infographic"
    assert pages[3]["kind"] == "decorative"
    saved = json.loads((parsed / "vision_pages.json").read_text(encoding="utf-8"))
    assert saved["candidates"] == 3
    assert {p["page"] for p in saved["pages"]} == {1, 2, 3}


def test_vision_for_document_records_backend_error(tmp_path) -> None:
    parsed = tmp_path / "doc_2025_年报"
    parsed.mkdir()
    content_list = [_item(0, "image"), _item(1, "chart")]
    (parsed / "content_list.json").write_text(json.dumps(content_list), encoding="utf-8")
    _make_pdf(tmp_path / "doc.pdf", 2)

    class Boom:
        def classify(self, png: bytes, prompt: str) -> dict:
            raise RuntimeError("模型超时")

    summary = vision.vision_for_document(parsed, pdf_path=tmp_path / "doc.pdf", backend=Boom())
    pages = {p["page"]: p for p in summary["pages"]}
    assert pages[1]["skipped"] is True  # 末页纯图 → 封底
    assert pages[2]["kind"] == "error"
    assert "模型超时" in pages[2]["error"]


def test_vision_for_document_missing_content_list(tmp_path) -> None:
    parsed = tmp_path / "doc_2025_年报"
    parsed.mkdir()
    summary = vision.vision_for_document(parsed, dry_run=True)
    assert "error" in summary


def test_parse_json_response_recovers_wrapped_json() -> None:
    assert vision.parse_json_response('```json\n{"kind": "chart"}\n```') == {"kind": "chart"}
    assert vision.parse_json_response('说明文字 {"kind": "blank"} 尾巴') == {"kind": "blank"}
    with pytest.raises(ValueError):
        vision.parse_json_response("完全不是 JSON")


class FlakyBackend:
    def __init__(self) -> None:
        self.calls = 0

    def classify(self, png: bytes, prompt: str) -> dict:
        self.calls += 1
        if self.calls == 1:
            raise ValueError("模型未返回合法 JSON")
        return {"kind": "chart", "confidence": 0.7}


def test_vision_for_document_retries_once(tmp_path) -> None:
    parsed = tmp_path / "doc_2025_年报"
    parsed.mkdir()
    (parsed / "content_list.json").write_text(json.dumps([_item(0, "chart")]), encoding="utf-8")
    _make_pdf(tmp_path / "doc.pdf", 1)

    backend = FlakyBackend()
    summary = vision.vision_for_document(parsed, pdf_path=tmp_path / "doc.pdf", backend=backend)
    assert backend.calls == 2
    assert summary["pages"][0]["kind"] == "chart"


def test_vision_for_document_retry_errors_reuses_previous(tmp_path) -> None:
    parsed = tmp_path / "doc_2025_年报"
    parsed.mkdir()
    (parsed / "content_list.json").write_text(
        json.dumps([_item(0, "image"), _item(1, "chart")]), encoding="utf-8"
    )
    _make_pdf(tmp_path / "doc.pdf", 2)
    (parsed / "vision_pages.json").write_text(
        json.dumps(
            {
                "pages": [
                    {"page": 1, "kind": "cover", "skipped": True, "reason": "edge_page_image"},
                    {"page": 2, "kind": "error", "error": "JSONDecodeError: bad"},
                ]
            }
        ),
        encoding="utf-8",
    )

    backend = FakeBackend()
    summary = vision.vision_for_document(
        parsed, pdf_path=tmp_path / "doc.pdf", backend=backend, retry_errors=True
    )
    assert len(backend.calls) == 1  # 只重跑第 2 页
    assert summary["pages"][1]["kind"] == "decorative"
