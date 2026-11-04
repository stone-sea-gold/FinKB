"""块级切块器（S3）：``page_anchored.md`` → ``chunks.jsonl``。

切块目标不是「切得小」，而是**每一块都能回查原文**：块里带 ``doc_id / page_num /
section``，正文从页文本原样取出；表格切段时重复表头，保证单独看一段也知道列含义。

两类块：

- ``text``：页内连续正文，合并到约 300 字；
- ``table``：Markdown 管道表，超过约 1,200 字按行切段并重复表头。

``page_anchored.md`` 里缺失的 436 页是跨页缝合表的空续页（ADR-022），没有正文，
自然会被跳过；切块消费的是逻辑表内容，不按页覆盖率判断完整性。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import yaml

from finkb.fact_engine.selector import FINKB_ROOT

META_RE = re.compile(r"<!--\s*META:\s*(\{.*?\})\s*-->")
DOC_RE = re.compile(r"^\d{6}_")
TEXT_CHARS = 300
TABLE_CHARS = 1200
MIN_TEXT_CHARS = 20


def _load_scope(path: str | Path | None = None) -> dict[str, dict]:
    file = Path(path or (FINKB_ROOT / "config" / "wiki_scope.yaml"))
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    return {str(doc["doc_id"]): doc for doc in data.get("docs") or []}


def iter_blocks(doc_dir: Path):
    """按 META 块切 ``page_anchored.md``，产出 (page, section, text)。"""
    file = doc_dir / "page_anchored.md"
    if not file.exists():
        return
    page, section, buffer = 0, "", []
    for line in file.read_text(encoding="utf-8").splitlines():
        match = META_RE.match(line.strip())
        if match:
            text = "\n".join(buffer).strip()
            if page and text:
                yield page, section, text
            buffer = []
            try:
                meta = json.loads(match.group(1))
            except json.JSONDecodeError:
                meta = {}
            page = int(meta.get("page") or 0)
            section = str(meta.get("section") or "")
            continue
        buffer.append(line)
    text = "\n".join(buffer).strip()
    if page and text:
        yield page, section, text


def iter_pages(doc_dir: Path):
    """按页聚合块文本，产出 (page, section, text)；供回查校验用。"""
    pages: dict[int, list[str]] = {}
    sections: dict[int, str] = {}
    for page, section, text in iter_blocks(doc_dir):
        pages.setdefault(page, []).append(text)
        sections.setdefault(page, section)
    for page in sorted(pages):
        yield page, sections[page], "\n".join(pages[page])


def _split_segments(text: str) -> list[tuple[str, str]]:
    """把页文本拆成 (kind, segment)：连续管道表行是 table，其余是 text。"""
    segments: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        kind = "table" if line.strip().startswith("|") else "text"
        if segments and segments[-1][0] == kind:
            segments[-1][1].append(line)
        else:
            segments.append((kind, [line]))
    return [(kind, "\n".join(lines).strip()) for kind, lines in segments if "\n".join(lines).strip()]


def _text_chunks(text: str, size: int = TEXT_CHARS) -> list[str]:
    """正文按段落合并到约 size 字；单段超长时按句号硬切。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]
    chunks: list[str] = []
    buffer = ""
    for para in paragraphs:
        if buffer and len(buffer) + len(para) + 1 > size:
            chunks.append(buffer.strip())
            buffer = para
        else:
            buffer = f"{buffer}\n{para}".strip()
    if buffer:
        chunks.append(buffer.strip())
    result: list[str] = []
    for chunk in chunks:
        if len(chunk) <= size * 2:
            result.append(chunk)
            continue
        for i in range(0, len(chunk), size):
            result.append(chunk[i : i + size])
    return [chunk for chunk in result if len(chunk) >= MIN_TEXT_CHARS]


def _table_chunks(text: str, size: int = TABLE_CHARS) -> list[tuple[str, str]]:
    """管道表按行切段，返回 (header, chunk_text)；段段重复表头。"""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = lines[0]
    body = lines[1:]
    if len(text) <= size or len(body) <= 1:
        return [(header, "\n".join(lines))]
    chunks: list[tuple[str, str]] = []
    buffer: list[str] = []
    length = len(header)
    for line in body:
        if buffer and length + len(line) + 1 > size:
            chunks.append((header, "\n".join([header, *buffer])))
            buffer, length = [], len(header)
        buffer.append(line)
        length += len(line) + 1
    if buffer:
        chunks.append((header, "\n".join([header, *buffer])))
    return chunks


