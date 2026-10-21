"""分节切分判据的只读审核工具（不修改生产代码、不写 parsadata 产物）。

目的：在正式实施 P1（物理表 → 逻辑表切分）之前，把判据套用到真实年报的
所有含「全宽合并行」的表上，生成可人工审核的报告。

设计约束
--------
- **只读**：不写 data/parsed，不调用 MinerU，不消耗额度。
- **与生产逻辑一致**：缝合结果直接取自 ``stitch_tables``，保证被审核的对象
  就是 L1 将来真正要消费的那张表。
- **span 还原**：生产代码的 ``colspan`` 展开会抹掉「全宽合并」这一事实，
  因此本工具按缝合结果（页码区间 + 表数量）回查原始 HTML，重建每行的 colspan。

用法::

    cd finkb
    ../.venv/bin/python tools/section_split_review.py \
        --content-list data/parsed/002594_2025_年报/content_list.json \
        --doc-id 002594_2025_年报 \
        --out data/reports/section-split-review.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from finkb.parser.mineru_client import content_list_to_pages  # noqa: E402
from finkb.parser.noise_filter import strip_running_noise  # noqa: E402
from finkb.parser.table_stitcher import stitch_tables  # noqa: E402

_TAG_RE = re.compile(r"<td([^>]*)>(.*?)</td>", re.S)
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)

#: 判据参数（可在报告中调整后复跑）
FOOTNOTE_MIN_LEN = 40
FOOTNOTE_PREFIX = "注"
HEADER_SIMILARITY = 0.5


@dataclass
class RawRow:
    """带 colspan 信息的原始行。"""

    spans: list[int]
    texts: list[str]

    @property
    def width(self) -> int:
        return sum(self.spans)

    @property
    def non_empty(self) -> list[str]:
        return [t for t in self.texts if t.strip()]

    def is_full_width_cell(self, total_width: int) -> bool:
        return len(self.spans) == 1 and self.spans[0] == total_width and bool(self.texts[0].strip())


def parse_html_rows(body: str) -> list[RawRow]:
    """把 ``table_body`` 解析成带 span 的行列表。"""
    rows: list[RawRow] = []
    for raw in _ROW_RE.findall(body):
        spans: list[int] = []
        texts: list[str] = []
        for attr, txt in _TAG_RE.findall(raw):
            match = re.search(r'colspan="?(\d+)', attr)
            spans.append(int(match.group(1)) if match else 1)
            texts.append(" ".join(txt.split()))
        if texts:
            rows.append(RawRow(spans=spans, texts=texts))
    return rows


def header_similarity(candidate: RawRow, reference: RawRow) -> float:
    """两行作为表头的相似度（非空单元格文本的 Jaccard）。"""
    a = {t for t in candidate.non_empty}
    b = {t for t in reference.non_empty}
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class FullWidthHit:
    row: int
    text: str
    kind: str          # data_section / summary_block / group_label / footnote
    evidence: str


def classify_full_width_rows(rows: list[RawRow]) -> list[FullWidthHit]:
    """对每个全宽合并行给出语义分类。"""
    width = max((r.width for r in rows), default=0)
    if width == 0:
        return []

    # 参考表头：第一个非全宽、且非空单元格数 >= 3 的行
    reference: RawRow | None = None
    for row in rows:
        if not row.is_full_width_cell(width) and len(row.non_empty) >= 3:
            reference = row
            break

    hits: list[FullWidthHit] = []
    for index, row in enumerate(rows):
        if not row.is_full_width_cell(width):
            continue
        text = row.texts[0].strip()

        if text.startswith(FOOTNOTE_PREFIX) or len(text) > FOOTNOTE_MIN_LEN:
            hits.append(FullWidthHit(index, text, "footnote", f"长度{len(text)}/前缀"))
            continue

        nxt = rows[index + 1] if index + 1 < len(rows) else None
        if nxt is None:
            hits.append(FullWidthHit(index, text, "group_label", "末行，无后继"))
            continue

        if reference is not None:
            sim = header_similarity(nxt, reference)
            if sim >= HEADER_SIMILARITY:
                hits.append(
                    FullWidthHit(index, text, "data_section", f"后继行与表头相似度 {sim:.0%}")
                )
                continue

        if len(nxt.non_empty) <= 4:
            hits.append(
                FullWidthHit(index, text, "summary_block", f"后继为键值对（{len(nxt.non_empty)} 个非空格）")
            )
            continue

        hits.append(
            FullWidthHit(index, text, "group_label", f"后继 {len(nxt.non_empty)} 个非空格，非表头")
        )
    return hits


def splice_rows(rows_a: list[RawRow], rows_b: list[RawRow], overlap: bool) -> list[RawRow]:
    """按顺序拼接两段行（``overlap`` 目前恒为 False，保留扩展位）。"""
    return rows_a + rows_b


def collect_rows_for_stitched_table(
    stitched, page_tables: dict[int, list[list[RawRow]]]
) -> list[RawRow]:
    """按缝合结果回查原始 HTML，重建整张物理表的行（含 span）。"""
    start, end = stitched.page_start, stitched.page_end
    rows: list[RawRow] = []
    for page_no in range(start, end + 1):
        candidates = page_tables.get(page_no, [])
        if not candidates:
            continue
        if page_no == start:
            rows += candidates[-1]      # 跨页表从本页的最后一张表开始
        else:
            rows += candidates[0]       # 续接段总是次页的第一张表
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--content-list", required=True)
    parser.add_argument("--doc-id", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-detail", type=int, default=40)
    args = parser.parse_args()

    content_list = json.loads(Path(args.content_list).read_text(encoding="utf-8"))
    pages = content_list_to_pages(content_list, args.doc_id)
    strip_running_noise(pages)
    result = stitch_tables(pages)

    # 建立 page -> [该页每张表的行(带span)]，供回查
    page_tables: dict[int, list[list[RawRow]]] = {}
    for item in content_list:
        if item.get("type") != "table":
            continue
        body = item.get("table_body")
        if not isinstance(body, str) or not body.strip():
            continue
        page_no = int(item.get("page_idx", 0)) + 1
        page_tables.setdefault(page_no, []).append(parse_html_rows(body))

    lines: list[str] = []
    add = lines.append

    analyzed = 0
    with_hits = 0
    kind_counter: dict[str, int] = {}
    details: list[tuple] = []

    for table in result.tables:
        rows = collect_rows_for_stitched_table(table, page_tables)
        if not rows:
            continue
        analyzed += 1
        hits = classify_full_width_rows(rows)
        if not hits:
            continue
        with_hits += 1
        for hit in hits:
            kind_counter[hit.kind] = kind_counter.get(hit.kind, 0) + 1
        details.append((table, rows, hits))

    add("# 分节切分判据（P1）审核报告")
    add("")
    add(f"- 文档：`{args.doc_id}`")
    add(f"- 被审核的物理表（缝合后）：**{analyzed}** 张")
    add(f"- 其中含全宽合并行：**{with_hits}** 张")
    add(f"- 全宽行分类计数：{kind_counter}")
    add("")
    add("## 分类定义")
    add("")
    add("| 分类 | 含义 | 是否切分 | 判据 |")
    add("|---|---|---|---|")
    add("| `data_section` | 新数据表标题 | ✅ 切分 | 后继行与表头相似度 ≥ 50% |")
    add("| `summary_block` | 汇总块标题 | ✅ 切分（kind=summary） | 后继为键值对（≤4 非空格） |")
    add("| `group_label` | 分组标签 | ❌ 不切分 | 后继是数据行 |")
    add("| `footnote` | 脚注/注释 | ❌ 不切分 | 以「注」开头或长度 > 40 |")
    add("")

    # 概览表
    add("## 概览")
    add("")
    add("| # | 物理表 | 页 | 列 | 行 | 全宽行 | 切分后逻辑表 |")
    add("|---|---|---|---|---|---|---|")
    for idx, (table, rows, hits) in enumerate(details, start=1):
        sections = sum(1 for h in hits if h.kind in {"data_section", "summary_block"})
        # 若首行就是全宽标题，它本身构成第一节
        if hits and hits[0].row == 0 and hits[0].kind in {"data_section", "summary_block"}:
            sections = max(sections, 1)
        else:
            sections += 0 if sections else 0
        n_logical = max(1, sections + (0 if hits and hits[0].row == 0 else 1))
        add(
            f"| {idx} | `{table.parent_id}` | P{table.page_start}-{table.page_end} "
            f"| {table.ncols} | {len(rows)} | {len(hits)} | {n_logical} |"
        )
    add("")

    # 逐表详情
    add("## 逐表详情")
    add("")
    for idx, (table, rows, hits) in enumerate(details[: args.max_detail], start=1):
        add(f"### {idx}. `{table.parent_id}`   P{table.page_start}-P{table.page_end}"
            f"   {table.ncols} 列 / {len(rows)} 行")
        add("")
        add("| 行 | 全宽行文本 | 分类 | 依据 |")
        add("|---|---|---|---|")
        for hit in hits:
            text = hit.text if len(hit.text) <= 60 else hit.text[:57] + "…"
            mark = {"data_section": "✅ 切分", "summary_block": "✅ 切分(汇总)",
                    "group_label": "❌ 保留", "footnote": "❌ 保留(脚注)"}[hit.kind]
            add(f"| r{hit.row:02d} | {text} | {mark} | {hit.evidence} |")
        add("")

        # 展示切分后的骨架
        add("<details><summary>切分骨架</summary>")
        add("")
        add("```")
        width = max((r.width for r in rows), default=0)
        breaks = {h.row: h for h in hits if h.kind in {"data_section", "summary_block"}}
        section_start = 0
        section_title = None
        if hits and hits[0].row == 0 and hits[0].kind in {"data_section", "summary_block"}:
            section_title = hits[0].text
            section_start = 1
        seg = 1
        for i, row in enumerate(rows):
            if i in breaks and i != 0:
                add(f"--- 切分 ---")
                section_title = breaks[i].text
                section_start = i + 1
                seg += 1
            head = f"[S{seg:02d}]" if i == section_start else "      "
            preview = " | ".join(row.non_empty[:4])[:70]
            fw = " ◀全宽" if row.is_full_width_cell(width) else ""
            add(f"{head} r{i:02d} ({row.width:>2}列) {preview}{fw}")
        add("```")
        add("")
        add("</details>")
        add("")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"被审核物理表: {analyzed}")
    print(f"含全宽行: {with_hits}")
    print(f"分类计数: {kind_counter}")
    print(f"报告已写入: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
