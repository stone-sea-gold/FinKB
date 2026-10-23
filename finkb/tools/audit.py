"""审计与核验工具：快照 / 对照 / 守恒律 / 人工核验清单。

为什么需要它
------------
解析层每改一条判据，都会**静默**改变一批表的结论（单位、类型、表头）。
没有对照机制时，只能靠人重新通读 324 张表——实际上做不到，于是改动
变成「改完看着差不多就提交」，错误会一路带进 L1。

本工具把「改了什么」变成一份可以直接读的报告：

- ``snapshot``：把当前全部结论 + 判定依据导出为 JSON（改动前拍一次，改动后再拍一次）
- ``diff``：只列出**变化过的表**，一行一处变化，附改前/改后/依据
- ``invariants``：四条守恒律，任何一条失败即退出码 1（可挂进 CI）
- ``manual-check``：高风险表单独成册，供人工逐条打勾
- ``unit-map``：单位归属全表（324 行），一眼看出哪些表单位未定

只读
----
不调用 MinerU、不写 ``data/parsed``。所有结论都由 ``content_list.json``
经与生产完全相同的代码路径（``content_list_to_pages`` → ``strip_running_noise``
→ ``stitch_tables`` → ``split_all``）重算得出，因此报告即生产行为。

用法::

    cd finkb
    # 1. 改动前拍快照
    ../.venv/bin/python tools/audit.py snapshot --doc data/parsed/002594_2025_年报 \
        --out data/snapshots/before-summary.json

    # 2. 改动后拍快照
    ../.venv/bin/python tools/audit.py snapshot --doc data/parsed/002594_2025_年报 \
        --out data/snapshots/after-summary.json

    # 3. 出对照报告（只列变化过的表）
    ../.venv/bin/python tools/audit.py diff \
        --before data/snapshots/before-summary.json \
        --after  data/snapshots/after-summary.json \
        --out data/reports/fix-summary.md

    # 4. 守恒律（失败退出码 1）
    ../.venv/bin/python tools/audit.py invariants --doc data/parsed/002594_2025_年报

    # 5. 人工核验清单
    ../.venv/bin/python tools/audit.py manual-check --doc data/parsed/002594_2025_年报 \
        --out data/reports/manual-check-年报.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Collection

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from finkb.common.models import LogicalTable, Page, StitchedTable  # noqa: E402
from finkb.parser.markdown_blocks import html_table_to_rows  # noqa: E402
from finkb.parser.mineru_client import content_list_to_pages  # noqa: E402
from finkb.parser.noise_filter import strip_running_noise  # noqa: E402
from finkb.parser.section_splitter import SplitOptions, split_all  # noqa: E402
from finkb.parser.table_stitcher import stitch_tables  # noqa: E402

SCHEMA_VERSION = 1

#: 单位声明。``单位为：千元`` / ``单位：万元`` / ``单位: 元`` 都要能命中。
UNIT_DECL_RE = re.compile(r"单位\s*(?:为)?\s*[:：]\s*([^\s|，。]{1,10})")

#: 表内单位标记，形如 ``营业收入(元)``、``金额（万元）``。
UNIT_INLINE_RE = re.compile(r"[（(](万港元|千港元|亿元|万元|千元|港元|元)[)）]")

#: 长的写在前面，避免 ``万港元`` 被 ``港元`` 抢先命中。
UNIT_TOKENS = ("万港元", "千港元", "亿元", "万元", "千元", "港元", "元")

PURE_NUMBER_RE = re.compile(r"^[+-]?[（(]?[\d,]+(?:\.\d+)?[）)]?%?$")


# ──────────────────────────────────────────────────────────────
# 1. 重算：与生产完全相同的代码路径
# ──────────────────────────────────────────────────────────────
def load_document(parsed_dir: Path) -> tuple[str, list[Page], list[StitchedTable], list[LogicalTable]]:
    """从 ``content_list.json`` 重算到逻辑表。

    Returns:
        ``(doc_id, pages, stitched_tables, logical_tables)``
    """
    doc_id = parsed_dir.name
    content_list = json.loads((parsed_dir / "content_list.json").read_text(encoding="utf-8"))
    pages = content_list_to_pages(content_list, doc_id)
    strip_running_noise(pages)
    stitched = stitch_tables(pages)
    logical = split_all(stitched.tables, SplitOptions())
    return doc_id, pages, stitched.tables, logical


def build_table_index(pages: list[Page]) -> dict[tuple[int, int], int]:
    """``(page, 页内第几个表)`` → ``block_index``。

    ``table_id`` 形如 ``{doc_id}#P130_T77``，其中 ``T77`` 是**页内表格序号**
    （不是块序号）。要把表格和「单位声明」按文档顺序比较，必须先换回块序号。
    """
    index: dict[tuple[int, int], int] = {}
    for page in pages:
        seq = 0
        for block in page.blocks:
            if block.kind == "table":
                seq += 1
                index[(page.number, seq)] = block.block_index
    return index


def table_position(table: StitchedTable, index: dict[tuple[int, int], int]) -> tuple[int, int]:
    """逻辑表在文档中的位置 ``(page, block_index)``。"""
    match = re.search(r"#P(\d+)_T(\d+)$", table.table_id)
    if match is None:  # pragma: no cover - table_id 由 _table_ids 统一生成
        raise ValueError(f"无法解析 table_id：{table.table_id}")
    page, seq = int(match.group(1)), int(match.group(2))
    return page, index.get((page, seq), 0)


# ──────────────────────────────────────────────────────────────
# 2. 单位判定链
# ──────────────────────────────────────────────────────────────
@dataclass
class UnitDecl:
    """一处「单位：X」声明。"""

    page: int
    block_index: int
    unit: str
    text: str


@dataclass
class UnitDecision:
    """一张表的单位结论 + 依据。

    ``rule`` 取值：

    - ``in_table``：表内自带标记（列名或行标签里的 ``(元)``），最可靠；
    - ``scope_declaration``：作用域声明——从声明处起，作用到**下一个声明**为止；
    - ``unknown``：两级都判不出，**不猜**。
    """

    unit: str | None
    rule: str
    evidence: dict = field(default_factory=dict)


def normalize_unit(token: str) -> str | None:
    """从任意片段里取出规范单位名；取不到返回 ``None``。"""
    for unit in UNIT_TOKENS:
        if unit in token:
            return unit
    return None


def scan_unit_decls(pages: list[Page]) -> list[UnitDecl]:
    """按文档顺序收集全部单位声明。"""
    decls: list[UnitDecl] = []
    for page in pages:
        for block in page.blocks:
            for line in block.lines:
                match = UNIT_DECL_RE.search(line)
                if match is None:
                    continue
                unit = normalize_unit(match.group(1))
                if unit is None:
                    continue
                decls.append(UnitDecl(page.number, block.block_index, unit, line.strip()))
                break  # 一个块只取第一处，避免同一行重复计数
    return decls


def unit_from_table(table: LogicalTable) -> tuple[str | None, dict]:
    """从表内标记推断单位（第一级）。

    只认**明确写出来的**标记：``营业收入(元)``、``金额（万元）``。
    命中多个且互相矛盾时返回 ``None``，交给下一级——宁可 unknown 也不猜。
    """
    texts: list[str] = []
    if table.header:
        texts.extend(table.header)
    for row in table.rows:
        if row:
            texts.append(row[0])

    hits: list[str] = []
    sources: list[str] = []
    for text in texts:
        for match in UNIT_INLINE_RE.finditer(text):
            hits.append(match.group(1))
            sources.append(text.strip()[:40])
        decl = UNIT_DECL_RE.search(text)
        if decl:
            unit = normalize_unit(decl.group(1))
            if unit:
                hits.append(unit)
                sources.append(text.strip()[:40])

    if not hits:
        return None, {}
    distinct = sorted(set(hits))
    if len(distinct) > 1:
        return None, {"conflict": distinct, "samples": sources[:3]}
    return distinct[0], {"samples": sources[:3], "hits": len(hits)}


def resolve_unit(
    table: LogicalTable,
    decls: list[UnitDecl],
    position: tuple[int, int],
) -> UnitDecision:
    """两级判定：表内标记 → 作用域声明 → unknown。"""
    unit, evidence = unit_from_table(table)
    if unit is not None:
        return UnitDecision(unit, "in_table", evidence)

    # 作用域声明：取**最后一个不晚于本表**的声明。
    # 「不晚于」用 ``<=``，这样声明写在表格首行（同一块内）时也能命中。
    applicable = [d for d in decls if (d.page, d.block_index) <= position]
    if applicable:
        decl = applicable[-1]
        return UnitDecision(
            decl.unit,
            "scope_declaration",
            {"page": decl.page, "text": decl.text},
        )

    if evidence:  # 表内有标记但互相矛盾
        return UnitDecision(None, "unknown", {"reason": "表内单位互相矛盾", **evidence})
    return UnitDecision(None, "unknown", {"reason": "声明区之前，无可用单位"})


# ──────────────────────────────────────────────────────────────
# 3. 数值指纹
# ──────────────────────────────────────────────────────────────
def _parse_number(cell: str) -> str | None:
    """把单元格还原成规范数值字符串；不是纯数值返回 ``None``。

    括号表示负数（``(1,234)`` → ``-1234``），百分号保留。
    """
    text = cell.strip()
    if not PURE_NUMBER_RE.match(text):
        return None
    negative = text.startswith("(") or text.startswith("（")
    cleaned = text.strip("()（）").replace(",", "").rstrip("%")
    try:
        value = float(cleaned)
    except ValueError:  # pragma: no cover - 正则已保证可解析
        return None
    if negative:
        value = -value
    return f"{value:.10g}" + ("%" if text.endswith("%") else "")


def numeric_digest(rows: list[list[str]]) -> str:
    """数据行里全部数值的有序指纹。

    只覆盖**数值**：表头、类型、单位这些元数据的改动不应改变它。
    守恒律「数值不变性」就是比较改动前后的这个值——相等即证明数字没被动过。
    """
    tokens: list[str] = []
    for row in rows:
        for cell in row:
            number = _parse_number(cell)
            if number is not None:
                tokens.append(number)
    joined = "\x1f".join(tokens)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def table_facts(table: LogicalTable) -> dict:
    """判定时真正看得到的原始事实——供人工核验时自行判断。"""
    nonempty = [sum(1 for cell in row if cell.strip()) for row in table.rows]
    return {
        "rows": len(table.rows),
        "ncols": table.ncols,
        "max_nonempty_per_row": max(nonempty) if nonempty else 0,
        "has_header": table.header is not None,
        "header_first_empty": bool(table.header) and table.header[0] == "",
        "numeric_cells": sum(
            1 for row in table.rows for cell in row if _parse_number(cell) is not None
        ),
        "nonempty_cells": sum(1 for row in table.rows for cell in row if cell.strip()),
        "first_row": [cell.strip() for cell in table.rows[0]] if table.rows else [],
    }


# ──────────────────────────────────────────────────────────────
# 4. 快照
# ──────────────────────────────────────────────────────────────
def _analyze(parsed_dir: Path) -> tuple[dict, list[Page], list[StitchedTable], list[LogicalTable]]:
    """重算一次，同时产出快照与全部中间结果（避免同一份数据算两遍）。"""
    doc_id, pages, stitched, logical = load_document(parsed_dir)
    index = build_table_index(pages)
    decls = scan_unit_decls(pages)

    by_parent = {t.table_id: t for t in stitched}
    entries: dict[str, dict] = {}
    for table in logical:
        parent = by_parent.get(table.parent_id)
        position = table_position(parent, index) if parent else (table.page_start, 0)
        decision = resolve_unit(table, decls, position)
        entries[table.logical_id] = {
            "kind": table.kind,
            "header": table.header,
            "header_source": table.header_source,
            "section_path": table.section_path,
            "unit": decision.unit,
            "unit_rule": decision.rule,
            "unit_evidence": decision.evidence,
            "row_count": len(table.rows),
            "row_range": list(table.row_range),
            "page_start": table.page_start,
            "page_end": table.page_end,
            "numeric_digest": numeric_digest(table.rows),
            "facts": table_facts(table),
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "doc_id": doc_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "logical_tables": len(logical),
            "physical_tables": len(stitched),
            "kinds": dict(Counter(t.kind for t in logical)),
            "units": dict(Counter(e["unit"] or "(unknown)" for e in entries.values())),
            "unit_rules": dict(Counter(e["unit_rule"] for e in entries.values())),
        },
        "tables": entries,
    }, pages, stitched, logical


def make_snapshot(parsed_dir: Path) -> dict:
    """把当前全部结论 + 判定依据导出为可 diff 的字典。"""
    return _analyze(parsed_dir)[0]


# ──────────────────────────────────────────────────────────────
# 5. 对照报告
# ──────────────────────────────────────────────────────────────
#: diff 时比较的字段。顺序即报告里的展示顺序。
DIFF_FIELDS = ("kind", "unit", "unit_rule", "header_source", "header", "section_path", "row_count")


def _short(value: object, limit: int = 60) -> str:
    if value is None:
        return "—"
    if isinstance(value, list):
        text = " / ".join(str(item) for item in value)
    else:
        text = str(value)
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def diff_snapshots(before: dict, after: dict) -> tuple[str, list[str]]:
    """生成 Markdown 对照报告。

    Returns:
        ``(markdown, 变化过的表 id 列表)``
    """
    b_tables = before.get("tables", {})
    a_tables = after.get("tables", {})
    added = [key for key in a_tables if key not in b_tables]
    removed = [key for key in b_tables if key not in a_tables]
    common = [key for key in a_tables if key in b_tables]

    changed: list[tuple[str, str, object, object]] = []
    for key in common:
        for name in DIFF_FIELDS:
            old, new = b_tables[key].get(name), a_tables[key].get(name)
            if old != new:
                changed.append((key, name, old, new))

    touched = sorted({key for key, _, _, _ in changed})

    lines: list[str] = [
        f"# 改动对照：{before.get('doc_id', '?')}",
        "",
        f"- 改前快照：`{before.get('created_at', '?')}`（{len(b_tables)} 张逻辑表）",
        f"- 改后快照：`{after.get('created_at', '?')}`（{len(a_tables)} 张逻辑表）",
        f"- **变化过的表：{len(touched)} 张，共 {len(changed)} 处字段变化**",
        f"- 新增 {len(added)} 张 / 删除 {len(removed)} 张",
        "",
    ]

    if not changed and not added and not removed:
        lines.append("> 没有任何变化。若本次改动本应产生影响，说明判据没生效。")
        return "\n".join(lines), []

    if changed:
        lines += ["## 字段变化", "", "| 表号 | 字段 | 改前 | 改后 |", "|---|---|---|---|"]
        for key, name, old, new in changed:
            lines.append(f"| `{key}` | {name} | {_short(old)} | {_short(new)} |")
        lines.append("")

    if added:
        lines += ["## 新增的表", "", "| 表号 | kind | 行数 |", "|---|---|---|"]
        for key in added:
            entry = a_tables[key]
            lines.append(f"| `{key}` | {entry['kind']} | {entry['row_count']} |")
        lines.append("")

    if removed:
        lines += ["## 消失的表", "", "| 表号 | kind | 行数 |", "|---|---|---|"]
        for key in removed:
            entry = b_tables[key]
            lines.append(f"| `{key}` | {entry['kind']} | {entry['row_count']} |")
        lines.append("")

    lines += [
        "## 守恒律速查",
        "",
        "| 表号 | 数值指纹 | 页码 | 结论 |",
        "|---|---|---|---|",
    ]
    for key in touched:
        old, new = b_tables[key], a_tables[key]
        digest_ok = old["numeric_digest"] == new["numeric_digest"]
        page_ok = (old["page_start"], old["page_end"]) == (new["page_start"], new["page_end"])
        verdict = []
        if not digest_ok:
            verdict.append("**数值变了**")
        if not page_ok:
            verdict.append("**页码变了**")
        lines.append(
            f"| `{key}` | {new['numeric_digest']} | P{new['page_start']}-P{new['page_end']} "
            f"| {'、'.join(verdict) or 'OK'} |"
        )
    lines.append("")
    return "\n".join(lines), touched


# ──────────────────────────────────────────────────────────────
# 6. 守恒律
# ──────────────────────────────────────────────────────────────
@dataclass
class Check:
    """一条守恒律的校验结果。"""

    name: str
    ok: bool
    detail: str


def _row_value(table: LogicalTable, label: str, column: int = 0) -> float | None:
    """取某一行在指定数值列上的值（按列位置跳过标签列）。"""
    for row in table.rows:
        if not row or row[0].strip() != label:
            continue
        for cell in row[1:]:
            number = _parse_number(cell)
            if number is not None:
                return float(number.rstrip("%"))
        return None
    return None


def _first_numeric_column(table: LogicalTable) -> int:
    """找出第一个「多数单元格是纯数值」的列下标。"""
    if not table.rows:
        return 1
    width = max(len(row) for row in table.rows)
    best, best_hits = 1, -1
    for col in range(1, width):
        hits = sum(
            1
            for row in table.rows
            if col < len(row) and _parse_number(row[col]) is not None
        )
        if hits > best_hits:
            best, best_hits = col, hits
    return best


def _check_cash_flow_table(section: str, table: LogicalTable) -> Check:
    """单张现金流量表的三段恒等式。"""
    short = table.logical_id.split("#", 1)[-1]
    col = _first_numeric_column(table)
    inflow = _row_value(table, f"{section}现金流入小计", col)
    outflow = _row_value(table, f"{section}现金流出小计", col)
    net = None
    for row in table.rows:
        label = row[0].strip() if row else ""
        if label.startswith(section) and label.endswith("现金流量净额"):
            # 必须按 ``col`` 取，不能取「行内第一个数值」——
            # 「附注七」列里也有数字（61、62），会把附注号当成金额。
            if col < len(row):
                number = _parse_number(row[col])
                if number is not None:
                    net = float(number)
            break

    if None in (inflow, outflow, net):
        return Check(f"现金流量表·{section}", False, f"{short}：行存在但取不到数值")

    delta = inflow - outflow - net
    return Check(
        f"现金流量表·{section}",
        abs(delta) < 1,
        f"{inflow:,.0f} − {outflow:,.0f} = {inflow - outflow:,.0f}，"
        f"表列净额 {net:,.0f}（{short}）",
    )


def check_cash_flow(logical: list[LogicalTable]) -> list[Check]:
    """现金流量表：净额 = 流入小计 − 流出小计。

    同一组指标可能在 MD&A 摘要表（元）与正式报表（千元）里各出现一次，
    **两张都要查**——只查第一张会漏掉另一份口径。
    """
    checks: list[Check] = []
    for section in ("经营活动", "投资活动", "筹资活动"):
        wanted = {f"{section}现金流入小计", f"{section}现金流出小计"}
        targets = [
            table
            for table in logical
            if wanted <= {row[0].strip() for row in table.rows if row}
        ]
        if not targets:
            checks.append(Check(f"现金流量表·{section}", False, "未找到流入/流出小计行"))
            continue
        checks.extend(_check_cash_flow_table(section, table) for table in targets)
    return checks


def check_balance_sheet(logical: list[LogicalTable]) -> list[Check]:
    """资产负债表：资产总计 = 负债合计 + 股东权益合计。"""
    assets = equity = None
    for table in logical:
        labels = {row[0].strip() for row in table.rows if row}
        if "资产总计" in labels and assets is None:
            assets = table
        if "负债合计" in labels and "股东权益合计" in labels and equity is None:
            equity = table

    if assets is None or equity is None:
        return [Check("资产负债表恒等式", False, "未找到资产总计或负债/权益合计")]

    col_a = _first_numeric_column(assets)
    col_e = _first_numeric_column(equity)
    total_assets = _row_value(assets, "资产总计", col_a)
    total_liab = _row_value(equity, "负债合计", col_e)
    total_equity = _row_value(equity, "股东权益合计", col_e)

    if None in (total_assets, total_liab, total_equity):
        return [Check("资产负债表恒等式", False, "行存在但取不到数值")]

    delta = total_assets - total_liab - total_equity
    return [
        Check(
            "资产负债表恒等式",
            abs(delta) < 1,
            f"资产 {total_assets:,.0f} vs 负债 {total_liab:,.0f} + 权益 {total_equity:,.0f}"
            f"（差额 {delta:,.0f}）",
        )
    ]


def check_invariants(
    parsed_dir: Path,
    before: dict | None = None,
    allow_digest: Collection[str] = (),
) -> list[Check]:
    """跑全部守恒律。

    ``allow_digest`` 是**已人工核对并认可**的数值指纹变化。行跨展开会把合并单元格
    的内容复制到被覆盖的每一行，数值因此重复出现，指纹必然变化——这类变化本身
    是对的，但要逐张核对过才能进白名单，所以默认仍报失败。
    """
    current, pages, stitched, logical = _analyze(parsed_dir)
    checks: list[Check] = []
    allowed = set(allow_digest)

    # 1. 数值不变性 / 2. 页码不变性（需要基线快照）
    if before is not None:
        numeric_bad, page_bad, missing, accepted = [], [], [], []
        for key, old in before["tables"].items():
            new = current["tables"].get(key)
            if new is None:
                missing.append(key)
                continue
            if old["numeric_digest"] != new["numeric_digest"]:
                (accepted if key in allowed else numeric_bad).append(key)
            if (old["page_start"], old["page_end"]) != (new["page_start"], new["page_end"]):
                page_bad.append(key)
        detail = "全部一致"
        if numeric_bad:
            detail = f"{len(numeric_bad)} 张数值变了：{numeric_bad[:5]}"
        elif accepted:
            detail = f"全部一致（另有 {len(accepted)} 张已核对认可：{accepted[:5]}）"
        checks.append(Check("数值不变性", not numeric_bad, detail))
        checks.append(
            Check(
                "页码不变性",
                not page_bad,
                "全部一致" if not page_bad else f"{len(page_bad)} 张页码变了：{page_bad[:5]}",
            )
        )
        checks.append(
            Check(
                "表数守恒",
                not missing,
                f"基线 {len(before['tables'])} 张 → 当前 {len(current['tables'])} 张"
                + ("" if not missing else f"，消失 {len(missing)} 张：{missing[:5]}"),
            )
        )
    else:
        checks.append(Check("数值不变性", True, "跳过（未提供 --before 基线）"))
        checks.append(Check("页码不变性", True, "跳过（未提供 --before 基线）"))
        checks.append(
            Check("表数守恒", True, f"当前 {len(logical)} 张逻辑表 / {len(stitched)} 张物理表（无基线）")
        )

    # 4. 算术恒等式
    checks.extend(check_balance_sheet(logical))
    checks.extend(check_cash_flow(logical))

    # 5. 页码合法性
    page_numbers = {page.number for page in pages}
    illegal = [
        t.logical_id
        for t in logical
        if t.page_start not in page_numbers or t.page_end not in page_numbers
    ]
    checks.append(
        Check(
            "页码合法性",
            not illegal,
            "全部落在 1..%d" % len(pages) if not illegal else f"{len(illegal)} 张越界：{illegal[:5]}",
        )
    )

    return checks


# ──────────────────────────────────────────────────────────────
# 7. 人工核验清单
# ──────────────────────────────────────────────────────────────
#: 高风险判据：命中任何一条就要人工看一眼。
def _looks_like_header(row: list[str]) -> bool:
    """首行是否「像表头」——只用于**圈出待人工确认的候选**，不做自动判定。

    纯文本表（释义表、承诺表）在结构上与数据行完全同构
    （``释义项|指|释义内容`` vs ``比亚迪|指|比亚迪股份有限公司``），
    任何规则都会同时命中或同时漏掉，所以这里只负责**缩小人工范围**。
    """
    cells = [cell for cell in row if cell]
    if len(cells) < 2:
        return False
    if any(_parse_number(cell) is not None for cell in cells):
        return False
    return max(len(cell) for cell in cells) <= 12


def risky_tables(snapshot: dict) -> dict[str, list[str]]:
    """按风险类别归集表号。"""
    buckets: dict[str, list[str]] = {
        "非 data 类型（summary / kv / kv_row）": [],
        "单位未定（unknown）": [],
        "无表头": [],
        "全空表（无任何非空单元格）": [],
        "表头首格为空": [],
        "首行像表头但未识别（P1-1 待人工确认）": [],
    }
    for key, entry in snapshot["tables"].items():
        if entry["kind"] != "data":
            buckets["非 data 类型（summary / kv / kv_row）"].append(key)
        if entry["unit"] is None:
            buckets["单位未定（unknown）"].append(key)
        if not entry["facts"]["has_header"]:
            buckets["无表头"].append(key)
        if entry["facts"]["nonempty_cells"] == 0:
            buckets["全空表（无任何非空单元格）"].append(key)
        if entry["facts"]["header_first_empty"]:
            buckets["表头首格为空"].append(key)
        if not entry["facts"]["has_header"] and _looks_like_header(entry["facts"]["first_row"]):
            buckets["首行像表头但未识别（P1-1 待人工确认）"].append(key)
    return buckets


#: 只有这些类别值得逐张展开；其余只报数量。
#: ``表头首格为空`` 是财务表的排版特征（213/324），不是缺陷，展开只会淹没重点。
DETAIL_BUCKETS = (
    "非 data 类型（summary / kv / kv_row）",
    "单位未定（unknown）",
    "首行像表头但未识别（P1-1 待人工确认）",
)


def _cells(cells: list[str], limit: int = 80) -> str:
    """把一行单元格渲染成可读形式，空格用 ``·`` 占位。"""
    text = " | ".join(cell if cell else "·" for cell in cells)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_manual_check(snapshot: dict, parsed_dir: Path, max_per_bucket: int = 20) -> str:
    """生成人工核验清单（Markdown）。"""
    doc_id = snapshot["doc_id"]
    buckets = risky_tables(snapshot)
    lines = [
        f"# 人工核验清单：{doc_id}",
        "",
        f"生成于 {datetime.now().isoformat(timespec='seconds')}，"
        f"共 {snapshot['summary']['logical_tables']} 张逻辑表。",
        "",
        "核验方法：翻到对应 PDF 页码，对照「当前判定」与「原始事实」是否一致。",
        "",
        "## 风险概览",
        "",
        "| 类别 | 数量 | 是否展开 |",
        "|---|---|---|",
    ]
    for name, keys in buckets.items():
        detail = "是" if name in DETAIL_BUCKETS else "否（只统计）"
        lines.append(f"| {name} | {len(keys)} | {detail} |")
    lines.append("")

    for name in DETAIL_BUCKETS:
        keys = buckets.get(name, [])
        if not keys:
            continue
        lines += [f"## {name}（{len(keys)} 张）", ""]
        if len(keys) > max_per_bucket:
            lines.append(f"> 仅展示前 {max_per_bucket} 张，完整清单见快照 JSON。")
            lines.append("")
        for key in keys[:max_per_bucket]:
            entry = snapshot["tables"][key]
            facts = entry["facts"]
            evidence = entry.get("unit_evidence") or {}
            lines += [
                f"### `{key.split('#', 1)[-1]}`  （P{entry['page_start']}–P{entry['page_end']}）",
                "",
                f"- 当前判定：kind=`{entry['kind']}`，"
                f"单位=`{entry['unit'] or '未定'}`（{entry['unit_rule']}）",
                f"- 原始事实：{facts['rows']} 行 × {facts['ncols']} 列，"
                f"数值单元格 {facts['numeric_cells']} 个，"
                f"表头 {'有' if facts['has_header'] else '无'}",
            ]
            if entry["header"]:
                lines.append(f"- 表头：`{_cells(entry['header'])}`")
            elif facts["first_row"]:
                lines.append(f"- 首行：`{_cells(facts['first_row'])}`")
            if evidence:
                lines.append(f"- 单位依据：`{json.dumps(evidence, ensure_ascii=False)}`")
            lines += ["", "- [ ] 与 PDF 原文一致", ""]
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 8. 单位归属全表
# ──────────────────────────────────────────────────────────────
def render_unit_map(snapshot: dict) -> str:
    """每张表的单位 + 判定级别 + 证据页码，一眼看出谁没定。"""
    lines = [
        f"# 单位归属全表：{snapshot['doc_id']}",
        "",
        "| 表号 | 页 | kind | 单位 | 判定级别 | 依据 |",
        "|---|---|---|---|---|---|",
    ]
    for key, entry in snapshot["tables"].items():
        evidence = entry.get("unit_evidence") or {}
        if entry["unit_rule"] == "scope_declaration":
            ref = f"P{evidence.get('page', '?')}「{_short(evidence.get('text'), 30)}」"
        elif entry["unit_rule"] == "in_table":
            ref = _short(evidence.get("samples"), 30)
        else:
            ref = _short(evidence.get("reason"), 30)
        lines.append(
            f"| `{key}` | {entry['page_start']} | {entry['kind']} | "
            f"{entry['unit'] or '**未定**'} | {entry['unit_rule']} | {ref} |"
        )
    lines.append("")
    lines.append("## 统计")
    lines.append("")
    for name, count in snapshot["summary"]["units"].items():
        lines.append(f"- {name}：{count} 张")
    lines.append("")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 8.5 rowspan 展开对照
# ──────────────────────────────────────────────────────────────
#: ``rowspan="2"`` / ``rowspan='2'`` 都要能剥掉。
_ROWS_ATTR_RE = re.compile(r"""\s+rowspan\s*=\s*("[^"]*"|'[^']*')""", re.IGNORECASE)


