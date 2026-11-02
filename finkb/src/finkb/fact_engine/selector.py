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
    # 逻辑表的 section_path（如 ["分产品"]）。分节切分常把「分产品/分行业」这类
    # 行标记吃掉，父表的 rows 里就看不到它了；结构表解析靠这个字段还原维度。
    section_path: tuple[str, ...] = ()

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
            section_path=tuple(t.get("section_path") or ()),
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


_FAMILY_NAME = {"bs": "资产负债表", "is": "利润表", "cf": "现金流量表"}
# 页面标题不可靠：有的把整张报表的续页写成单位说明、甚至写成审计责任段落。
# 这些章节只可能是附注/分析，不能当作报表的起点或续页。
_NOTE_SECTIONS = ("附注", "补充资料", "主营业务分析", "财务指标", "分季度", "非经常性损益")
# 报表续页的首行（上一页在「非流动资产」「三、营业利润」处断开时）。
_CONT_START = {
    "bs": ("非流动资产", "负债和股东权益", "负债", "股东权益"),
    "is": ("三、营业利润", "营业利润", "四、净利润", "净利润", "其他综合收益", "综合收益总额"),
    "cf": ("收回投资收到的现金", "投资活动", "吸收投资收到的现金", "筹资活动", "汇率变动"),
}
# 报表在续页上重新以这些行开头时，说明是另一张表（合并 → 母公司）。
_STATEMENT_START = {
    "bs": ("资产",),
    "is": ("一、营业收入", "营业收入", "营业总收入"),
    "cf": ("销售商品", "销售商品、提供劳务收到的现金", "为交易目的"),
}
# 一张报表在单页上排到这些行，说明这半张已经结束。
_TERMINAL = {
    "bs": ("负债和股东权益总计",),
    "is": ("净利润", "综合收益总额"),
    "cf": ("年末现金及现金等价物余额", "现金及现金等价物净增加额"),
}
_CALIBER = ("少数股东权益", "归属于母公司", "少数股东损益")


def _has_note_section(t: SourceTable) -> bool:
    return any(k in t.section for k in _NOTE_SECTIONS)


def _family_match(t: SourceTable, family: str) -> bool:
    """粗判一张表是否属于某类报表（只看内容，不看章节）。"""
    lab = t.labels
    if family == "bs":
        return any(a in lab for a in BS_ANCHORS)
    if family == "is":
        return ("营业收入" in lab or "营业总收入" in lab or "利润表" in t.section)
    return (
        bool(_CF_NET.search(lab))
        or "现金流量表" in t.section
        # 续页只剩投资/筹资活动，没有「经营活动净额」可认，靠这两个锚点接上。
        or "投资活动" in lab
        or "筹资活动" in lab
        or "现金及现金等价物" in lab
    )


def _seed(t: SourceTable, family: str) -> bool:
    """报表的「种子页」：带足以定性的锚点，且不是母公司/附注。"""
    if _is_parent(t) or _has_note_section(t):
        return False
    lab = t.labels
    if family == "bs":
        return "资产总计" in lab or "负债和股东权益总计" in lab
    if family == "is":
        return ("营业收入" in lab or "营业总收入" in lab) and len(t.rows) >= 10
    return bool(_CF_NET.search(lab)) and len(t.rows) >= 8


def _is_terminal(t: SourceTable, family: str) -> bool:
    lab = t.labels
    return any(a in lab for a in _TERMINAL[family])


def _first_label(t: SourceTable) -> str:
    return str(t.rows[0][0]).strip() if t.rows else ""


def _hit(label: str, anchors: tuple[str, ...]) -> bool:
    """``label`` 是否命中任一锚点。

    必须是**子串**匹配而不是相等：续页首行常带序号前缀（「三、筹资活动产生的
    现金流量:」「一、营业收入」），用 ``in tuple`` 判等会全部漏掉，跨页缝合就
    静默失败——万科 000002 的合并现金流量表正是因此被切成两半，然后由「选行数
    最多的表」规则选中了母公司表（v3 修正，见 DEVLOG 坑-77）。
    """
    return any(a in label for a in anchors)


