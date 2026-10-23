"""分节切分结果的只读审核报告生成器。

与早期版本的区别：**直接用生产代码的判定结果**（``section_splitter.split_sections``），
不再自己复刻一套判据。这样报告里的每一处切分，就是 L1 将来真正会拿到的切分。

只读：不调用 MinerU、不写 data/parsed，仅读 content_list.json 并输出 Markdown 报告。

用法::

    cd finkb
    ../.venv/bin/python tools/section_split_review.py \
        --content-list "data/parsed/002594_2025_三季报/content_list.json" \
        --doc-id 002594_2025_三季报 \
        --out "data/reports/section-split-review-三季报.md"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from finkb.common.models import StitchedTable  # noqa: E402
from finkb.parser.mineru_client import content_list_to_pages  # noqa: E402
from finkb.parser.noise_filter import strip_running_noise  # noqa: E402
from finkb.parser.section_splitter import SplitOptions, split_sections  # noqa: E402
from finkb.parser.table_stitcher import stitch_tables  # noqa: E402

KIND_LABEL = {
    "data": "数据表",
    "summary": "汇总块",
    "kv": "键值段",
    "kv_row": "横表键值对",
    "footnote": "脚注",
}


def _preview(row: list[str], limit: int = 60) -> str:
    cells = [c for c in row if c.strip()]
    text = " | ".join(cells[:4])
    return text[:limit] + ("…" if len(text) > limit else "")


def build_report(doc_id: str, tables: list[StitchedTable], options: SplitOptions) -> list[str]:
    lines: list[str] = []
    add = lines.append

    total_logical = 0
    kind_counter: dict[str, int] = {}
    with_cuts = 0
    details: list[tuple[StitchedTable, object]] = []

    for table in tables:
        result = split_sections(table, options)
        total_logical += result.count
        for logical in result.tables:
            kind_counter[logical.kind] = kind_counter.get(logical.kind, 0) + 1
        if result.cuts:
            with_cuts += 1
        details.append((table, result))

    add(f"# 分节切分结果审核报告 —— {doc_id}")
    add("")
    add("> 本报告直接反映**生产代码**的实际判定（`finkb.parser.section_splitter`），")
    add("> 不是另写一套判据复刻。报告中每一处切分都会进入下游 L1。")
    add("")
    add("## 总览")
    add("")
    add("| 指标 | 数值 |")
    add("|---|---|")
    add(f"| 文档 | `{doc_id}` |")
    add(f"| 物理表（缝合后） | **{len(tables)}** |")
    add(f"| 含分节点的物理表 | **{with_cuts}** |")
    add(f"| 切分后逻辑表 | **{total_logical}** |")
    add(f"| 类型分布 | {kind_counter} |")
    inherited = sum(
        1
        for table, result in details
        for logical in result.tables
        if logical.header_source == "inherited" and logical.header
    )
    add(f"| 继承到非空表头 | **{inherited}** |")
    add("")

    add("## 判据说明")
    add("")
    add("| 分类 | 判据 | 处理 |")
    add("|---|---|---|")
    add("| `data` + 自有表头 | 后继行像表头（≥3 非空、无纯数值、与父表头相似或父表无表头） | 切分，用自有表头 |")
    add(f"| `summary` | 表宽 ≥ {options.summary_min_width} 列，后继行非空 ≤ {options.summary_max_nonempty}，**且切出的段 ≤ {options.summary_max_rows} 行** | 切分，继承父表头 |")
    add("| `data`（分节标签） | 窄表且文本带冒号/中文序号；或宽表后继为数据行 | 切分，继承父表头 |")
    add("| `footnote` | 以「注」开头 或 长度 > 40 | **不切分** |")
    add("")
    add("**分节候选**来自两处：① HTML 里 `colspan` 跨满整行的全宽行；")
    add("② 「仅首列非空 + 标题形态」的行（MinerU 对同一语义可能用两种编码）。")
    add("")

    add("## 逐表概览")
    add("")
    add("| # | 物理表 | 页 | 列 | 行 | 分节点 | 逻辑表 |")
    add("|---|---|---|---|---|---|---|")
    for idx, (table, result) in enumerate(details, start=1):
        add(
            f"| {idx} | `{table.parent_id}` | P{table.page_start}-{table.page_end} "
            f"| {table.ncols} | {len(table.rows)} | {len(result.cuts)} | {result.count} |"
        )
    add("")

    add("## 逐表详情")
    add("")
    for idx, (table, result) in enumerate(details, start=1):
        add(f"### {idx}. `{table.parent_id}`   P{table.page_start}-P{table.page_end}"
            f"   {table.ncols} 列 / {len(table.rows)} 行")
        add("")
        add(f"**父表表头**：`{table.header}`")
        add("")

        if result.cuts:
            add("**分节点判定**")
            add("")
            add("| 数据行 | 文本 | 分类 | 自有表头 | 依据 |")
            add("|---|---|---|---|---|")
            for cut in result.cuts:
                text = cut.text if len(cut.text) <= 50 else cut.text[:47] + "…"
                add(
                    f"| r{cut.row:02d} | {text} | `{cut.kind}` "
                    f"| {'是' if cut.own_header else '否'} | {cut.reason} |"
                )
            add("")

        add("**切分结果**")
        add("")
        add("| 逻辑表 | 类型 | 表头来源 | 分节路径 | 行数 | 首行预览 |")
        add("|---|---|---|---|---|---|")
        for logical in result.tables:
            name = logical.logical_id.split("#")[-1]
            path = "/".join(logical.section_path) if logical.section_path else "—"
            if len(path) > 34:
                path = path[:31] + "…"
            first = _preview(logical.rows[0]) if logical.rows else "—"
            add(
                f"| `{name}` | {KIND_LABEL.get(logical.kind, logical.kind)} "
                f"| {logical.header_source} | {path} | {len(logical.rows)} | {first} |"
            )
        add("")

        add("<details><summary>分节骨架（含表头与分节标签）</summary>")
        add("")
        add("```")
        if table.header:
            add(f"[HEADER] {_preview(table.header, 80)}")
        cursor = 0
        for logical in result.tables:
            if logical.section_path:
                add(f"--- 分节：{'/'.join(logical.section_path)} ---")
            if logical.header_source == "self" and logical.header:
                add(f"[HEADER·自有] {_preview(logical.header, 70)}")
            for row in logical.rows:
                add(f"  r{cursor:02d} {_preview(row, 72)}")
                cursor += 1
        add("```")
        add("")
        add("</details>")
        add("")

    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--content-list", required=True)
    parser.add_argument("--doc-id", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    content_list = json.loads(Path(args.content_list).read_text(encoding="utf-8"))
    pages = content_list_to_pages(content_list, args.doc_id)
    strip_running_noise(pages)
    result = stitch_tables(pages)

    lines = build_report(args.doc_id, result.tables, SplitOptions())

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")

    logical_total = sum(1 for _ in lines if False)  # 占位，统计在报告内
    print(f"物理表: {len(result.tables)}")
    print(f"报告已写入: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