def _show_row(cells: list[str]) -> str:
    """把一行渲染成「· 分隔」的形式，空单元格显式画成 ``·``，便于看出错位。"""
    return " | ".join(cell if cell.strip() else "·" for cell in cells)


def render_rowspan_report(parsed_dir: Path) -> str:
    """列出所有含 ``rowspan`` 的表，给出「展开前 / 展开后」逐行对照。

    「展开前」用「删掉 HTML 里所有 ``rowspan`` 属性」复现：旧实现只记录不展开
    ``rowspan``，等价于每个单元格都是 ``rowspan=1``。因此这份对照精确等于
    「本次改动前后的差异」，而不是重新实现一套判据。
    """
    payload = json.loads((parsed_dir / "content_list.json").read_text(encoding="utf-8"))
    doc_id = parsed_dir.name

    total = with_rowspan = changed = 0
    empty_body = 0
    blocks: list[str] = []
    page_ordinal: dict[int, int] = {}

    for block in payload:
        if block.get("type") != "table":
            continue
        total += 1
        page = int(block.get("page_idx", 0)) + 1
        page_ordinal[page] = page_ordinal.get(page, 0) + 1
        html = block.get("table_body") or block.get("html") or ""
        if not html.strip():
            # MinerU 识别到表格区域但没产出 HTML；``content_list_to_pages``
            # 会因取不到文本而整块跳过，这里如实计数，不让它无声消失。
            empty_body += 1
            continue
        if "rowspan" not in html.lower():
            continue
        with_rowspan += 1

        old_rows, _, _ = html_table_to_rows(_ROWS_ATTR_RE.sub("", html))
        new_rows, _, _ = html_table_to_rows(html)
        width = max([len(row) for row in old_rows + new_rows] or [0])
        if width:
            old_rows = [row + [""] * (width - len(row)) for row in old_rows]
            new_rows = [row + [""] * (width - len(row)) for row in new_rows]
        if old_rows == new_rows:
            continue

        changed += 1
        blocks.append(f"### P{page} 表格#{page_ordinal[page]}")
        blocks.append("")
        blocks.append(f"- 列数：{width}　行数：{len(new_rows)}")
        blocks.append("- 原始 HTML：")
        blocks.append("")
        blocks.append("```html")
        blocks.append(html[:400] + ("…" if len(html) > 400 else ""))
        blocks.append("```")
        blocks.append("")
        shown = 0
        for index in range(max(len(old_rows), len(new_rows))):
            before = old_rows[index] if index < len(old_rows) else []
            after = new_rows[index] if index < len(new_rows) else []
            if before == after:
                continue
            blocks.append(f"- **第 {index} 行**")
            blocks.append(f"  - 改前：{_show_row(before)}")
            blocks.append(f"  - 改后：{_show_row(after)}")
            shown += 1
            if shown >= 4:
                blocks.append("- …（其余行相同，略）")
                break
        blocks.append("- [ ] 我核对过 PDF 这一处")
        blocks.append("")

    lines = [
        f"# rowspan 展开对照：{doc_id}",
        "",
        f"- `content_list` 中的 table 块：{total} 张",
        f"- 其中 `table_body` 为空（MinerU 识别到表格区域但没产出 HTML，"
        f"解析时整块跳过）：{empty_body} 张",
        f"- 实际进入解析：{total - empty_body} 张",
        f"- 含 `rowspan` 的表：{with_rowspan} 张",
        f"- **展开后内容发生变化：{changed} 张**",
        "",
        "> 对照方法：把 HTML 里的 `rowspan` 属性全部删掉，再用同一个解析函数算一遍，",
        "> 得到「改前」；直接解析得到「改后」。两者只差 rowspan 是否进位，",
        "> 因此这份清单就是本次改动的完整影响面。",
        ">",
        "> 注意：行跨展开会把合并单元格的内容**复制**到被覆盖的每一行，所以含数字的",
        "> 合并格会让同一数字在表里重复出现——这类表在 `invariants --before` 里会报",
        "> 「数值指纹变化」。逐张核对确认是「复制」而不是「错位」后，用",
        "> `--allow-digest` 显式认可，不要让白名单悄悄变长。",
        "",
    ]
    if blocks:
        lines.extend(blocks)
    else:
        lines.append("没有任何表的内容发生变化。若本次改动本应产生影响，说明 rowspan 没被展开。")
        lines.append("")
    return "\n".join(lines)