def _statement_runs(by_page: list[SourceTable], family: str) -> list[list[SourceTable]]:
    """把跨页切开的同一张报表按页序拼成一组。

    规则不依赖 section 是否写着报表名：种子页看内容锚点，向后续页只要
    「相邻页 + 同类内容 + 不是母公司/附注」；若续页标题明确写着报表名就直接接，
    否则只在「上一页尚未结束 + 下一页首行是续页行」时接。向前同理，
    用于补回第一页被标成审计段落的情况（神华 P147）。
    """
    name = _FAMILY_NAME[family]
    runs: list[list[SourceTable]] = []
    seen: set[str] = set()
    for i, t in enumerate(by_page):
        if not _seed(t, family):
            continue
        start = i
        while start > 0:
            prev, cur = by_page[start - 1], by_page[start]
            if cur.page - prev.page != 1:
                break
            if _is_parent(prev) or _has_note_section(prev) or not _family_match(prev, family):
                break
            if not _hit(_first_label(cur), _CONT_START[family]) and name not in cur.section:
                break
            start -= 1
        end = i
        while end + 1 < len(by_page):
            cur, nxt = by_page[end], by_page[end + 1]
            if nxt.page - cur.page != 1:
                break
            if _is_parent(nxt) or _has_note_section(nxt) or not _family_match(nxt, family):
                break
            if name in nxt.section:
                pass  # 「合并利润表（续）」这类明确续页，直接接
            else:
                if _is_terminal(cur, family) or _hit(_first_label(nxt), _STATEMENT_START[family]):
                    break
                if not _hit(_first_label(nxt), _CONT_START[family]):
                    break
            end += 1
        run = by_page[start : end + 1]
        key = run[0].logical_id
        if key not in seen:
            seen.add(key)
            runs.append(run)
    return runs


def _complete(run: list[SourceTable], family: str) -> bool:
    lab = "".join(t.labels for t in run)
    rows = sum(len(t.rows) for t in run)
    if family == "bs":
        return "资产总计" in lab and ("负债合计" in lab or "负债和股东权益总计" in lab) and rows >= 20
    if family == "is":
        return ("营业收入" in lab or "营业总收入" in lab) and ("净利润" in lab or "营业利润" in lab) and rows >= 15
    return bool(_CF_NET.search(lab)) and ("投资活动" in lab or "筹资活动" in lab) and rows >= 15


def _best_run(by_page: list[SourceTable], family: str) -> list[SourceTable]:
    """在完整的候选块里挑合并口径：有少数股东/归母线的最优，其次章节带「合并」。"""
    best: list[SourceTable] = []
    best_score = -1
    for run in _statement_runs(by_page, family):
        if not _complete(run, family):
            continue
        lab = "".join(t.labels for t in run)
        score = sum(len(t.rows) for t in run)
        if any(m in lab for m in _CALIBER):
            score += 100
        if any("合并" in t.section for t in run):
            score += 10
        if max(_year_span(t.header) for t in run) > 3:
            score -= 1000  # 多年度摘要表不当期报表，永不胜出
        if score > best_score:
            best, best_score = run, score
    return best


def _repair_family(
    by_page: list[SourceTable], family: str, picked: list[SourceTable]
) -> list[SourceTable]:
    """旧规则有明显缺口时，用整块拼接结果替换；否则原样返回。"""
    run = _best_run(by_page, family)
    if not run:
        return picked
    lab = "".join(t.labels for t in picked)
    if family == "bs":
        broken = (
            not picked
            or "资产总计" not in lab
            or _first_label(picked[0]) in ("非流动资产", "负债和股东权益", "负债")
        )
    elif family == "is":
        broken = not picked
    else:
        broken = (
            not picked
            or ("投资活动" not in lab and "筹资活动" not in lab)
            or any(k in t.section for t in picked for k in ("主营业务分析", "财务指标"))
        )
    return run if broken else picked


