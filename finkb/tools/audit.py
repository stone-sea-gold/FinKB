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
from finkb.parser.markdown_blocks import (  # noqa: E402
    html_table_layout,
    html_table_to_rows,
)
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

    **保留全部有效位**：报表有「千元整数」也有「元、两位小数」两种口径，
    后者一个数就有 14 位有效数字。按 10 位截断会让
    ``190,501,783,194.92`` 变成 ``190,501,783,200``，恒等式凭空差 30 元。
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
    return f"{value:.15g}" + ("%" if text.endswith("%") else "")


def _fingerprint_token(number: str) -> str:
    """把已解析的数值再归一成指纹用的短形式（10 位有效数字）。

    指纹的职责是「数字有没有被改动」，不是「精确到分」。归一化让
    ``1,234`` 与 ``1,234.00000001`` 同指纹，也保证换精度不会让历史快照
    全部变成「数值已变」。
    """
    percent = number.endswith("%")
    rounded = f"{float(number.rstrip('%')):.10g}"
    return rounded + ("%" if percent else "")


def _has_bad_grouping(cell: str) -> bool:
    """千分位分组不合法：``602,855430,242``（两个数粘在一起）、``0,01%``（小数逗号）。

    合法写法只有「首组 1–3 位、其余每组恰好 3 位」。粘连与小数逗号都会破坏这条规则，
    而它们都会让数字本身变成错的——比列名错更值得报出来。
    """
    text = cell.strip().lstrip("-+").rstrip("%").replace("(", "").replace(")", "")
    if "," not in text or _parse_number(text) is None:
        return False
    groups = text.split(".")[0].split(",")
    return not (1 <= len(groups[0]) <= 3 and all(len(group) == 3 for group in groups[1:]))


def find_malformed_numbers(logical: list[LogicalTable]) -> list[tuple[str, int, str]]:
    """返回 ``(logical_id, 行号, 单元格原文)``；行号从 0 起。"""
    found: list[tuple[str, int, str]] = []
    for table in logical:
        for index, row in enumerate(table.rows):
            for cell in row:
                if _has_bad_grouping(cell):
                    found.append((table.logical_id, index, cell.strip()))
    return found


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
                tokens.append(_fingerprint_token(number))
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
            "period_label": table.period_label,
            "caption": table.caption,
            "caption_source": table.caption_source,
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
DIFF_FIELDS = ("kind", "unit", "unit_rule", "header_source", "header", "section_path",
               "period_label", "caption", "caption_source", "row_count")


def _short(value: object, limit: int = 60) -> str:
    if value is None:
        return "—"
    if isinstance(value, list):
        text = " / ".join(str(item) for item in value)
    else:
        text = str(value)
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


#: 没有任何数值单元格时 ``numeric_digest`` 的取值。
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()[:16]


def parent_digests(snapshot: dict) -> dict[str, list[str]]:
    """按**物理表**聚合非空数值指纹的多重集。

    分节边界可能移动——多行表头被吃掉后，原本单独成段的表头行并入下一段，
    逻辑表号随之改变，但同一张物理表里的数字一个也不会多、一个也不会少。
    只按逻辑表号比指纹会把这种「搬家」误报成「数值变了」。
    """
    out: dict[str, list[str]] = {}
    for key, entry in snapshot["tables"].items():
        digest = entry["numeric_digest"]
        if digest == EMPTY_DIGEST:
            continue
        out.setdefault(key.split("#")[0], []).append(digest)
    for values in out.values():
        values.sort()
    return out


#: 表号里的页码段；``#P94_T57`` 与 ``#P94-P103_T57`` 指同一张表。
_PAGE_SEGMENT_RE = re.compile(r"#P[0-9P\-]+_T(\d+)")


def _table_key(key: str) -> str:
    """把页码段抹平成表序号，用来跨 id 认出同一张表。

    只抹页码、不动 ``_T{seq}``：表序号是这张表在文档里的位次，页码区间变化
    （跨页表锚点回填）不影响它。若连表序号都不同，那就是两张表，哪怕内容
    一模一样也不能配成「改号」——合并报表与母公司报表常有完全相同的两张表。
    """
    return _PAGE_SEGMENT_RE.sub(r"#T\1", key)


def _pair_renames(
    b_tables: dict, a_tables: dict, added: list[str], removed: list[str]
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """把「表号变了、内容没变」的删除与新增配成一对。

    MinerU 把跨页表格合并成一个块后，表号里的页码区间会从 ``P94`` 扩成
    ``P94-P103``——id 全变，而 kind、行数、数值指纹三项一个都不会变。
    不配对的话，报告读起来像是「29 张表消失了」，是最容易误导人的写法。
    """

    def sig(entry: dict) -> tuple[object, ...]:
        return (entry["kind"], entry["row_count"], entry["numeric_digest"])

    pool: dict[tuple[object, ...], list[str]] = {}
    for key in added:
        pool.setdefault((sig(a_tables[key]), _table_key(key)), []).append(key)

    pairs: list[tuple[str, str]] = []
    rest_removed: list[str] = []
    for key in removed:
        bucket = pool.get((sig(b_tables[key]), _table_key(key)))
        if bucket:
            pairs.append((key, bucket.pop(0)))
        else:
            rest_removed.append(key)

    paired = {new for _, new in pairs}
    return pairs, [key for key in added if key not in paired], rest_removed


def diff_snapshots(before: dict, after: dict) -> tuple[str, list[str]]:
    """生成 Markdown 对照报告。

    Returns:
        ``(markdown, 变化过的表 id 列表)``
    """
    b_tables = before.get("tables", {})
    a_tables = after.get("tables", {})
    # 旧快照没有新加的字段时不能报成「全部变化」，统一补默认值
    for tables in (b_tables, a_tables):
        for record in tables.values():
            record.setdefault("period_label", "")
            record.setdefault("caption", "")
            record.setdefault("caption_source", "missing")
    added = [key for key in a_tables if key not in b_tables]
    removed = [key for key in b_tables if key not in a_tables]
    common = [key for key in a_tables if key in b_tables]
    renames, added, removed = _pair_renames(b_tables, a_tables, added, removed)

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
        f"- 改号 {len(renames)} 张（仅页码区间变化）"
        f" / 新增 {len(added)} 张 / 删除 {len(removed)} 张",
        "",
    ]

    if not changed and not added and not removed and not renames:
        lines.append("> 没有任何变化。若本次改动本应产生影响，说明判据没生效。")
        return "\n".join(lines), []

    if changed:
        lines += ["## 字段变化", "", "| 表号 | 字段 | 改前 | 改后 |", "|---|---|---|---|"]
        for key, name, old, new in changed:
            lines.append(f"| `{key}` | {name} | {_short(old)} | {_short(new)} |")
        lines.append("")

    if renames:
        lines += [
            "## 改号（页码区间变化，内容未变）",
            "",
            "> 表号里的页码区间变了，但 kind、行数、数值指纹三项全一致。",
            "> 典型原因是 MinerU 把跨页表格合并成一个块，锚点从 `P94` 扩成 `P94-P103`。",
            "",
            "| 改前表号 | 改后表号 | kind | 行数 |",
            "|---|---|---|---|",
        ]
        for old_key, new_key in renames:
            entry = a_tables[new_key]
            lines.append(
                f"| `{old_key}` | `{new_key}` | {entry['kind']} | {entry['row_count']} |"
            )
        lines.append("")

    if added:
        lines += ["## 新增的表", "", "| 表号 | kind | 行数 |", "|---|---|---|"]
        for key in added:
            entry = a_tables[key]
            lines.append(f"| `{key}` | {entry['kind']} | {entry['row_count']} |")
        lines.append("")

    if removed:
        lines += [
            "## 消失的表",
            "",
            "> 逻辑表号消失不等于数据丢失：分节边界移动会把两段并成一段。",
            "> 判断依据看下一节「守恒律速查」——物理表的数值指纹守恒即说明数字还在。",
            "",
            "| 表号 | kind | 行数 |",
            "|---|---|---|",
        ]
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
    old_parents, new_parents = parent_digests(before), parent_digests(after)
    for key in touched:
        old, new = b_tables[key], a_tables[key]
        parent = key.split("#")[0]
        # 先看自己有没有变；变了不代表错——可能只是分节边界挪了位置，
        # 所以再看同一张物理表的数字总量是否守恒。
        digest_ok = old["numeric_digest"] == new["numeric_digest"] or (
            old_parents.get(parent) == new_parents.get(parent)
        )
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