def _cmd_rowspan(args: argparse.Namespace) -> int:
    report = render_rowspan_report(Path(args.doc).expanduser().resolve())
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"rowspan 对照已写入 {out}")
    else:
        print(report)
    return 0


# ──────────────────────────────────────────────────────────────
# 9. CLI
# ──────────────────────────────────────────────────────────────
def _cmd_snapshot(args: argparse.Namespace) -> int:
    parsed_dir = Path(args.doc).expanduser().resolve()
    snapshot = make_snapshot(parsed_dir)
    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = snapshot["summary"]
    print(f"快照已写入 {out}")
    print(f"  逻辑表 {summary['logical_tables']} 张 / 物理表 {summary['physical_tables']} 张")
    print(f"  类型分布 {summary['kinds']}")
    print(f"  单位分布 {summary['units']}")
    print(f"  判定级别 {summary['unit_rules']}")
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    before = json.loads(Path(args.before).expanduser().resolve().read_text(encoding="utf-8"))
    after = json.loads(Path(args.after).expanduser().resolve().read_text(encoding="utf-8"))
    markdown, touched = diff_snapshots(before, after)

    if args.out:
        out = Path(args.out).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown, encoding="utf-8")
        print(f"对照报告已写入 {out}")
    else:
        print(markdown)

    print(f"\n变化过的表：{len(touched)} 张")
    for key in touched[:10]:
        print(f"  {key}")
    if len(touched) > 10:
        print(f"  … 另有 {len(touched) - 10} 张，见报告")
    return 0