def chunk_document(doc_dir: Path, meta: dict) -> list[dict]:
    """切一份文档，返回块列表（带稳定 chunk_id）。"""
    doc_id = doc_dir.name
    company = str(meta.get("company") or doc_id.split("_")[0])
    period = str(meta.get("period") or "")
    chunks: list[dict] = []
    page_blocks: dict[int, list[tuple[str, str]]] = {}
    page_sections: dict[int, str] = {}
    for page, section, text in iter_blocks(doc_dir):
        page_blocks.setdefault(page, []).append((section, text))
        page_sections.setdefault(page, section)
    for page in sorted(page_blocks):
        # 页内先按行拆 text/table 段，再把相邻正文合并到约 300 字。
        merged: list[list] = []
        for section, text in page_blocks[page]:
            for kind, segment in _split_segments(text):
                if kind == "text" and merged and merged[-1][0] == "text":
                    merged[-1][1] = f"{merged[-1][1]}\n{segment}"
                else:
                    merged.append([kind, segment, section])
        seq = 0
        for kind, segment, section in merged:
            pieces = (
                [(None, piece) for piece in _text_chunks(segment)]
                if kind == "text"
                else _table_chunks(segment)
            )
            for header, piece in pieces:
                chunks.append(
                    {
                        "chunk_id": f"{doc_id}:p{page}:{seq}",
                        "doc_id": doc_id,
                        "company": company,
                        "period": period,
                        "page_num": page,
                        "section": section or page_sections.get(page, ""),
                        "block_type": kind,
                        "text": piece,
                        "table_header": header,
                        "char_len": len(piece),
                    }
                )
                seq += 1
    return chunks


def build(
    parsed_root: str | Path | None = None,
    out_path: str | Path | None = None,
    scope_path: str | Path | None = None,
) -> dict:
    """全量切块并写 ``chunks.jsonl``；返回统计。"""
    root = Path(parsed_root or (FINKB_ROOT / "data" / "parsed"))
    out = Path(out_path or (FINKB_ROOT / "data" / "_search" / "chunks.jsonl"))
    scope = _load_scope(scope_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    docs = [p for p in sorted(root.iterdir()) if p.is_dir() and DOC_RE.match(p.name)]
    all_chunks: list[dict] = []
    for doc_dir in docs:
        all_chunks.extend(chunk_document(doc_dir, scope.get(doc_dir.name, {})))
    with out.open("w", encoding="utf-8") as handle:
        for chunk in all_chunks:
            handle.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    text_chunks = sum(1 for c in all_chunks if c["block_type"] == "text")
    return {
        "docs": len(docs),
        "chunks": len(all_chunks),
        "text": text_chunks,
        "table": len(all_chunks) - text_chunks,
        "pages": len({(c["doc_id"], c["page_num"]) for c in all_chunks}),
        "out": str(out),
    }


def verify(parsed_root: str | Path | None = None, chunks_path: str | Path | None = None) -> dict:
    """校验块锚：页存在、正文可在页文本中找到（表格看首末行）。"""
    root = Path(parsed_root or (FINKB_ROOT / "data" / "parsed"))
    path = Path(chunks_path or (FINKB_ROOT / "data" / "_search" / "chunks.jsonl"))
    pages: dict[tuple[str, int], str] = {}
    for doc_dir in sorted(root.iterdir()):
        if not doc_dir.is_dir() or not DOC_RE.match(doc_dir.name):
            continue
        for page, _section, text in iter_pages(doc_dir):
            pages[(doc_dir.name, page)] = re.sub(r"\s+", "", text)
    checked = missing_pages = text_mismatch = table_mismatch = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            chunk = json.loads(line)
            checked += 1
            page_text = pages.get((chunk["doc_id"], chunk["page_num"]))
            if page_text is None:
                missing_pages += 1
                continue
            flat = re.sub(r"\s+", "", chunk["text"])
            if chunk["block_type"] == "text":
                if flat and flat not in page_text:
                    text_mismatch += 1
            else:
                rows = [row for row in flat.split("|") if row][:3]
                if rows and rows[0] not in page_text:
                    table_mismatch += 1
    return {
        "checked": checked,
        "missing_pages": missing_pages,
        "text_mismatch": text_mismatch,
        "table_mismatch": table_mismatch,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="块级切块")
    parser.add_argument("--parsed", default=str(FINKB_ROOT / "data" / "parsed"))
    parser.add_argument("--out", default=None)
    parser.add_argument("--scope", default=None)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(args.parsed, args.out, args.scope), ensure_ascii=False))
    if args.verify:
        print(json.dumps(verify(args.parsed, args.out), ensure_ascii=False))


if __name__ == "__main__":
    main()
