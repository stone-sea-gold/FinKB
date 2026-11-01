#!/usr/bin/env python3
"""FinKB L2 评测集校验（离线、确定性，不依赖 LLM）。

用法（在仓库根目录）：
    .venv/bin/python eval/check.py

校验内容：
  1. eval/gold_qa.json 的 schema 与题目类型分布（单点 8 / 跨期 4 / 跨司 3）；
  2. 每题 sql 在 finkb/data/facts.duckdb（只读）上复算：
     复算值必须等于 answer_exact，四舍五入两位后必须等于 answer；
  3. 每条 evidence 指向的 fact 必须真实存在，且 source_doc / page_num /
     metric_id / company / period / caliber / 单位一致；
  4. 物理页码必须在 data/parsed/<doc_id>/report.json 的页数范围内，
     page_anchored.md 中存在该页 META，且 fact 的 raw_text_matched
     确实出现在该页文本中；
  5. 跨期只允许方案 R2 口径：2025 年报内同期/上年列，或 2025 vs 2026 半年报；
     跨司必须是三家 2025 年报的同口径指标。

全部通过 exit 0；否则打印差异并 exit 1。
"""

from __future__ import annotations

import json
import re
import sys
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import duckdb

EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
GOLD_PATH = EVAL_DIR / "gold_qa.json"
DB_PATH = ROOT / "finkb" / "data" / "facts.duckdb"
PARSED_DIR = ROOT / "finkb" / "data" / "parsed"

EXPECTED_TYPE_COUNTS = {"single": 8, "cross_period": 4, "cross_company": 3}
EXPECTED_TOTAL = 15
META_RE = re.compile(r"<!-- META: (\{.*?\}) -->")
YEAR_REPORT_RE = re.compile(r"^\d{6}_2025_年报$")
HALF_REPORT_RE = re.compile(r"^\d{6}_2026_半年报$")
FORBIDDEN_DOC_RE = re.compile(r"_(2023|2024)_")

REQUIRED_FIELDS = (
    "id",
    "type",
    "question",
    "answer",
    "answer_exact",
    "unit",
    "metric_id",
    "companies",
    "periods",
    "caliber",
    "evidence",
    "sql",
)
# 题目单位 -> 事实库 norm_unit 允许值
UNIT_FAMILY = {"亿元": "亿元", "%": "%", "个百分点": "%", "元/股": "元/股"}

NUM_TOL = 1e-6  # 复算值与 answer_exact 的容差
ANS_TOL = 1e-9  # 复算两位小数与 answer 的容差
DB_LOCK_TIMEOUT_SEC = 60  # 事实库被其它进程写时，等待只读连接的最长秒数

FACT_COLUMNS = (
    "fact_id",
    "company",
    "period",
    "metric_id",
    "value_norm",
    "norm_unit",
    "caliber",
    "source_doc",
    "page_num",
    "raw_text_matched",
)
FACT_SQL = "SELECT " + ", ".join(FACT_COLUMNS) + " FROM facts WHERE fact_id = ?"


class CheckError(Exception):
    """单题校验失败（fail-fast 用，避免级联误报）。"""


def round2(value: float) -> float:
    """四舍五入到两位小数（ROUND_HALF_UP，与题目答案口径一致）。"""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def load_page_texts(doc_id: str, cache: dict[str, dict[int, str]]) -> dict[int, str]:
    """返回 doc 的 物理页码 -> 页文本；顺手校验 META 的 doc 字段与目录名一致。"""
    if doc_id in cache:
        return cache[doc_id]
    md = PARSED_DIR / doc_id / "page_anchored.md"
    if not md.is_file():
        raise CheckError(f"data/parsed/{doc_id}/page_anchored.md 不存在")
    text = md.read_text(encoding="utf-8")
    matches = list(META_RE.finditer(text))
    if not matches:
        raise CheckError(f"data/parsed/{doc_id}/page_anchored.md 没有任何 META")
    pages: dict[int, list[str]] = {}
    for index, match in enumerate(matches):
        meta = json.loads(match.group(1))
        if meta.get("doc") != doc_id:
            raise CheckError(f"{md.name} 中 META.doc={meta.get('doc')!r} 与目录名 {doc_id!r} 不一致")
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pages.setdefault(int(meta["page"]), []).append(text[start:end])
    result = {page: "\n".join(chunks) for page, chunks in pages.items()}
    cache[doc_id] = result
    return result