def _cmd_invariants(args: argparse.Namespace) -> int:
    parsed_dir = Path(args.doc).expanduser().resolve()
    before = None
    if args.before:
        before = json.loads(Path(args.before).expanduser().resolve().read_text(encoding="utf-8"))

    raw_allow = args.allow_digest or ""
    allow = [key.strip() for key in raw_allow.split(",") if key.strip()]
    checks = check_invariants(parsed_dir, before, allow)
    width = max(len(check.name) for check in checks)
    failed = 0
    for check in checks:
        flag = "✓" if check.ok else "✗"
        print(f"  {flag} {check.name.ljust(width)}  {check.detail}")
        if not check.ok:
            failed += 1

    print()
    if failed:
        print(f"{failed} 条守恒律失败——拒绝提交。")
        return 1
    print(f"全部 {len(checks)} 条守恒律通过。")
    return 0


def _cmd_manual_check(args: argparse.Namespace) -> int:
    parsed_dir = Path(args.doc).expanduser().resolve()
    snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8")) if args.snapshot else make_snapshot(parsed_dir)
    markdown = render_manual_check(snapshot, parsed_dir, max_per_bucket=args.max_per_bucket)

    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown, encoding="utf-8")
    print(f"人工核验清单已写入 {out}")

    buckets = risky_tables(snapshot)
    for name, keys in buckets.items():
        print(f"  {name}: {len(keys)} 张")
    return 0