_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def _year_span(header: list[str]) -> int:
    """表头里出现过几个不同年份。多年度摘要（5 年）不是当期法定报表。"""
    years: set[str] = set()
    for cell in header:
        years.update(_YEAR_RE.findall(cell or ""))
    return len(years)


def select_tables(parents: list[SourceTable]) -> list[SourceTable]:
    """从父表列表里选出三表 + 主要会计数据，返回**逻辑子表**（保持行顺序）。"""
    by_page = sorted(parents, key=lambda t: (t.page, t.logical_id))
    picked: list[SourceTable] = []

    # 主要会计数据
    for t in by_page:
        if t.kind == "summary" and ("营业收入" in t.labels or "营业总收入" in t.labels):
            picked.append(t)
            break

    # 主要财务指标：部分文档把每股收益/ROE 单独成表（如 601127 2025 年报的
    # 「(二) 主要财务指标」），与主要会计数据分开；漏选会静默丢掉 eps/roe。
    for t in by_page:
        if t in picked:
            continue
        head = str(t.header[0] or "") if t.header else ""
        if ("主要财务指标" in t.section or "主要财务指标" in head) and (
            "每股收益" in t.labels or "净资产收益率" in t.labels
        ):
            picked.append(t)
            break

    # 资产负债表：优先整块取同章节相邻页（银行/保险把资产与权益分页）
    bs: list[SourceTable] = []
    for block in _bs_blocks(by_page):
        labels = "".join(t.labels for t in block)
        if "资产总计" in labels or "资产合计" in labels:
            bs = block[:4]
            break
    if not bs:
        bs_count = 0
        started = False
        for t in by_page:
            if t.kind != "bs":
                continue
            if not started:
                if "资产总计" not in t.labels:
                    continue
                started = True
            bs.append(t)
            bs_count += 1
            if "所有者权益合计" in t.labels or "负债合计" in t.labels:
                break
            if bs_count >= 4:
                break

    # 利润表：优先章节写明「合并」的，其次行数最多；母公司表不参与
    is_candidates = [t for t in by_page if t.kind == "is" and not _is_parent(t)]
    # 多年度摘要表（如神华的 2021–2025 五年利润表）不是当期法定报表：
    # 只在还剩下「当期口径」候选时才把它排除，避免把唯一候选也筛掉。
    skipped_history = False
    if len(is_candidates) > 1:
        current = [t for t in is_candidates if _year_span(t.header) <= 3]
        if current:
            skipped_history = any(_year_span(t.header) > 3 for t in is_candidates if t not in current)
            is_candidates = current
    income: list[SourceTable] = []
    if is_candidates:
        consolidated = [t for t in is_candidates if "合并" in t.section]
        income = [max(consolidated or is_candidates, key=lambda t: len(t.rows))]
    if skipped_history:
        # 当期利润表常常还有续页（净利润的分类、其他综合收益），单张会漏「归母净利润」。
        run = _best_run(by_page, "is")
        if run:
            income = run

    # 现金流量表：先按页序缝合跨页报表，缝不出来才退回「合并章节 + 行数最多」。
    # 不能直接用行数最多：母公司现金流量表通常比合并表的单页片段更长，万科
    # 000002 就是被这条规则选中了母公司表，导致经营/投资/筹资来自不同主体。
    cf_candidates = [
        t
        for t in by_page
        if t.kind == "cf"
        and not _is_parent(t)
        and "附注" not in t.section
        and "补充资料" not in t.section
    ]
    cash: list[SourceTable] = []
    if cf_candidates:
        cash = _best_run(by_page, "cf")
    if not cash and cf_candidates:
        consolidated = [t for t in cf_candidates if "合并" in t.section]
        cash = [max(consolidated or cf_candidates, key=lambda t: len(t.rows))]

    picked.extend(_repair_family(by_page, "bs", bs))
    picked.extend(_repair_family(by_page, "is", income))
    picked.extend(_repair_family(by_page, "cf", cash))

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