def _looks_like_money(cell: str) -> bool:
    """金额单元格：带千分位或小数点，或至少 5 位数字。

    附注号的「62」「61」不是金额，靠这条排除——否则现金流量表的「附注七」列
    会被当成金额列。
    """
    if _parse_number(cell) is None:
        return False
    digits = cell.replace(",", "").replace(".", "").replace("-", "").strip("%")
    return "," in cell or "." in cell or len(digits) >= 5


def _first_numeric_column(table: LogicalTable) -> int:
    """当期金额所在列。

    取**最左边**那个「多数行是金额」的列，而不是数值最多的列：两期并排时上期
    那列往往更满（本期有空白），按「最多」会选到上期，把上期净额当成本期
    （000625 半年报 `P42-P43_T37#S02` 就这么错了）。没有明显金额列时退回
    数值最多的旧规则。
    """
    if not table.rows:
        return 1
    width = max(len(row) for row in table.rows)
    for col in range(1, width):
        hits = sum(
            1 for row in table.rows if col < len(row) and _looks_like_money(row[col])
        )
        if hits * 2 >= len(table.rows):
            return col
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


_ZERO_MARKERS = {"-", "--", "—", "——", "/", "－", "－"}


def _amount_in_column(table: LogicalTable, label: str, column: int) -> float | None:
    """取指定行在指定列上的金额；**单元格为空或破折号**（不是缺行）时记 0。

    母公司现金流量表常整段留空（本期没有筹资流入），PDF 原文就是空的，
    按 0 处理才符合恒等式；真丢数据时等式仍然对不上，不会被掩盖。
    报表里的 ``-`` / ``—`` / ``/`` 表示「本期无金额」，与空单元格同义
    （中国神华 2025 年报的筹资流入小计就是 ``-``）。
    """
    for row in table.rows:
        if not row or row[0].strip() != label:
            continue
        if column < len(row):
            number = _parse_number(row[column])
            if number is not None:
                return float(number.rstrip("%"))
            if not row[column].strip() or row[column].strip() in _ZERO_MARKERS:
                return 0.0
        return None
    return None