def _cmd_unit_map(args: argparse.Namespace) -> int:
    parsed_dir = Path(args.doc).expanduser().resolve()
    snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8")) if args.snapshot else make_snapshot(parsed_dir)
    markdown = render_unit_map(snapshot)

    if args.out:
        out = Path(args.out).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown, encoding="utf-8")
        print(f"单位归属全表已写入 {out}")
    else:
        print(markdown)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="审计与核验：快照 / 对照 / 守恒律 / 人工核验清单",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rowspan = sub.add_parser("rowspan", help="rowspan 展开前后逐行对照")
    rowspan.add_argument("--doc", required=True, help="已解析目录，如 data/parsed/002594_2025_年报")
    rowspan.add_argument("--out", help="报告输出路径（.md），省略则打印到标准输出")
    rowspan.set_defaults(func=_cmd_rowspan)

    snapshot = sub.add_parser("snapshot", help="导出全量结论快照")
    snapshot.add_argument("--doc", required=True, help="已解析目录，如 data/parsed/002594_2025_年报")
    snapshot.add_argument("--out", required=True, help="快照输出路径（.json）")
    snapshot.set_defaults(func=_cmd_snapshot)

    diff = sub.add_parser("diff", help="对比两份快照，只列出变化过的表")
    diff.add_argument("--before", required=True)
    diff.add_argument("--after", required=True)
    diff.add_argument("--out", help="Markdown 输出路径；省略则打印到 stdout")
    diff.set_defaults(func=_cmd_diff)

    invariants = sub.add_parser("invariants", help="跑守恒律，失败退出码 1")
    invariants.add_argument("--doc", required=True)
    invariants.add_argument("--before", help="基线快照；提供后才会校验数值/页码不变性")
    invariants.add_argument(
        "--allow-digest",
        help="已人工核对认可的数值指纹变化，逗号分隔的逻辑表 id",
    )
    invariants.set_defaults(func=_cmd_invariants)

    manual = sub.add_parser("manual-check", help="生成人工核验清单")
    manual.add_argument("--doc", required=True)
    manual.add_argument("--snapshot", help="复用已有快照；省略则现场重算")
    manual.add_argument("--out", required=True)
    manual.add_argument("--max-per-bucket", type=int, default=20)
    manual.set_defaults(func=_cmd_manual_check)

    unit_map = sub.add_parser("unit-map", help="单位归属全表")
    unit_map.add_argument("--doc", required=True)
    unit_map.add_argument("--snapshot", help="复用已有快照；省略则现场重算")
    unit_map.add_argument("--out")
    unit_map.set_defaults(func=_cmd_unit_map)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