def load_report_pages(doc_id: str, cache: dict[str, int]) -> int:
    """report.json 的总页数（int 直接用，list 取长度）。"""
    if doc_id in cache:
        return cache[doc_id]
    report_path = PARSED_DIR / doc_id / "report.json"
    if not report_path.is_file():
        raise CheckError(f"data/parsed/{doc_id}/report.json 不存在")
    data = json.loads(report_path.read_text(encoding="utf-8"))
    pages = data.get("pages")
    if isinstance(pages, int) and not isinstance(pages, bool):
        total = pages
    elif isinstance(pages, list):
        total = len(pages)
    else:
        raise CheckError(f"{doc_id}/report.json 的 pages 字段类型无法识别：{pages!r}")
    if total <= 0:
        raise CheckError(f"{doc_id}/report.json 的 pages 非正数：{total}")
    cache[doc_id] = total
    return total


def fetch_fact(con: duckdb.DuckDBPyConnection, fact_id: int) -> tuple:
    rows = con.execute(FACT_SQL, [fact_id]).fetchall()
    if len(rows) != 1:
        raise CheckError(f"fact_id={fact_id} 在 facts 表中命中 {len(rows)} 行（期望 1 行）")
    return rows[0]


def check_evidence(
    question: dict,
    evidence: dict,
    fact: tuple,
    page_cache: dict[str, dict[int, str]],
    report_cache: dict[str, int],
) -> None:
    """校验一条 evidence 与库里 fact 的一致性，以及页码/原文可核对性。"""
    (
        fact_id,
        company,
        period,
        metric_id,
        value_norm,
        norm_unit,
        caliber,
        source_doc,
        page_num,
        raw_text_matched,
    ) = fact
    qid = question["id"]
    doc_id, page = evidence["doc_id"], evidence["page"]

    if source_doc != doc_id:
        raise CheckError(
            f"[{qid}] fact_id={fact_id} 的 source_doc={source_doc!r}，evidence 写的是 {doc_id!r}"
        )
    if int(page_num) != int(page):
        raise CheckError(f"[{qid}] fact_id={fact_id} 的 page_num={page_num}，evidence 写的是 {page}")
    if metric_id != question["metric_id"]:
        raise CheckError(f"[{qid}] fact_id={fact_id} 的 metric_id={metric_id!r}，题目写的是 {question['metric_id']!r}")
    if company not in question["companies"]:
        raise CheckError(f"[{qid}] fact_id={fact_id} 的 company={company!r} 不在题目 companies={question['companies']} 中")
    if period not in question["periods"]:
        raise CheckError(f"[{qid}] fact_id={fact_id} 的 period={period!r} 不在题目 periods={question['periods']} 中")
    if caliber != question["caliber"]:
        raise CheckError(f"[{qid}] fact_id={fact_id} 的 caliber={caliber!r}，题目写的是 {question['caliber']!r}")
    expected_unit = UNIT_FAMILY[question.get("evidence_unit", question["unit"])]
    if norm_unit != expected_unit:
        raise CheckError(f"[{qid}] fact_id={fact_id} 的 norm_unit={norm_unit!r}，与证据单位 {expected_unit!r} 不匹配")
    if not isinstance(value_norm, (int, float)):
        raise CheckError(f"[{qid}] fact_id={fact_id} 的 value_norm 非数值：{value_norm!r}")

    total_pages = load_report_pages(doc_id, report_cache)
    if not (1 <= int(page) <= total_pages):
        raise CheckError(f"[{qid}] 页码 {page} 超出 {doc_id} 的页数范围 1..{total_pages}")
    page_texts = load_page_texts(doc_id, page_cache)
    if int(page) not in page_texts:
        raise CheckError(f"[{qid}] {doc_id} 的 page_anchored.md 中找不到第 {page} 页的 META")
    raw = (raw_text_matched or "").strip()
    if not raw:
        raise CheckError(f"[{qid}] fact_id={fact_id} 的 raw_text_matched 为空，无法核对原页")
    if raw not in page_texts[int(page)]:
        raise CheckError(f"[{qid}] fact_id={fact_id} 的原文 {raw!r} 未出现在 {doc_id} 第 {page} 页文本中")


def connect_readonly(db_path: Path) -> duckdb.DuckDBPyConnection:
    """以只读方式打开事实库。

    本仓库其它模块（如 L1 事实抽取）可能短时间以写模式占用 DuckDB（单写锁），
    这时只读连接会失败，这里等待并重试，避免把“别人正在写库”误报成评测集错误。
    """
    deadline = time.monotonic() + DB_LOCK_TIMEOUT_SEC
    warned = False
    while True:
        try:
            return duckdb.connect(str(db_path), read_only=True)
        except duckdb.IOException as exc:
            if time.monotonic() >= deadline:
                raise CheckError(
                    f"无法以只读方式打开事实库（可能正被其它进程写入，已等待 {DB_LOCK_TIMEOUT_SEC} 秒）：{exc}"
                ) from exc
            if not warned:
                print(f"  事实库正被其它进程占用，等待最多 {DB_LOCK_TIMEOUT_SEC} 秒 ...")
                warned = True
            time.sleep(1.0)


def check_question(
    question: dict,
    con: duckdb.DuckDBPyConnection,
    companies: dict[str, str],
    page_cache: dict[str, dict[int, str]],
    report_cache: dict[str, int],
) -> float:
    """校验一道题；通过时返回 SQL 复算值，失败抛 CheckError。"""
    qid = question.get("id", "?")

    missing = [field for field in REQUIRED_FIELDS if field not in question]
    if missing:
        raise CheckError(f"[{qid}] 缺少字段：{missing}")
    if question["type"] not in EXPECTED_TYPE_COUNTS:
        raise CheckError(f"[{qid}] type={question['type']!r} 非法")
    if not isinstance(question["question"], str) or not question["question"].strip():
        raise CheckError(f"[{qid}] question 必须是非空字符串")
    if question["unit"] not in UNIT_FAMILY:
        raise CheckError(f"[{qid}] unit={question['unit']!r} 不在允许集合 {sorted(UNIT_FAMILY)} 中")
    # evidence_unit 可选：派生的比率题（如同比增速）答案是 %，但证据 fact 是亿元
    if question.get("evidence_unit") and question["evidence_unit"] not in UNIT_FAMILY:
        raise CheckError(f"[{qid}] evidence_unit={question['evidence_unit']!r} 不在允许集合 {sorted(UNIT_FAMILY)} 中")
    if question["caliber"] != "合并报表":
        raise CheckError(f"[{qid}] caliber 必须是「合并报表」")
    for field in ("answer", "answer_exact"):
        value = question[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CheckError(f"[{qid}] {field} 必须是数值：{value!r}")
    if abs(round2(question["answer_exact"]) - question["answer"]) > ANS_TOL:
        raise CheckError(
            f"[{qid}] answer={question['answer']} 与 answer_exact={question['answer_exact']} 的两位小数不一致"
        )

    evidence = question["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise CheckError(f"[{qid}] evidence 必须是非空数组")
    for item in evidence:
        for key in ("doc_id", "page", "fact_id"):
            if key not in item:
                raise CheckError(f"[{qid}] evidence 缺少 {key}：{item!r}")
    companies_in_q = question["companies"]
    periods_in_q = question["periods"]
    for code in companies_in_q:
        name = companies.get(code)
        if not name:
            raise CheckError(f"[{qid}] companies 中的代码 {code} 不在顶层 companies 映射里")
        if name not in question["question"]:
            raise CheckError(f"[{qid}] question 文本未提到公司名「{name}」")

    # ---- SQL 复算 ----
    sql = question["sql"].strip()
    if not sql.lower().startswith("select"):
        raise CheckError(f"[{qid}] sql 必须以 SELECT 开头（只读复算）")
    rows = con.execute(sql).fetchall()
    if len(rows) != 1 or len(rows[0]) != 1:
        raise CheckError(f"[{qid}] sql 必须返回 1 行 1 列，实际 {len(rows)} 行")
    computed = rows[0][0]
    if isinstance(computed, bool) or not isinstance(computed, (int, float)):
        raise CheckError(f"[{qid}] sql 复算结果不是数值：{computed!r}")
    if abs(float(computed) - float(question["answer_exact"])) > NUM_TOL:
        raise CheckError(
            f"[{qid}] sql 复算值 {computed} 与 answer_exact {question['answer_exact']} 不符"
        )
    if abs(round2(float(computed)) - question["answer"]) > ANS_TOL:
        raise CheckError(
            f"[{qid}] sql 复算值 {computed} 四舍五入两位为 {round2(float(computed))}，"
            f"与 answer {question['answer']} 不符"
        )

    # ---- 类型专项规则 ----
    qtype = question["type"]
    if qtype == "single":
        if len(evidence) != 1:
            raise CheckError(f"[{qid}] single 题必须有且仅 1 条 evidence")
        if len(set(companies_in_q)) != 1 or len(set(periods_in_q)) != 1:
            raise CheckError(f"[{qid}] single 题必须只有 1 家公司、1 个报告期")
    elif qtype == "cross_period":
        if len(evidence) < 2:
            raise CheckError(f"[{qid}] cross_period 题至少 2 条 evidence")
        if len(set(companies_in_q)) != 1:
            raise CheckError(f"[{qid}] cross_period 题必须只涉及 1 家公司")
        if len(set(periods_in_q)) < 2:
            raise CheckError(f"[{qid}] cross_period 题至少涉及 2 个报告期")
        for item in evidence:
            doc_id = item["doc_id"]
            if FORBIDDEN_DOC_RE.search(doc_id):
                raise CheckError(f"[{qid}] 跨期证据 {doc_id} 使用了 2023/2024 年报，违反 R2 口径")
            if not (YEAR_REPORT_RE.match(doc_id) or HALF_REPORT_RE.match(doc_id)):
                raise CheckError(f"[{qid}] 跨期证据 doc={doc_id!r} 不在 R2 允许口径内")
    elif qtype == "cross_company":
        if len(evidence) != 3:
            raise CheckError(f"[{qid}] cross_company 题必须恰好 3 条 evidence")
        if len(set(companies_in_q)) != 3:
            raise CheckError(f"[{qid}] cross_company 题必须涉及 3 家不同公司")
        for item in evidence:
            if not YEAR_REPORT_RE.match(item["doc_id"]):
                raise CheckError(f"[{qid}] 跨司证据 doc={item['doc_id']!r} 必须是「代码_2025_年报」")

    # ---- evidence 逐条核对（并收集 fact，用于跨期/跨司口径断言）----
    facts = []
    for item in evidence:
        fact = fetch_fact(con, item["fact_id"])
        check_evidence(question, item, fact, page_cache, report_cache)
        facts.append(fact)

    if qtype == "cross_period":
        for item, fact in zip(evidence, facts):
            doc_id = item["doc_id"]
            period = fact[2]
            if YEAR_REPORT_RE.match(doc_id):
                if period not in {"2025", "2024"}:
                    raise CheckError(f"[{qid}] 2025 年报证据的 fact.period={period!r}，R2 口径只允许 2025/2024")
            elif HALF_REPORT_RE.match(doc_id):
                if period not in {"2026H1", "2025H1"}:
                    raise CheckError(f"[{qid}] 2026 半年报证据的 fact.period={period!r}，R2 口径只允许 2026H1/2025H1")
    if qtype == "cross_company":
        fact_companies = {fact[1] for fact in facts}
        if len(fact_companies) != 3:
            raise CheckError(f"[{qid}] 3 条 evidence 指向的公司不是 3 家：{fact_companies}")
        if {fact[2] for fact in facts} != {"2025"}:
            raise CheckError(f"[{qid}] 跨司题的 fact.period 必须全部为 2025")
        metrics = {fact[3] for fact in facts}
        if metrics != {question["metric_id"]}:
            raise CheckError(f"[{qid}] 跨司题的 metric_id 不一致：{metrics}")

    return float(computed)


def main() -> int:
    if not GOLD_PATH.is_file():
        print(f"找不到 {GOLD_PATH}", file=sys.stderr)
        return 1
    gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
    questions = gold.get("questions")
    if not isinstance(questions, list):
        print("gold_qa.json 的 questions 必须是数组", file=sys.stderr)
        return 1
    companies = gold.get("companies")
    if not isinstance(companies, dict):
        print("gold_qa.json 的 companies 必须是对象", file=sys.stderr)
        return 1
    if not DB_PATH.is_file():
        print(f"找不到事实库 {DB_PATH}", file=sys.stderr)
        return 1

    print("FinKB L2 评测集校验")
    print(f"  评测集: {GOLD_PATH}")
    print(f"  事实库: {DB_PATH}（只读）")
    print()

    failures: list[tuple[str, str]] = []
    counts = {key: 0 for key in EXPECTED_TYPE_COUNTS}
    seen_ids: set[str] = set()
    page_cache: dict[str, dict[int, str]] = {}
    report_cache: dict[str, int] = {}

    try:
        con = connect_readonly(DB_PATH)
    except CheckError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    try:
        for question in questions:
            qid = question.get("id", "?")
            if qid in seen_ids:
                failures.append((qid, f"题目 id 重复：{qid}"))
                continue
            seen_ids.add(qid)
            qtype = question.get("type", "?")
            if qtype in counts:
                counts[qtype] += 1
            try:
                computed = check_question(question, con, companies, page_cache, report_cache)
            except CheckError as exc:
                failures.append((qid, str(exc)))
                print(f"[{qid}] {qtype:<14} FAIL")
            else:
                print(
                    f"[{qid}] {qtype:<14} PASS  "
                    f"{question['answer']:.2f} {question['unit']}"
                    f"（复算 {computed:.10g}，evidence {len(question['evidence'])} 条）"
                )
    finally:
        con.close()

    print()
    distribution = " / ".join(f"{key}={counts[key]}" for key in EXPECTED_TYPE_COUNTS)
    expected_distribution = " / ".join(f"{key}={value}" for key, value in EXPECTED_TYPE_COUNTS.items())
    if len(questions) != EXPECTED_TOTAL:
        failures.append(("总计", f"题目数 {len(questions)} ≠ {EXPECTED_TOTAL}"))
    if counts != EXPECTED_TYPE_COUNTS:
        failures.append(("总计", f"类型分布 {distribution} ≠ {expected_distribution}"))
    if failures:
        print(f"失败 {len(failures)} 处：")
        for qid, message in failures:
            print(f"  - {message}")
        return 1
    print(f"题目数 {len(questions)}（{distribution}）")
    print(f"通过 {len(questions)}/{EXPECTED_TOTAL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