def _check_cash_flow_table(section: str, table: LogicalTable) -> Check:
    """单张现金流量表的三段恒等式。"""
    short = table.logical_id.split("#", 1)[-1]
    col = _first_numeric_column(table)
    inflow = _amount_in_column(table, f"{section}现金流入小计", col)
    outflow = _amount_in_column(table, f"{section}现金流出小计", col)
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

    # 流出按绝对值算：银行、保险的现金流量表把流出印成 (596,456) 这种负数，
    # 而制造业印成正数。`inflow - abs(outflow)` 两种写法都对。
    delta = inflow - abs(outflow) - net
    return Check(
        f"现金流量表·{section}",
        abs(delta) < 1,
        f"{_money(inflow)} − {_money(abs(outflow))} = {_money(inflow - abs(outflow))}，"
        f"表列净额 {_money(net)}（{short}）",
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


#: 资产负债表「权益合计」的别名。新准则报表写「所有者权益(或股东权益)合计」，
#: 旧版写「股东权益合计」——指的是同一行，只是叫法不同。
_EQUITY_TOTAL_LABELS = (
    "股东权益合计",
    "所有者权益合计",
    "所有者权益(或股东权益)合计",
    "所有者权益（或股东权益）合计",
)


def _locate_row(
    logical: list[LogicalTable],
    labels: tuple[str, ...] | str,
    parent_id: str | None = None,
) -> tuple[LogicalTable, str] | None:
    """按科目名找第一张含它的表；``parent_id`` 给定时只在该物理表内找。"""
    wanted = {labels} if isinstance(labels, str) else set(labels)
    for table in logical:
        if parent_id is not None and table.parent_id != parent_id:
            continue
        for row in table.rows:
            if row and row[0].strip() in wanted:
                return table, row[0].strip()
    return None


def _money(value: float) -> str:
    """金额显示：有角分就带两位小数，整数就不带——两种口径互不打扰。"""
    return f"{value:,.2f}" if value != int(value) else f"{value:,.0f}"


def _balance_rows(
    logical: list[LogicalTable], labels: tuple[str, ...]
) -> list[tuple[LogicalTable, str, float]]:
    """把文档里所有「资产总计 / 负债合计 / 权益合计」行都收集起来。

    一个文档里这些行往往不止一处：财务报表正文一套，母公司报表一套，还常有
    「资产构成」这种**看起来一样但口径不同**的汇总表（万科的「资产总计 102,062,283.15」
    是万元口径的占比表）。哪一套才是资产负债表，只看行名判断不出来。
    """
    found: list[tuple[LogicalTable, str, float]] = []
    for table in logical:
        for row in table.rows:
            label = row[0].strip() if row else ""
            if label in labels:
                # 取**该行最左边**的金额格：两期并排时左列是本期，且避开了
                # 「附注」列里的 62、61 这种小序号（坑-38）
                money = [c for c in row[1:] if _looks_like_money(c)]
                numeric = [c for c in row[1:] if _parse_number(c) is not None]
                for cell in money or numeric:
                    value = _parse_number(cell)
                    if value is not None:
                        found.append((table, label, float(value.rstrip("%"))))
                    break
    return found


def check_balance_sheet(logical: list[LogicalTable]) -> list[Check]:
    """资产负债表：资产总计 = 负债合计 + 权益合计。

    三项**不要求在同一张逻辑表里**：一张物理表被切成「资产 / 负债 / 权益」三段是
    常见排版（赛力斯 2025 年报的 ``#S02`` / ``#S04`` / ``#S05``）。

    **选哪一套用恒等式自己挑**：把所有候选按三元组枚举，取残差最小的一组，同
    物理表的组合优先。只按行名取第一条会踩到「资产构成表」——万科的勾稽表里
    ``资产总计 102,062,283.15``（万元口径），真报表是 ``1,020,622,831,532.86``。
    恒等式本身就是最好的筛选器：拿错一套三元组，残差会大到离谱。
    """
    assets_rows = _balance_rows(logical, ("资产总计",))
    if not assets_rows:
        return [Check("资产负债表恒等式", False, "未找到资产总计")]
    liability_rows = _balance_rows(logical, ("负债合计",))
    equity_rows = _balance_rows(logical, _EQUITY_TOTAL_LABELS)
    if not liability_rows or not equity_rows:
        return [Check("资产负债表恒等式", False, "未找到负债合计或权益合计")]

    matching: list[tuple[bool, float, tuple, tuple, tuple]] = []
    for asset in assets_rows:
        for liability in liability_rows:
            for equity in equity_rows:
                delta = asset[2] - liability[2] - equity[2]
                tolerance = abs(asset[2]) * 1e-10
                same_parent = (
                    asset[0].parent_id == liability[0].parent_id == equity[0].parent_id
                )
                matching.append(
                    (abs(delta) <= tolerance, abs(delta) / max(abs(asset[2]), 1.0), asset, liability, equity)
                )
    # 合并报表的资产总计一定 ≥ 母公司报表，所以「等式成立且资产最大」的那一组就是
    # 合并报表；等式都不成立时退回残差最小的一组，把差额报出来。
    consistent = [item for item in matching if item[0]]
    if consistent:
        best = max(consistent, key=lambda item: item[2][2])
    else:
        best = min(matching, key=lambda item: item[1])

    _, _, (assets, assets_label, total_assets), (liability_table, liability_label, total_liab), (
        equity_table,
        equity_label,
        total_equity,
    ) = best

    delta = total_assets - total_liab - total_equity
    aliased = "" if equity_label == "股东权益合计" else f"，权益行作「{equity_label}」"
    # 财报本身就是四舍五入后的整数元，三项加起来可能与合计差几块钱；广汽的母公司报表
    # 差 2 元、上年同期那一列却分毫不差，PDF 原文三个数都对得上——是报表自己的事。
    # 容差**只按相对量级**给（一百亿分之一）：2000 亿的报表允许差 21 元，
    # 一亿的报表只允许差 0.01 元——所以真读错一位，小公司一定报错。
    # 大公司里 1 元的读错确实看不出来，这是算术本身的分辨率极限，认了。
    tolerance = abs(total_assets) * 1e-10
    rounded = "" if abs(delta) < 1 else "，视为报表自身四舍五入"
    return [
        Check(
            "资产负债表恒等式",
            abs(delta) <= tolerance,
            f"资产 {_money(total_assets)} vs 负债 {_money(total_liab)}"
            f" + 权益 {_money(total_equity)}（差额 {_money(delta)}{aliased}{rounded}）",
        )
    ]


def _check_fingerprint(parsed_dir: Path) -> Check:
    """产物是否由当前代码生成。

    没有这把尺子，"改了判据但没重跑" 是查不出来的：`_analyze()` 从 `content_list.json`
    现算，永远不读磁盘上的 `logical_tables/*.md`，所以指标看着没变、产物其实全是旧的。
    """
    from finkb.version import pipeline_fingerprint

    report_path = parsed_dir / "report.json"
    if not report_path.exists():
        return Check("解析版本", False, "缺 report.json")
    try:
        recorded = json.loads(report_path.read_text(encoding="utf-8")).get("pipeline_fingerprint")
    except (OSError, json.JSONDecodeError) as exc:
        return Check("解析版本", False, f"report.json 读不出来：{exc}")
    current = pipeline_fingerprint()
    if recorded is None:
        return Check("解析版本", False, f"产物无版本戳（当前 {current}），需重跑")
    if recorded != current:
        return Check("解析版本", False, f"产物版本 {recorded} ≠ 当前 {current}，需重跑")
    return Check("解析版本", True, current)



def _check_row_accounting(parsed_dir: Path) -> Check:
    """每一行都必须有去处：进逻辑表、当段表头、当分节标题。"""
    lost = find_unaccounted_rows(parsed_dir)
    if not lost:
        return Check("行去向", True, "所有原始行都归到了逻辑表、表头或分节标题")
    sample = "；".join(f"{item['table']} r{item['row']}" for item in lost[:3])
    return Check("行去向", False, f"{len(lost)} 行没有去处：{sample}")


def find_unaccounted_rows(parsed_dir: Path) -> list[dict]:
    """找出既没进任何逻辑表、也没被当成分节标题或表头的原始行。

    一行的去向有五种：进了某张逻辑表、成了该段的表头、成了分节标题、
    成了前面某段的表头（多行表头）、或者是重复表头被吸收。五种都不属于
    的行才是真的不见了。
    """
    _, _, stitched, logical = _analyze(parsed_dir)
    by_parent: dict[str, list] = {}
    for table in logical:
        by_parent.setdefault(table.parent_id, []).append(table)

    lost: list[dict] = []
    for table in stitched:
        segments = sorted(by_parent.get(table.parent_id, []), key=lambda t: t.row_range[0])
        covered: set[int] = set()
        end_of_previous = 0
        for segment in segments:
            start, end = segment.row_range
            covered |= set(range(start, end))
            # 段前到上一段结束之间是这一段的表头行（可能是多行）
            covered |= set(range(end_of_previous, start))
            end_of_previous = end
        section = {index for index, _ in table.section_rows}
        for index in sorted(set(range(len(table.rows))) - covered - section):
            cells = tuple(cell.strip() for cell in table.rows[index])
            if sum(1 for cell in cells if cell) <= 1:
                continue
            lost.append(
                {
                    "table": table.parent_id,
                    "row": index,
                    "cells": [cell for cell in cells if cell][:6],
                }
            )
    return lost


def render_row_report(root: Path) -> str:
    lines = ["# 行去向核查", ""]
    total = 0
    for parsed_dir in sorted(root.iterdir()):
        if not parsed_dir.is_dir() or parsed_dir.name.startswith("_"):
            continue
        if not (parsed_dir / "content_list.json").exists():
            continue
        lost = find_unaccounted_rows(parsed_dir)
        total += len(lost)
        lines.append(f"## {parsed_dir.name}：{len(lost)} 行")
        for item in lost[:12]:
            lines.append(f"- `{item['table']}` r{item['row']}：{' | '.join(item['cells'])}")
        lines.append("")
    lines.insert(2, f"全语料 {total} 行既没进逻辑表、也不是分节标题或重复表头。")
    lines.insert(3, "")
    return "\n".join(lines)



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
    checks.append(_check_fingerprint(parsed_dir))
    checks.append(_check_row_accounting(parsed_dir))
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


def render_header_report(parsed_dir: Path) -> str:
    """逐表列出「单行表头 → 多层表头压平」的前后对照。

    「改前」= 第 0 行当表头、其余全部当数据（这正是改动前 ``html_table_to_rows``
    的契约）；「改后」= 多层表头压平成一行、只把真正的数据行留下。两者行数之差
    就是被多行表头吃掉的层数，可以逐张核对。
    """
    payload = json.loads((parsed_dir / "content_list.json").read_text(encoding="utf-8"))
    doc_id = parsed_dir.name

    total = with_header = 0
    multi: list[str] = []
    titles: list[str] = []
    page_ordinal: dict[int, int] = {}

    for block in payload:
        if block.get("type") != "table":
            continue
        body = str(block.get("table_body") or "")
        if not body.strip():
            continue
        total += 1
        page = int(block.get("page_idx", 0)) + 1
        page_ordinal[page] = page_ordinal.get(page, 0) + 1
        layout = html_table_layout(body)
        if layout.table_title:
            titles.append(
                f"- P{page} 表格#{page_ordinal[page]}：`{layout.table_title}`"
                f"（原为首行全宽单格，已抽出为表标题，不再占数据行）"
            )
        if not layout.header:
            continue
        with_header += 1

        if layout.header_layers < 2:
            continue

        # 改前只有第 0 行是表头，其余层全被当成数据行。
        rows_before = len(layout.rows) + layout.header_layers - 1
        multi.append(f"### P{page} 表格#{page_ordinal[page]}")
        multi.append("")
        multi.append(
            f"- 表头层数：{layout.header_layers}　"
            f"数据行数：改前 {rows_before} → 改后 {len(layout.rows)}"
        )
        multi.append(f"- 改前表头（只有第 0 行）：`{layout.header_rows[0]}`")
        for extra in layout.header_rows[1:]:
            multi.append(f"- 改前误当数据行：`{extra}`")
        multi.append(f"- **改后表头**：`{layout.header}`")
        multi.append("- 原始 HTML：")
        multi.append("")
        multi.append("```html")
        multi.append(body[:400] + ("…" if len(body) > 400 else ""))
        multi.append("```")
        multi.append("")
        multi.append("- [ ] 我核对过 PDF 这一处")
        multi.append("")

    lines = [
        f"# 多层表头对照：{doc_id}",
        "",
        f"- 有表头的物理表：{with_header} 张（共解析 {total} 张非空表）",
        f"- **识别出多层表头的：{sum(1 for line in multi if line.startswith('### '))} 张**",
        f"- 抽出全宽表标题的：{len(titles)} 张",
        "",
        "> 判据（全部是 HTML 结构事实，不含任何「这份 PDF 数出来的」阈值）：",
        "> R1 当前层有 `colspan>1` 且不是单格跨满全宽（那是表标题）；",
        "> R2 下一层的自有列 ⊆ 本层分组覆盖的列；R3 下一层自有格无纯数值。",
        "",
        "## 一、多层表头压平",
        "",
    ]
    if multi:
        lines.extend(multi)
    else:
        lines.append("没有识别出多层表头。")
        lines.append("")
    lines.append("## 二、抽出的表标题")
    lines.append("")
    lines.extend(titles or ["（无）"])
    lines.append("")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 8d. 页码区间（跨页表锚点回填）
# ──────────────────────────────────────────────────────────────
#: 全角转半角——MinerU 的单元格用半角冒号，PDF 原文用全角，不统一就对不上。
_HALFWIDTH = str.maketrans("：（）％，；", ":()%,;")


def _normalize(text: str) -> str:
    """去掉所有空白并把全角标点转半角，用于跨来源文本比对。"""
    return "".join(text.split()).translate(_HALFWIDTH)


@dataclass
class PageSpan:
    """一张跨页物理表的页码区间证据。"""

    table_id: str
    page_start: int
    page_end: int
    row_count: int
    logical_count: int
    placeholder_pages: list[int]
    total: int
    hits_in: list[int]
    hits_outside: int
    misses: int

    @property
    def verdict(self) -> str:
        """把三类命中情况压成一句话结论。"""
        if not self.total:
            return "无样本"
        if self.hits_in and not self.hits_outside:
            return "✓ 区间内命中"
        if self.hits_in:
            return f"✓ 区间内命中；另有 {self.hits_outside} 行也出现在末页+1"
        if self.hits_outside:
            return f"⚠ 仅出现在末页+1（{self.hits_outside} 行）"
        return "✗ 无法定位"


def _placeholder_pages(content_list: list[dict]) -> dict[int, list[int]]:
    """锚点页 → 该表在后续页留下的空占位块页号。"""
    out: dict[int, list[int]] = {}
    anchor_page: int | None = None
    seen_on_page: dict[int, int] = {}
    for item in content_list:
        page_no = int(item.get("page_idx", 0)) + 1
        if str(item.get("type", "text")) != "table":
            continue
        body = str(item.get("table_body") or item.get("html") or "")
        if body.strip():
            anchor_page = page_no
            seen_on_page[page_no] = seen_on_page.get(page_no, 0) + 1
            continue
        if anchor_page is None or seen_on_page.get(page_no):
            continue
        out.setdefault(anchor_page, []).append(page_no)
    return out


def _sampled_rows(table: StitchedTable, limit: int = 6) -> list[str]:
    """挑几行有辨识度的文本，用来在 PDF 里反查。"""
    texts: list[str] = []
    for row in table.rows:
        for cell in row:
            text = cell.strip()
            if len(text) >= 8:
                texts.append(text)
                break
    if not texts:
        return []
    step = max(1, len(texts) // limit)
    return texts[::step][:limit]


def _pdf_page_texts(pdf_path: Path) -> list[str] | None:
    """逐页取文本并归一化；两个库都没有就返回 None。

    优先 PyMuPDF：pypdf 对部分年报取不全表格文字（000625 半年报第 5 页「主要财务
    指标」整表取不到），会把「数值回查」误报成解析错误。
    """
    try:
        import pymupdf
    except ImportError:
        pymupdf = None
    if pymupdf is not None:
        with pymupdf.open(str(pdf_path)) as document:
            return [_normalize(page.get_text()) for page in document]
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    reader = PdfReader(str(pdf_path))
    return [_normalize(page.extract_text() or "") for page in reader.pages]


def _scanned_pages(pdf_path: Path) -> set[int]:
    """返回「整页是扫描图片、没有文字层」的页码（1 基）。

    这类页面的数字只能靠 OCR 识别，无法用 PDF 文字层回查；调用方应把它们
    标成 OCR 并提示人工审查，而不是当成「查不到」。
    """
    try:
        import pymupdf
    except ImportError:
        pymupdf = None
    if pymupdf is not None:
        with pymupdf.open(str(pdf_path)) as document:
            return {
                number
                for number, page in enumerate(document, 1)
                if not page.get_text().strip() and page.get_images()
            }
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - 依赖缺失时不做 OCR 判定
        return set()
    reader = PdfReader(str(pdf_path))
    scanned: set[int] = set()
    for number, page in enumerate(reader.pages, 1):
        resources = page.get("/Resources") or {}
        xobjects = resources.get("/XObject") or {}
        has_image = any(
            ref.get_object().get("/Subtype") == "/Image" for ref in xobjects.values()
        )
        if has_image and not (page.extract_text() or "").strip():
            scanned.add(number)
    return scanned


MIN_VERIFY_DIGITS = 6


def _digit_signature(cell: str) -> str | None:
    """单元格里数字的「纯数字指纹」，用来回查 PDF 文本。

    只取位数够长的数字：位数太短的（年份、个位数）在整页文本里到处都能命中，
    查了也没有信息量。
    """
    if _parse_number(cell) is None:
        return None
    digits = re.sub(r"\D", "", cell)
    return digits if len(digits) >= MIN_VERIFY_DIGITS else None


def verify_cells(
    parsed_dir: Path,
    pdf_path: Path,
    sample_per_table: int = 12,
) -> list[dict]:
    """逐表抽样，把数值回查到 PDF 原文（返回明细、最佳偏移、各偏移命中数）。

    这是唯一能从**外部**证明「单元格没被解析错、页码区间没错」的检查：
    数字必须在它声称的那几页里出现。查不到就是真问题——页码错了，或者值被改了。

    扫描页（整页图片、无文字层）上的表无法用文字层回查：这类表不抽数字，
    结果里 ``ocr=True``、``ocr_pages`` 给出扫描页，由报告提示「数字来自 OCR，
    建议人工审查」。
    """
    _, _, _, logical = _analyze(parsed_dir)
    pages = _pdf_page_texts(pdf_path)
    if pages is None:
        raise RuntimeError("需要 pypdf 才能回查 PDF 文本")
    scanned = _scanned_pages(pdf_path)

    results: list[dict] = []
    offsets = {shift: [0, 0] for shift in (-1, 0, 1)}
    for table in logical:
        span_pages = list(range(table.page_start, table.page_end + 1))
        ocr_pages = [page for page in span_pages if page in scanned]
        verifiable = [page for page in span_pages if page not in scanned]
        if not verifiable:
            results.append(
                {
                    "logical_id": table.logical_id,
                    "kind": table.kind,
                    "page_start": table.page_start,
                    "page_end": table.page_end,
                    "checked": 0,
                    "missing": [],
                    "ocr": True,
                    "ocr_pages": ocr_pages,
                }
            )
            continue
        span = [pages[page - 1] for page in verifiable]
        haystack = "".join(re.sub(r"\D", "", text) for text in span)
        signatures = []
        for row in table.rows:
            for cell in row:
                signature = _digit_signature(cell)
                if signature is not None and signature not in signatures:
                    signatures.append(signature)
        if not signatures:
            continue
        step = max(1, len(signatures) // sample_per_table)
        sample = signatures[::step][:sample_per_table]
        missing = [sig for sig in sample if sig not in haystack]
        for shift, tally in offsets.items():
            window = "".join(
                re.sub(r"\D", "", text)
                for text in pages[
                    max(0, table.page_start - 1 + shift) : table.page_end + shift
                ]
            )
            for sig in sample:
                tally[0] += 1
                if sig in window:
                    tally[1] += 1
        results.append(
            {
                "logical_id": table.logical_id,
                "kind": table.kind,
                "page_start": table.page_start,
                "page_end": table.page_end,
                "checked": len(sample),
                "missing": missing,
                "ocr": False,
                "ocr_pages": ocr_pages,
            }
        )
    best_shift = max(offsets, key=lambda shift: offsets[shift][1] / max(offsets[shift][0], 1))
    return results, best_shift, offsets


def collect_page_spans(parsed_dir: Path, pdf_path: Path | None = None) -> list[PageSpan]:
    """找出跨页物理表，并收集「表续到哪一页」的证据。"""
    _, _, stitched, logical = _analyze(parsed_dir)
    content_list = json.loads(
        (parsed_dir / "content_list.json").read_text(encoding="utf-8")
    )
    placeholders = _placeholder_pages(content_list)

    counts: dict[str, int] = {}
    for table in logical:
        parent = table.logical_id.split("#S")[0]
        counts[parent] = counts.get(parent, 0) + 1

    pdf_pages = _pdf_page_texts(pdf_path) if pdf_path is not None else None
    spans: list[PageSpan] = []
    for table in stitched:
        if table.page_start == table.page_end:
            continue
        samples = [_normalize(text) for text in _sampled_rows(table)]
        hits_in: list[int] = []
        hits_outside = 0
        misses = 0
        for sample in samples:
            inside = [
                page_no
                for page_no in range(table.page_start, table.page_end + 1)
                if page_no <= len(pdf_pages or []) and sample in pdf_pages[page_no - 1]
            ]
            if inside:
                hits_in.append(inside[0])
                continue
            beyond = table.page_end + 1
            if pdf_pages is not None and beyond <= len(pdf_pages) and sample in pdf_pages[beyond - 1]:
                hits_outside += 1
            else:
                misses += 1
        spans.append(
            PageSpan(
                table_id=table.parent_id,
                page_start=table.page_start,
                page_end=table.page_end,
                row_count=len(table.rows),
                logical_count=counts.get(table.parent_id, 1),
                placeholder_pages=placeholders.get(table.page_start, []),
                total=len(samples),
                hits_in=hits_in,
                hits_outside=hits_outside,
                misses=misses,
            )
        )
    return spans


CHECKBOX = "- [ ]"


def render_page_span_report(parsed_dir: Path, pdf_path: Path | None = None) -> str:
    """页码区间核验报告：逐张列出跨页表的区间与证据。"""
    spans = collect_page_spans(parsed_dir, pdf_path)
    lines: list[str] = [
        f"# 页码区间核验：{parsed_dir.name}",
        "",
        f"- 生成时间：`{datetime.now().isoformat(timespec='seconds')}`",
        f"- PDF：`{pdf_path.name if pdf_path else '（未提供，跳过文本反查）'}`",
        f"- 跨页物理表：**{len(spans)} 张**",
        "",
        "## 一览",
        "",
        "| 表号 | 页码区间 | 行数 | 逻辑表 | 占位块页 | PDF 反查 | 结论 |",
        "|---|---|---|---|---|---|---|",
    ]
    for span in spans:
        placeholders = "、".join(f"P{p}" for p in span.placeholder_pages) or "（无）"
        if span.total:
            check = f"{len(span.hits_in)}/{span.total} 行落在区间内"
            if span.misses:
                check += f"，{span.misses} 行无法定位"
        else:
            check = "无样本"
        lines.append(
            f"| `{span.table_id}` | P{span.page_start}–P{span.page_end} "
            f"| {span.row_count} | {span.logical_count} | {placeholders} "
            f"| {check} | {span.verdict} |"
        )
    lines.append("")

    lines += [
        "## 怎么读这张表",
        "",
        "- **占位块页** = MinerU 在该页留下一个空的 table 块。这是「表续到了这里」的原始证据，"
        "本次修复就是把它回填成 `page_end`。",
        "- **PDF 反查** = 从表里抽最多 6 行文本，去 PDF 对应页里找。找不到只说明"
        "「PDF 的文本流顺序和表格行不一致」，**不代表区间错**（P72-P73 就是全角冒号导致的对不上）。",
        "- **末页+1 也出现同源文本** 不是错误信号：P113-P114 与 P115 是两张不同的表"
        "（中介机构情况 / 募集资金使用情况），键列都是同一批债券名称，自然重复。",
        "",
        "## 逐张核对",
        "",
        "> 只需要核对下面的勾选项。区间正确与否，看的是「PDF 里这张表是否从起始页开始、"
        "在末页结束、末页+1 不再有表体」，不是看反查命中率。",
        "",
    ]
    for span in spans:
        lines += [
            f"### `{span.table_id}` — P{span.page_start}–P{span.page_end}"
            f"（{span.row_count} 行，{span.logical_count} 张逻辑表）",
            "",
            f"{CHECKBOX} PDF 第 {span.page_start} 页有这张表的表体",
            f"{CHECKBOX} PDF 第 {span.page_end} 页仍有这张表的表体",
            f"{CHECKBOX} PDF 第 {span.page_end + 1} 页没有这张表的**续表内容**",
            "",
        ]
    return "\n".join(lines)


def _cmd_verify_cells(args: argparse.Namespace) -> int:
    """把每个逻辑表的抽样数值回查到 PDF 文本。"""
    results, best_shift, offsets = verify_cells(Path(args.doc), Path(args.pdf))
    verifiable = [item for item in results if not item["ocr"]]
    ocr_tables = [item for item in results if item["ocr"]]
    total = sum(item["checked"] for item in verifiable)
    miss = sum(len(item["missing"]) for item in verifiable)
    lines = [
        f"# 数值回查：{Path(args.doc).name}",
        "",
        f"- 参与核对的表：**{len(verifiable)}** 张",
        f"- 抽样数值：**{total}** 个，查不到 **{miss}** 个"
        f"（命中率 **{(total - miss) * 100 // max(total, 1)}%**）",
    ]
    if ocr_tables:
        lines.append(
            f"- 扫描页上的表（无文字层，数字来自 OCR）：**{len(ocr_tables)}** 张，"
            "未计入回查，**建议人工审查**（见文末）"
        )
    lines += [
        "",
        "**页码偏移自检**（把每张表的检索窗口整体平移 ±1 页再查一遍）："
        + "、".join(
            f"{shift:+d} 页 {tally[1] * 100 // max(tally[0], 1)}%"
            for shift, tally in sorted(offsets.items())
        )
        + f"；最佳偏移 **{best_shift:+d}**。"
        + (
            "偏移非 0 说明本文件的表页码与 PDF 物理页存在系统性错位。"
            if best_shift != 0
            else "偏移 0 最佳，页码与 PDF 物理页一致。"
        ),
        "",
        "## 有缺失的表",
        "",
        "| 表号 | 类型 | 页码 | 抽检 | 缺失 | 缺失的数字 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in sorted(verifiable, key=lambda r: -len(r["missing"])):
        if not item["missing"]:
            continue
        short = item["logical_id"].split("#", 1)[1]
        pages = f"{item['page_start']}"
        if item["page_end"] != item["page_start"]:
            pages = f"{item['page_start']}-{item['page_end']}"
        if item["ocr_pages"]:
            pages += "（含扫描页）"
        shown = "、".join(item["missing"][:4])
        lines.append(
            f"| `{short}` | {item['kind']} | {pages} | {item['checked']} | "
            f"{len(item['missing'])} | {shown} |"
        )
    if miss == 0:
        lines.append("| —— | —— | —— | —— | 0 | 全部命中 |")
    if ocr_tables:
        lines += [
            "",
            "## 扫描页（OCR，建议人工审查）",
            "",
            "> 以下页面的 PDF 只有图片、没有文字层，表格数字是 **OCR 识别**出来的，"
            "无法用 PDF 文字层核对。**请对照原文图片自行审查这些数字。**",
            "",
            "| 表号 | 类型 | 页码 | 抽检数字 |",
            "| --- | --- | --- | --- |",
        ]
        for item in ocr_tables:
            short = item["logical_id"].split("#", 1)[1]
            pages = f"{item['page_start']}"
            if item["page_end"] != item["page_start"]:
                pages = f"{item['page_start']}-{item['page_end']}"
            lines.append(f"| `{short}` | {item['kind']} | {pages} | 0（OCR 页） |")
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"写入 {args.out}")
    else:
        print(text)
    return 0 if miss == 0 else 4


def _cmd_page_spans(args: argparse.Namespace) -> int:
    pdf_path = Path(args.pdf).expanduser().resolve() if args.pdf else None
    report = render_page_span_report(Path(args.doc).expanduser().resolve(), pdf_path)
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"页码区间核验已写入 {out}")
    else:
        print(report)
    return 0


def _cmd_headers(args: argparse.Namespace) -> int:
    report = render_header_report(Path(args.doc).expanduser().resolve())
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"表头对照已写入 {out}")
    else:
        print(report)
    return 0


def _cmd_numbers(args: argparse.Namespace) -> int:
    parsed_dir = Path(args.doc)
    _, _, _, logical = _analyze(parsed_dir)
    found = find_malformed_numbers(logical)
    lines = [f"# 数字格式异常：{parsed_dir.name}", ""]
    if not found:
        lines.append("没有发现千分位分组异常的数字。")
    else:
        lines += [f"共 {len(found)} 处。", "",
                  "| 表号 | 行 | 单元格 |", "| --- | --- | --- |"]
        lines += [f"| `{tid}` | {row} | `{cell}` |" for tid, row, cell in found]
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0 if not found else 4


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


_RECALL_NUMBER_RE = re.compile(r"\d{6,}(?:\.\d+)?")
_SEPARATOR_RE = re.compile(r"[,，\s]")


def _page_haystacks(pages, stitched) -> dict[int, str]:
    """每页可用的数字串：覆盖该页的表格 + 该页的文本块，去掉千分位与空白。

    拼接后直接做**子串**比对。PDF 抽取常把一个数字拆到两行、或把两个数字粘成一串，
    用正则会两边都判错；子串比对对这两种失真都免疫。
    """
    hay: dict[int, str] = {}
    for table in stitched:
        text = _SEPARATOR_RE.sub("", "".join(cell for row in table.rows for cell in row))
        for page in range(table.page_start, table.page_end + 1):
            hay[page] = hay.get(page, "") + text
    for page in pages:
        text = _SEPARATOR_RE.sub("", "".join(block.text for block in page.blocks))
        hay[page.number] = hay.get(page.number, "") + text
    return hay


def check_recall(parsed_dir: Path, pdf_path: Path) -> dict:
    """PDF 原文里的每个数字，是否都能在解析结果里找到（召回率）。"""
    _, pages, stitched, _ = _analyze(parsed_dir)
    texts = _pdf_page_texts(pdf_path)
    if texts is None:
        raise SystemExit("需要 pymupdf 或 pypdf 才能读 PDF 原文")
    hay = _page_haystacks(pages, stitched)
    whole = "".join(hay.values())
    total = covered = elsewhere = 0
    misses: list[tuple[int, list[str]]] = []
    for index, text in enumerate(texts, start=1):
        numbers = set(_RECALL_NUMBER_RE.findall(_SEPARATOR_RE.sub("", text)))
        page_hay = hay.get(index, "")
        missing = sorted(number for number in numbers if number not in page_hay)
        total += len(numbers)
        covered += len(numbers) - len(missing)
        elsewhere += sum(1 for number in missing if number in whole)
        if missing:
            misses.append((index, missing))
    return {
        "doc_id": parsed_dir.name,
        "total": total,
        "covered": covered,
        "elsewhere": elsewhere,
        "misses": misses,
    }


def render_recall_report(result: dict) -> str:
    """把召回率结果渲染成 Markdown。"""
    total = result["total"]
    covered = result["covered"]
    misses = result["misses"]
    lines = [
        f"# 原文召回：{result['doc_id']}",
        "",
        f"- PDF 原文数字：**{total}** 个",
        f"- 页内能对上：**{covered}** 个（**{covered * 100 // max(total, 1)}%**）",
        f"- 页内对不上但**全文档能找到**：**{result['elsewhere']}** 个（归属页不对，不是丢失）",
        f"- 真正找不到：**{total - covered - result['elsewhere']}** 个",
        f"- 有缺口的页：**{len(misses)}** 页",
        "",
        "> 比对口径：数字去掉千分位与空白后做**子串**比对，对「PDF 抽取把数字拆行 /",
        "> 粘串」这两种失真都免疫。表按 `page_start..page_end` 覆盖页归属。",
        "",
        "## 对不上的页",
        "",
        "| 页 | 个数 | 样例 |",
        "| --- | --- | --- |",
    ]
    for page, missing in misses:
        lines.append(f"| {page} | {len(missing)} | {'、'.join(missing[:4])} |")
    if not misses:
        lines.append("| — | 0 | 全部对上 |")
    return "\n".join(lines) + "\n"


def _cmd_recall(args: argparse.Namespace) -> int:
    """统计原文数字被解析结果覆盖的比例。"""
    result = check_recall(Path(args.doc), Path(args.pdf))
    report = render_recall_report(result)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"报告已写入 {out}")
    else:
        print(report)
    return 0 if result["covered"] == result["total"] else 4


BASELINE_SCHEMA = 1


def collect_corpus(root: Path, pdf_root: Path | None = None) -> list[dict]:
    """把整份语料过一遍，返回每份文档的指标行。

    给了 ``pdf_root`` 就顺带做数值回查——那是唯一从**外部**（PDF 原文）证明
    「数字没被解析错、页码没算错」的检查，代价是每份多读一次 PDF（本地，不耗额度）。
    """
    rows = []
    for doc in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        if not (doc / "content_list.json").exists():
            continue
        snapshot, pages, _, logical = _analyze(doc)
        quality: dict = {}
        # 优先用解析时写进 report.json 的 quality（快）；老产物没有该字段时现场统计。
        report_path = doc / "report.json"
        if report_path.exists():
            try:
                quality = json.loads(report_path.read_text(encoding="utf-8")).get("quality") or {}
            except Exception:
                quality = {}
        if not quality:
            content_list_path = doc / "content_list.json"
            if content_list_path.exists():
                try:
                    from finkb.parser.quality import analyze_quality

                    quality = analyze_quality(
                        json.loads(content_list_path.read_text(encoding="utf-8"))
                    )
                except Exception:  # 质量体检失败不影响主检查
                    quality = {}
        checks = check_invariants(doc)
        failed = [check for check in checks if not check.ok]
        with_header = sum(1 for table in logical if table.header)
        checked = missing = 0
        shift = 0
        ocr_tables = ocr_pages = 0
        if pdf_root is not None:
            pdf = pdf_root / f"{doc.name}.pdf"
            if pdf.exists():
                results, shift, _ = verify_cells(doc, pdf)
                checked = sum(item["checked"] for item in results)
                missing = sum(len(item["missing"]) for item in results)
                ocr_tables = sum(1 for item in results if item["ocr"])
                ocr_pages = len(
                    {
                        page
                        for item in results
                        if item["ocr"]
                        for page in item["ocr_pages"]
                    }
                )
        rows.append(
            {
                "doc": doc.name,
                "verify_checked": checked,
                "verify_missing": missing,
                "verify_shift": shift,
                "ocr_tables": ocr_tables,
                "ocr_pages": ocr_pages,
                "pages": len(pages),
                "logical": len(logical),
                "header": with_header,
                "bare": len(logical) - with_header,
                "invariants": len(checks) - len(failed),
                "total_invariants": len(checks),
                "numbers": len(find_malformed_numbers(logical)),
                "empty_tables": int(quality.get("empty_table_count") or 0),
                "chart_pages": len(quality.get("chart_pages") or []),
                "image_only_pages": len(quality.get("image_only_pages") or []),
                "failures": [f"{check.name}：{check.detail}" for check in failed],
            }
        )
    return rows


def compare_with_baseline(rows: list[dict], baseline: dict) -> list[str]:
    """对照基线找退步，返回人话说明的列表（空列表表示没有退步）。

    只报**变差**：新增文档、表头变多、表数因判据改进而变化都不算。表数变化单独提
    一句是为了让人看见「这个改动动了结构」，不一定是退步。
    """
    saved = baseline.get("docs", {})
    current = {row["doc"]: row for row in rows}
    problems: list[str] = []
    for doc, old in saved.items():
        row = current.get(doc)
        if row is None:
            problems.append(f"`{doc}` 从语料里消失了")
            continue
        if row["header"] < old["header"]:
            problems.append(
                f"`{doc}` 有表头 {old['header']} → {row['header']}（退步 {old['header'] - row['header']} 张）"
            )
        if row["invariants"] < old["invariants"]:
            problems.append(
                f"`{doc}` 守恒律通过 {old['invariants']} → {row['invariants']}"
            )
        if row.get("verify_missing", 0) > old.get("verify_missing", 0):
            problems.append(
                f"`{doc}` 数值回查查不到 {old.get('verify_missing', 0)} → "
                f"{row['verify_missing']} 个"
            )
    return problems


def write_baseline(rows: list[dict], target: Path) -> None:
    """把当前指标存成基线文件（进 git，供下一版对照）。"""
    payload = {
        "schema": BASELINE_SCHEMA,
        "docs": {
            row["doc"]: {
                "pages": row["pages"],
                "logical": row["logical"],
                "header": row["header"],
                "invariants": row["invariants"],
                "total_invariants": row["total_invariants"],
                "verify_checked": row.get("verify_checked", 0),
                "verify_missing": row.get("verify_missing", 0),
            }
            for row in rows
        },
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _cmd_baseline(args: argparse.Namespace) -> int:
    """写入或展示语料基线。"""
    rows = collect_corpus(Path(args.root), Path(args.pdf_root) if args.pdf_root else None)
    target = Path(args.out)
    write_baseline(rows, target)
    print(f"基线已写入 {target}（{len(rows)} 份）")
    return 0


def _cmd_corpus(args: argparse.Namespace) -> int:
    """把整份语料过一遍：守恒律、数字格式异常、表头覆盖率，合成一张表。

    每改一版解析代码就跑一次，比逐份点命令快得多；哪一份退步一眼能看见。
    """
    rows = collect_corpus(Path(args.root), Path(args.pdf_root) if args.pdf_root else None)
    problems: list[str] = []
    if args.baseline:
        path = Path(args.baseline)
        if path.exists():
            problems = compare_with_baseline(rows, json.loads(path.read_text(encoding="utf-8")))
        else:
            print(f"（基线 {path} 不存在，跳过对照）")

    total_tables = sum(row["logical"] for row in rows)
    total_header = sum(row["header"] for row in rows)
    lines = [
        "# 语料体检",
        "",
        (
            f"共 {len(rows)} 份、{total_tables} 张逻辑表，"
            f"有表头 {total_header}（{total_header / total_tables:.1%}）"
            if total_tables
            else "# 语料体检\n\n（没有找到已解析的文档）"
        ),
        "",
        "| 文档 | 页 | 逻辑表 | 有表头 | 裸表 | 守恒律 | 数字异常 | 扫描页表(OCR) | 空表 | 图表页 | 纯图片页 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| `{row['doc']}` | {row['pages']} | {row['logical']} | {row['header']} | "
            f"{row['bare']} | {row['invariants']}/{row['total_invariants']} | {row['numbers']} | "
            f"{row.get('ocr_tables', 0)} | {row.get('empty_tables', 0)} | "
            f"{row.get('chart_pages', 0)} | {row.get('image_only_pages', 0)} |"
        )
    broken = [row for row in rows if row["failures"]]
    if broken:
        lines += ["", "## 未通过的守恒律", ""]
        for row in broken:
            for failure in row["failures"]:
                lines.append(f"- `{row['doc']}` — {failure}")
    ocr_docs = [row for row in rows if row.get("ocr_tables")]
    if ocr_docs:
        lines += [
            "",
            "## 扫描页（OCR，建议自行审查）",
            "",
            "> 以下文档的部分表格页在 PDF 里只有图片、没有文字层，表格数字由 **OCR 识别**，"
            "无法用 PDF 文字层回查，**未计入数值回查**。建议对照原文图片人工审查。",
            "",
        ]
        for row in ocr_docs:
            lines.append(
                f"- `{row['doc']}` — {row['ocr_tables']} 张表、{row['ocr_pages']} 个扫描页"
            )
    quality_rows = [
        row
        for row in rows
        if row.get("empty_tables") or row.get("chart_pages") or row.get("image_only_pages")
    ]
    if quality_rows:
        lines += [
            "",
            "## 解析质量（缺表/缺图/缺页，建议人工核对）",
            "",
            "> 空表 = MinerU 返回了 table 块但没有表体；图表页 = chart 块无文字；"
            "纯图片页 = 整页只有图片。这些内容没有进入知识库，详见各文档 `report.json` 的 `quality` 字段。",
            "",
        ]
        for row in quality_rows:
            lines.append(
                f"- `{row['doc']}` — 空表 {row.get('empty_tables', 0)} 张、"
                f"图表页 {row.get('chart_pages', 0)} 页、纯图片页 {row.get('image_only_pages', 0)} 页"
            )
    if problems:
        lines += ["", "## 相对基线退步", ""] + [f"- {item}" for item in problems]
    text = "\n".join(lines) + "\n"
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    print(text)
    return 0 if not broken and not problems else 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="审计与核验：快照 / 对照 / 守恒律 / 人工核验清单",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cells = sub.add_parser("verify-cells", help="数值回查 PDF 原文")
    cells.add_argument("--doc", required=True)
    cells.add_argument("--pdf", required=True)
    cells.add_argument("--out")
    cells.set_defaults(func=_cmd_verify_cells)

    recall = sub.add_parser("recall", help="原文数字召回：PDF 里的每个数字能否在解析结果里找到")
    recall.add_argument("--doc", required=True)
    recall.add_argument("--pdf", required=True)
    recall.add_argument("--out")
    recall.set_defaults(func=_cmd_recall)

    spans = sub.add_parser("page-spans", help="跨页表页码区间核验")
    spans.add_argument("--doc", required=True, help="已解析目录")
    spans.add_argument("--pdf", help="年报 PDF，给出则做文本反查")
    spans.add_argument("--out", help="报告输出路径（.md），省略则打印到标准输出")
    spans.set_defaults(func=_cmd_page_spans)

    corpus = sub.add_parser("corpus", help="整份语料的体检表")
    corpus.add_argument("--root", default="data/parsed")
    corpus.add_argument("--out")
    corpus.add_argument("--baseline", help="对照基线 JSON；退步时退出码 4")
    corpus.add_argument("--pdf-root", help="PDF 目录；给了就顺带做数值回查")
    corpus.set_defaults(func=_cmd_corpus)

    baseline = sub.add_parser("baseline", help="写入语料基线（供以后对照退步）")
    baseline.add_argument("--root", default="data/parsed")
    baseline.add_argument("--out", default="config/corpus-baseline.json")
    baseline.add_argument("--pdf-root", help="PDF 目录；给了就把数值回查结果一起写进基线")
    baseline.set_defaults(func=_cmd_baseline)
    numbers = sub.add_parser("numbers", help="数字格式异常（千分位粘连、小数逗号）")
    numbers.add_argument("--doc", required=True)
    numbers.add_argument("--out")
    numbers.set_defaults(func=_cmd_numbers)
    rowspan = sub.add_parser("rowspan", help="rowspan 展开前后逐行对照")
    rowspan.add_argument("--doc", required=True, help="已解析目录，如 data/parsed/002594_2025_年报")
    rowspan.add_argument("--out", help="报告输出路径（.md），省略则打印到标准输出")
    rowspan.set_defaults(func=_cmd_rowspan)

    headers = sub.add_parser("headers", help="多层表头压平前后对照")
    headers.add_argument("--doc", required=True)
    headers.add_argument("--out")
    headers.set_defaults(func=_cmd_headers)

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

