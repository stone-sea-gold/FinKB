"""纯图片页 / 图表页的视觉解读（试点能力，默认不接入解析主流程）。

解决的问题：MinerU 的 content_list 里，纯图片页会被整页跳过（不进
page_anchored.md），图表页（chart 块）只有图片、没有结构化文字；质量体检只能
把它们记成页码，问答时既用不上、也无法判断“这页有没有值得看的信息”。

本模块把这些页面渲染成图片，交给视觉模型做一次“分类 + 描述（必要时抽表）”，
结果单独落盘到 ``<parsed_dir>/vision_pages.json``：

* 不写入 page_anchored.md，不进入事实层（避免模型看错数字污染 facts）；
* 首/末物理页的纯图片页按“封面/封底”跳过，不调用模型（用户定的省成本规则）；
* 其余纯图片页与 chart 图表页逐页调用，结果带 ``source=vision`` 与置信度。

用法（CLI）::

    finkb vision --doc 601668_2025_年报 --dry-run     # 只看候选页
    finkb vision --all --pdf-root ../raw              # 批量跑真实模型
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from finkb.parser.mineru_client import pdf_page_count

KINDS = ("cover", "back_cover", "blank", "decorative", "chart", "infographic", "other")
NOISE_TYPES = {"header", "footer", "page_number", "page_footnote"}

SYSTEM_PROMPT = "你是财务报告页面识别助手。只输出 JSON，不要解释。"

USER_PROMPT = """判断这一页图片的内容类型，并提取图中可见的信息。
类型只能是：cover（封面）、back_cover（封底）、blank（空白页）、decorative（装饰图/照片/logo）、chart（图表）、infographic（信息图/时间轴/组织架构）、other。
若能读到文字或图表数据，写进 description 和 tables；数字必须能从图中确定读到，读不准就留空，不要猜。
输出 JSON：{"kind": "chart", "description": "一句话说明这页是什么", "confidence": 0.9, "tables": [{"caption": "表名", "columns": ["项目", "2025"], "rows": [["营业收入", "123"]]}]}"""


class VisionBackend(Protocol):
    def classify(self, png: bytes, prompt: str) -> dict[str, Any]: ...


def _item_page(item: dict[str, Any]) -> int:
    return int(item.get("page_idx", 0)) + 1


def candidate_pages(content_list: list[dict[str, Any]]) -> dict[int, str]:
    """返回 ``{物理页: "image" | "chart"}``。

    * image：该页除页眉/页脚/页码外只有 image 块（整页纯图片）；
    * chart：该页至少有一个 chart 块（图表页，文字可能只有零散标签）。
    """
    kinds: dict[int, set[str]] = {}
    for item in content_list:
        item_type = str(item.get("type", "text"))
        if item_type in NOISE_TYPES:
            continue
        kinds.setdefault(_item_page(item), set()).add(item_type)
    out: dict[int, str] = {}
    for page, types in kinds.items():
        if "chart" in types:
            out[page] = "chart"
        elif types and types <= {"image"}:
            out[page] = "image"
    return dict(sorted(out.items()))


def is_edge_page(page: int, total_pages: int) -> bool:
    """首/末物理页：纯图片时按封面/封底处理，不调用模型。"""
    return total_pages > 0 and page in (1, total_pages)


def render_page_png(pdf_path: str | Path, page: int, *, dpi: int = 110, quality: int = 80) -> bytes:
    """把指定物理页渲染成 JPEG 字节（控制分辨率与体积，减少视觉 token）。"""
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    try:
        if page < 1 or page > len(doc):
            raise IndexError(f"页码越界：{page} / {len(doc)}")
        pix = doc[page - 1].get_pixmap(dpi=dpi)
        return pix.tobytes("jpeg", jpg_quality=quality)
    finally:
        doc.close()


def _strip_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def parse_json_response(text: str) -> dict[str, Any]:
    """模型偶尔返回截断/带前后缀的 JSON；尽量救回来，救不回抛 ValueError。"""
    s = _strip_fence(text)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(s[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise ValueError(f"模型未返回合法 JSON（前 200 字：{s[:200]!r}）") from None


def normalize_result(raw: dict[str, Any]) -> dict[str, Any]:
    """把模型输出收敛成固定字段；非法类型归 other，置信度夹到 0–1。"""
    kind = str(raw.get("kind") or "other").strip().lower()
    if kind not in KINDS:
        kind = "other"
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    description = str(raw.get("description") or "").strip()[:200]
    tables = raw.get("tables")
    if not isinstance(tables, list):
        tables = []
    clean_tables: list[dict[str, Any]] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        rows = table.get("rows")
        if not isinstance(rows, list) or not rows:
            continue
        clean_tables.append(
            {
                "caption": str(table.get("caption") or "")[:120],
                "columns": [str(c)[:40] for c in table.get("columns") or []],
                "rows": [[str(c)[:80] for c in row] for row in rows if isinstance(row, list)][:40],
            }
        )
    return {"kind": kind, "description": description, "confidence": confidence, "tables": clean_tables}


@dataclass
class VisionPage:
    page: int
    kind: str
    description: str = ""
    confidence: float = 0.0
    tables: list[dict[str, Any]] = field(default_factory=list)
    skipped: bool = False
    reason: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "page": self.page,
            "kind": self.kind,
            "description": self.description,
            "confidence": self.confidence,
            "tables": self.tables,
        }
        if self.skipped:
            out["skipped"] = True
            out["reason"] = self.reason
        if self.error:
            out["error"] = self.error
        return out


def _post_vision(config: Any, png: bytes, prompt: str, *, timeout: float = 120.0) -> dict[str, Any]:
    """调用 OpenAI 兼容的视觉接口；``config`` 需有 api_key/base_url/model。"""
    import httpx

    data_url = "data:image/jpeg;base64," + base64.b64encode(png).decode("ascii")
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": 4000,
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "User-Agent": "finkb/0.1 (page vision)",
    }
    session_id = getattr(config, "session_id", "")
    if session_id:
        headers["x-opencode-session"] = session_id
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(f"{config.base_url.rstrip('/')}/v1/chat/completions", headers=headers, json=payload)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        if isinstance(content, list):  # 少数网关返回分段 content
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        return parse_json_response(str(content))


class HttpVisionBackend:
    """默认后端：复用项目 ``LLMConfig``（opencode key），按页调用视觉模型。"""

    def __init__(self, config: Any | None = None, *, model: str | None = None) -> None:
        if config is None:
            from finkb.fact_engine.extractor import LLMConfig

            config = LLMConfig.from_env()
        if model:
            from dataclasses import replace

            config = replace(config, model=model)
        self.config = config

    def classify(self, png: bytes, prompt: str) -> dict[str, Any]:
        return _post_vision(self.config, png, prompt)


def _resolve_pdf(doc_id: str, pdf_root: str | Path | None) -> Path | None:
    candidates = []
    if pdf_root is not None:
        root = Path(pdf_root)
        candidates.append(root / f"{doc_id}.pdf")
    candidates.extend(
        [
            Path("data/raw") / f"{doc_id}.pdf",
            Path("../raw") / f"{doc_id}.pdf",
            Path("raw") / f"{doc_id}.pdf",
        ]
    )
    for path in candidates:
        if path.exists():
            return path
    return None


def vision_for_document(
    parsed_dir: str | Path,
    *,
    pdf_path: str | Path | None = None,
    pdf_root: str | Path | None = None,
    backend: VisionBackend | None = None,
    model: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    retry_errors: bool = False,
) -> dict[str, Any]:
    """处理一份文档的图片/图表页，返回汇总；``dry_run`` 只列候选不调模型。"""
    parsed_dir = Path(parsed_dir)
    doc_id = parsed_dir.name
    content_path = parsed_dir / "content_list.json"
    if not content_path.exists():
        return {"doc_id": doc_id, "error": "缺少 content_list.json", "pages": []}
    content_list = json.loads(content_path.read_text(encoding="utf-8"))
    candidates = candidate_pages(content_list)
    pdf = Path(pdf_path) if pdf_path else _resolve_pdf(doc_id, pdf_root)
    total_pages = pdf_page_count(pdf) if pdf and pdf.exists() else 0
    if not total_pages:
        total_pages = max((int(i.get("page_idx", 0)) + 1 for i in content_list), default=0)

    previous: dict[int, dict[str, Any]] = {}
    existing_path = parsed_dir / "vision_pages.json"
    if retry_errors and existing_path.exists():
        try:
            for entry in json.loads(existing_path.read_text(encoding="utf-8")).get("pages", []):
                if entry.get("kind") not in ("error", "unprocessed") and not entry.get("skipped"):
                    previous[int(entry.get("page", 0))] = entry
        except (json.JSONDecodeError, TypeError, ValueError):
            previous = {}

    pages: list[VisionPage] = []
    for page, source_kind in candidates.items():
        if limit is not None and len(pages) >= limit:
            break
        if page in previous:
            old = previous[page]
            pages.append(
                VisionPage(
                    page=page,
                    kind=str(old.get("kind", "other")),
                    description=str(old.get("description") or ""),
                    confidence=float(old.get("confidence") or 0.0),
                    tables=list(old.get("tables") or []),
                )
            )
            continue
        if is_edge_page(page, total_pages) and source_kind == "image":
            pages.append(
                VisionPage(
                    page=page,
                    kind="cover" if page == 1 else "back_cover",
                    skipped=True,
                    reason="edge_page_image",
                )
            )
            continue
        pages.append(VisionPage(page=page, kind="unprocessed", reason=source_kind))

    summary: dict[str, Any] = {
        "doc_id": doc_id,
        "pdf": str(pdf) if pdf else None,
        "total_pages": total_pages,
        "candidates": len(candidates),
        "pages": [p.to_dict() for p in pages],
    }
    if dry_run:
        return summary
    if backend is None:
        backend = HttpVisionBackend(model=model)
    if not pdf or not pdf.exists():
        for item in pages:
            if not item.skipped:
                item.error = "缺少 PDF 原件，无法渲染页面"
        summary["pages"] = [p.to_dict() for p in pages]
        summary["model"] = getattr(getattr(backend, "config", None), "model", model)
        summary["created_at"] = datetime.now().isoformat(timespec="seconds")
        (parsed_dir / "vision_pages.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return summary

    for item in pages:
        if item.skipped:
            continue
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                png = render_page_png(pdf, item.page)
                prompt = USER_PROMPT if attempt == 0 else USER_PROMPT + "\n输出要精简：description 不超过 80 字，tables 最多 40 行、每行最多 8 列。"
                raw = backend.classify(png, prompt)
                normalized = normalize_result(raw)
                item.kind = normalized["kind"]
                item.description = normalized["description"]
                item.confidence = normalized["confidence"]
                item.tables = normalized["tables"]
                item.error = ""
                break
            except Exception as exc:  # noqa: BLE001 - 单页失败不应中断整份文档
                last_exc = exc
        else:
            item.kind = "error"
            item.error = f"{type(last_exc).__name__}: {last_exc}"[:300]
    summary["pages"] = [p.to_dict() for p in pages]
    summary["model"] = getattr(getattr(backend, "config", None), "model", model)
    summary["created_at"] = datetime.now().isoformat(timespec="seconds")
    (parsed_dir / "vision_pages.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary
