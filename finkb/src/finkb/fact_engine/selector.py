"""为 L1 抽取挑表：从一份已解析文档里选出「财务三表 + 主要会计数据」。

为什么不能按章节标题挑：实测同一份文档里章节字段极不稳定——有的写成
「二、财务报表」，有的被单位说明行覆盖成「（除特别注明外，金额单位均为人民币元）」，
同一页上还会随块变化。所以这里以**表格自身的内容**（行标签、行数）为准，
章节只作为辅助信号。

另一个容易踩的点：一张物理表会被切成多张逻辑表（资产负债表常被切成 5 段），
所以判类与挑选都在**父表**层面做，选中后再整组输出它的逻辑子表——否则
「资产总计」和「负债合计」会落在不同子表里，只挑一张就丢半个报表。

挑选规则（v1，只取合并报表）：

- 主要会计数据：章节含「主要会计数据」，且行里有「营业收入」；
- 资产负债表：从第一张含「资产总计」的父表开始，取到第一张含「所有者权益合计」
  或「负债合计」的父表为止（中间的表全部保留）；
- 利润表：含「营业收入/营业总收入」与「净利润」、行数 ≥20，取行数最多的一张；
- 现金流量表：含「经营活动产生的现金流量净额」、没有营业收入、行数 ≥8，
  排除「补充资料」，取行数最多的一张。

母公司报表 v1 不抽：跨公司对比用的是合并口径，母公司表只会制造同键冲突。
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

FINKB_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = FINKB_ROOT.parent

BS_ANCHORS = (
    "资产总计",
    "资产合计",
    "负债合计",
    "所有者权益合计",
    "股东权益合计",
    "流动资产合计",
    "流动负债合计",
    "非流动资产合计",
    "非流动负债合计",
    "少数股东权益",
    "归属于母公司所有者权益合计",
)

_NORM = re.compile(r"[\s\u3000（）()：:，,]")


def norm_label(text: object) -> str:
    """行标签归一：去空白、去中英文括号与冒号逗号，便于包含匹配。"""
    return _NORM.sub("", str(text or ""))


_HEADER_PERIOD = re.compile(r"(19|20)\d{2}\s*年|本报告期|年初至|期末|上年同期|本期")
_NUMBER_ONLY = re.compile(r"[\d,.\-()（）%]+")
# 「经营活动产生的现金流量净额」有各种写法：产生/(使用)的、产生的、产生（使用）的……
_CF_NET = re.compile(r"经营活动.*现金流量净额")


def split_header_blocks(table: SourceTable) -> list[SourceTable]:
    """拆开「一张表里又嵌了一段表头」的情况。

    银行的主要会计数据常把「经营业绩（1–6 月）」和「规模指标（期末余额）」拼在
    同一张表里，第二段自带表头行。只认第一个表头会把 2024-12-31 的余额当成
    2024 年 1–6 月，单位对、期间错，逐字反查也发现不了。
    """
    blocks: list[SourceTable] = []
    header = list(table.header)
    rows: list[list[str]] = []

    def flush() -> None:
        if rows:
            blocks.append(replace(table, header=list(header), rows=list(rows)))
            rows.clear()

    for row in table.rows:
        cells = [str(c).strip() for c in row]
        hits = sum(1 for c in cells[1:] if c and _HEADER_PERIOD.search(c))
        numeric = any(_NUMBER_ONLY.fullmatch(c) for c in cells[1:] if c)
        if hits >= 2 and not numeric:
            flush()
            header = cells
            continue
        rows.append(row)
    flush()
    return blocks or [table]


@dataclass
class SourceTable:
    """一张逻辑表（已展开成纯文本网格）；``children`` 只在父表上非空。"""

    doc_id: str
    company: str
    company_name: str
    period: str
    report_date: str
    logical_id: str
    page: int
    section: str
    header: list[str]
    rows: list[list[str]]
    children: list["SourceTable"] = field(default_factory=list)

    @property
    def labels(self) -> str:
        return "".join(norm_label(r[0]) for r in self.rows if r)

    @property
    def kind(self) -> str:
        joined = self.labels
        has_revenue = "营业收入" in joined or "营业总收入" in joined
        if "主要会计数据" in self.section:
            return "summary"
        if sum(1 for a in BS_ANCHORS if a in joined) >= 3 and len(self.rows) >= 15:
            return "bs"
        if has_revenue and "净利润" in joined and len(self.rows) >= 20:
            return "is"
        # 利润表/现金流量表在部分文档里章节被写成「二、财务报表」甚至单位说明，
        # 所以现金流优先看内容：有经营净额、没有营业收入、行数够。
        # 净额行的写法不固定（美的写成「经营活动产生/(使用)的现金流量净额」），用正则认。
        if (
            _CF_NET.search(joined)
            and not has_revenue
            and len(self.rows) >= 8
            and "补充资料" not in self.section
        ):
            return "cf"
        return "other"


def doc_period(doc_id: str) -> str:
    """``002594_2025_年报`` → ``2025``；半年报 → ``2025H1``；一季报 → ``2025Q1``。"""
    _, year, kind = doc_id.split("_", 2)
    suffix = {"年报": "", "半年报": "H1", "一季报": "Q1", "三季报": "Q3"}[kind]
    return f"{year}{suffix}"


def load_manifest(path: Path | None = None) -> dict[tuple[str, str, str], dict]:
    """读 ``raw/manifest.csv``，键是 (股票代码, 会计年度, 报告类型)。"""
    p = path or (REPO_ROOT / "raw" / "manifest.csv")
    out: dict[tuple[str, str, str], dict] = {}
    if not p.exists():
        return out
    with p.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            out[(row["股票代码"], row["会计年度"], row["报告类型"])] = row
    return out


def load_tables(doc_dir: Path, manifest: dict | None = None) -> list[SourceTable]:
    """把一份文档的逻辑表按父表分组：返回父表列表，父表的 ``children`` 是逻辑子表。"""
    doc_id = doc_dir.name
    code, year, kind = doc_id.split("_", 2)
    manifest = manifest if manifest is not None else load_manifest()
    meta = manifest.get((code, year, kind), {})
    report = json.loads((doc_dir / "report.json").read_text(encoding="utf-8"))
    parents = {p["parent_id"]: p for p in report["tables"]}

    groups: dict[str, SourceTable] = {}
    for t in report["logical_tables"]:
        p = parents.get(t["parent_id"])
        if not p:
            continue
        a, b = t["row_range"]
        header = [str(x or "") for x in (t.get("header") or [])]
        child = SourceTable(
            doc_id=doc_id,
            company=code,
            company_name=meta.get("公司名称", code),
            period=doc_period(doc_id),
            report_date=meta.get("公告日期", ""),
            logical_id=t["logical_id"],
            page=int(p.get("page_start") or 0),
            section=str(p.get("section") or ""),
            header=header,
            rows=[[str(c or "") for c in row] for row in p["rows"][a:b]],
        )
        parent = groups.get(t["parent_id"])
        if parent is None:
            parent = SourceTable(
                doc_id=child.doc_id,
                company=child.company,
                company_name=child.company_name,
                period=child.period,
                report_date=child.report_date,
                logical_id=t["parent_id"],
                page=child.page,
                section=child.section,
                header=header,
                rows=[],
            )
            groups[t["parent_id"]] = parent
        parent.children.append(child)
        parent.rows.extend(child.rows)

    return sorted(groups.values(), key=lambda t: (t.page, t.logical_id))


def _bs_blocks(by_page: list[SourceTable]) -> list[list[SourceTable]]:
    """同章节、相邻页的资产负债表父表并成一块。

    银行/保险把资产、负债与权益分在两三页，单张父表凑不齐锚点；
    合并后再判，合并报表才不会漏选。
    """
    blocks: list[list[SourceTable]] = []
    i = 0
    while i < len(by_page):
        head = by_page[i]
        if "资产负债表" not in head.section or any(k in head.section for k in ("母公司", "公司资产")):
            i += 1
            continue
        block = [head]
        labels = head.labels
        j = i
        while j + 1 < len(by_page):
            nxt = by_page[j + 1]
            if nxt.section == head.section and nxt.page - by_page[j].page <= 1:
                block.append(nxt)
                labels += nxt.labels
                j += 1
            else:
                break
        if sum(1 for a in BS_ANCHORS if a in labels) >= 3 and sum(len(t.rows) for t in block) >= 15:
            blocks.append(block)
        i = j + 1
    return blocks


def _is_parent(t: SourceTable) -> bool:
    """母公司报表（含「公司资产负债表/公司利润表/公司现金流量表」）v1 不抽。

    注意「合并及公司利润表」这种把两张表写进一个章节标题的情况：只要带「合并」
    就当作合并报表，不能按「公司利润表」误杀。
    """
    s = t.section
    if "母公司" in s:
        return True
    if "合并" in s:
        return False
    return any(k in s for k in ("公司资产", "公司利润表", "公司现金流量表"))


def select_tables(parents: list[SourceTable]) -> list[SourceTable]:
    """从父表列表里选出三表 + 主要会计数据，返回**逻辑子表**（保持行顺序）。"""
    by_page = sorted(parents, key=lambda t: (t.page, t.logical_id))
    picked: list[SourceTable] = []

    # 主要会计数据
    for t in by_page:
        if t.kind == "summary" and ("营业收入" in t.labels or "营业总收入" in t.labels):
            picked.append(t)
            break

    # 资产负债表：优先整块取同章节相邻页（银行/保险把资产与权益分页）
    picked_bs = False
    for block in _bs_blocks(by_page):
        labels = "".join(t.labels for t in block)
        if "资产总计" in labels or "资产合计" in labels:
            picked.extend(block[:4])
            picked_bs = True
            break
    if not picked_bs:
        bs_count = 0
        started = False
        for t in by_page:
            if t.kind != "bs":
                continue
            if not started:
                if "资产总计" not in t.labels:
                    continue
                started = True
            picked.append(t)
            bs_count += 1
            if "所有者权益合计" in t.labels or "负债合计" in t.labels:
                break
            if bs_count >= 4:
                break

    # 利润表：优先章节写明「合并」的，其次行数最多；母公司表不参与
    is_candidates = [t for t in by_page if t.kind == "is" and not _is_parent(t)]
    if is_candidates:
        consolidated = [t for t in is_candidates if "合并" in t.section]
        picked.append(max(consolidated or is_candidates, key=lambda t: len(t.rows)))

    # 现金流量表：排除附注/补充资料与母公司表；优先「合并」章节，其次行数最多
    cf_candidates = [
        t
        for t in by_page
        if t.kind == "cf"
        and not _is_parent(t)
        and "附注" not in t.section
        and "补充资料" not in t.section
    ]
    if cf_candidates:
        consolidated = [t for t in cf_candidates if "合并" in t.section]
        picked.append(max(consolidated or cf_candidates, key=lambda t: len(t.rows)))

    # 返回父表（已合并各逻辑段）：抽取以整张报表为单位，避免把半张表喂给模型
    seen: set[str] = set()
    out: list[SourceTable] = []
    for t in picked:
        if not t.children:
            t.children = [t]
        if t.logical_id not in seen:
            seen.add(t.logical_id)
            out.append(t)
    return out
