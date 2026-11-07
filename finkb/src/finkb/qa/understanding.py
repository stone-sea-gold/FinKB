"""问题理解层：规则优先、LLM 可选；输出结构化槽位并做时间覆盖守卫。

这一层只回答“问题在问什么”，不回答“答案是多少”：

- 规则解析公司/期间/指标/运算/限定词；
- 显式时间词没被解析 → ``unresolved``，事实通道拒答而不是默认年报；
- LLM 只补槽位（``FINKB_LLM_UNDERSTAND=1`` 时启用），失败自动回退规则；
- 数字与 SQL 仍只由事实通道/代码产生（ADR-001）。
"""

from __future__ import annotations

import copy
import difflib
import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from finkb.fact_engine.metric_dict import Metric, load_metric_dict
from finkb.fact_engine.selector import FINKB_ROOT

SCOPE_PATH = FINKB_ROOT / "config" / "wiki_scope.yaml"

# 期间写法：先长后短，避免「前三季度」被「三季度」抢先。
_PERIOD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?P<y>20\d{2})\s*年?\s*(?:前\s*[三3]\s*季度|[1一]\s*[-~至]\s*[3三]\s*季度)"), "Q1-Q3"),
    (re.compile(r"(?P<y>20\d{2})\s*年?\s*(?:第)?\s*[一1]\s*季度"), "Q1"),
    (re.compile(r"(?P<y>20\d{2})\s*年?\s*(?:第)?\s*[三3]\s*季度"), "Q3"),
    (re.compile(r"(?P<y>20\d{2})\s*年?\s*(?:上半年|半年度|半年报|半年)"), "H1"),
    (re.compile(r"(?P<y>20\d{2})\s*年?\s*(?:一季报)"), "Q1"),
    (re.compile(r"(?P<y>20\d{2})\s*年?\s*(?:三季报)"), "Q1-Q3"),
    (re.compile(r"(?P<y>20\d{2})\s*年?\s*(?:全年|年度|年报)"), ""),
    (re.compile(r"(?P<y>20\d{2})\s*年"), ""),
)
_QUARTER_PATTERN = re.compile(r"(?P<y>20\d{2})\s*[Qq]\s*(?P<q>[1-4])")
_BARE_YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_DOC_PATTERN = re.compile(r"(?P<y>20\d{2})\s*年?\s*(?P<t>年度报告|年报|半年报|一季报|三季报)")
_DOC_SUFFIX = {"年报": "", "年度报告": "", "半年报": "H1", "一季报": "Q1", "三季报": "Q3"}
_DOC_NAME = {"": "年报", "H1": "半年报", "Q1": "一季报", "Q3": "三季报"}

# 没有年份的期间词（“2025 年一季报和半年报”里的“半年报”），年份从上下文继承。
_BARE_PERIOD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"前\s*[三3]\s*季度|[1一]\s*[-~至]\s*[3三]\s*季度"), "Q1-Q3"),
    (re.compile(r"(?:第)?\s*[一1]\s*季度|一季报"), "Q1"),
    (re.compile(r"(?:第)?\s*[三3]\s*季度|三季报"), "Q1-Q3"),
    (re.compile(r"上半年|半年度|半年报"), "H1"),
)

_TIME_PRECISION = (
    ("季度", re.compile(r"(?:第)?\s*[一二三1-3]\s*季度|前\s*[三3]\s*季度")),
    ("半年", re.compile(r"上半年|半年度|半年报")),
    ("月度", re.compile(r"(?<!\d)(?:[1-9]|1[0-2])\s*月|前\s*[一二三四五六七八九十]{1,3}\s*个月")),
    ("相对期间", re.compile(r"去年|今年|明年|上一年|同期")),
)
_QUARTER_SUFFIX = {"1": "Q1", "2": "H1", "3": "Q3", "4": ""}

QUALIFIER_TOKENS = (
    "分产品", "分地区", "分行业", "分客户", "按产品", "按地区", "按行业", "按客户",
    "业务分部", "子行业", "分部",
    "海外", "境外", "境内", "国内", "国外", "出口", "内销",
    "华东", "华南", "华北", "华中", "西南", "西北", "东北",
    "客户", "渠道", "零售", "批发", "直销", "经销", "线上", "线下",
)
FILLER = (
    "请问", "一下", "是多少", "多少", "几", "亿元", "万元", "%", "％",
    "同比增速", "同比", "合计", "总计", "最高值", "最低值", "最高", "最低",
    "增加", "减少", "增长", "下降", "相比", "比较",
)


@dataclass
class Understanding:
    """问题理解结果；字段与旧 ``Slot`` 兼容。"""

    companies: list[str] = field(default_factory=list)
    company_names: list[str] = field(default_factory=list)
    periods: list[str] = field(default_factory=list)
    period_spans: list[str] = field(default_factory=list)
    metric: Metric | None = None
    operation: str = "single"
    qualifiers: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    source: str = "rules"
    source_doc: str | None = None
    document_period: str | None = None
    # 多轮上下文：这些槽位来自宿主传入的上一轮 resolved_slots（P5）。
    inherited: list[str] = field(default_factory=list)
    overridden: list[str] = field(default_factory=list)
    context_used: bool = False
    context_question: str = ""


@lru_cache(maxsize=1)
def _scope() -> dict[str, str]:
    raw = yaml.safe_load(Path(SCOPE_PATH).read_text(encoding="utf-8"))
    names: dict[str, str] = {}
    for item in raw.get("docs", []):
        name = str(item["company_name"])
        code = str(item["company"])
        names[name] = code
        names[code] = code
        short = name.rstrip("AB")
        if short != name and len(short) >= 2:
            names.setdefault(short, code)
    return names


@lru_cache(maxsize=1)
def scope_companies() -> tuple[tuple[str, str], ...]:
    """(公司名, 代码) 去重列表，供拒答文案与模糊匹配使用。"""
    seen: dict[str, str] = {}
    for name, code in _scope().items():
        if not name.isdigit():
            seen.setdefault(code, name)
    return tuple((name, code) for code, name in seen.items())


def _period_label(period: str) -> str:
    year, rest = period[:4], period[4:]
    return {
        "": f"{year} 年",
        "H1": f"{year} 年半年度",
        "Q1": f"{year} 年第一季度",
        "Q3": f"{year} 年第三季度",
        "Q1-Q3": f"{year} 年前三季度",
    }.get(rest, period)


def _parse_periods(text: str) -> tuple[list[str], list[str]]:
    """按出现顺序解析期间；同一片段只消费一次。"""
    periods: list[str] = []
    spans: list[str] = []
    consumed: list[tuple[int, int]] = []

    def _add(year: str, suffix: str, start: int, end: int, raw: str) -> None:
        if any(start < right and left < end for left, right in consumed):
            return
        consumed.append((start, end))
        period = f"{year}{suffix}"
        if period not in periods:
            periods.append(period)
            spans.append(raw)

    matches: list[tuple[int, int, str, str]] = []
    for pattern, suffix in _PERIOD_PATTERNS:
        for match in pattern.finditer(text):
            matches.append((match.start(), match.end(), f"{match.group('y')}{suffix}", match.group(0)))
    for match in _QUARTER_PATTERN.finditer(text):
        suffix = _QUARTER_SUFFIX.get(match.group("q"), "")
        matches.append((match.start(), match.end(), f"{match.group('y')}{suffix}", match.group(0)))
    for start, end, period, raw in sorted(matches, key=lambda item: item[0]):
        _add(period[:4], period[4:], start, end, raw)

    # 裸期间词（无年份）继承上下文年份：优先向前找最近年份，其次向后找，再次用唯一已解析年份。
    def _nearest_year(start: int) -> str | None:
        before = [m for m in _BARE_YEAR.finditer(text) if m.end() <= start]
        if before:
            return before[-1].group(1)
        after = [m for m in _BARE_YEAR.finditer(text) if m.start() >= start]
        if after:
            return after[0].group(1)
        years = {period[:4] for period in periods}
        return years.pop() if len(years) == 1 else None

    for pattern, suffix in _BARE_PERIOD_PATTERNS:
        for match in pattern.finditer(text):
            year = _nearest_year(match.start())
            if year:
                _add(year, suffix, match.start(), match.end(), match.group(0))

    if not periods:
        for match in _BARE_YEAR.finditer(text):
            _add(match.group(1), "", match.start(), match.end(), match.group(0))
            break
    return periods, spans


def _company_hits(text: str) -> tuple[list[str], list[str], list[str]]:
    names = _scope()
    hits: list[tuple[int, str, str]] = []
    for name, code in names.items():
        index = text.find(name)
        if index >= 0:
            hits.append((index, name, code))
    hits.sort()
    companies: list[str] = []
    company_names: list[str] = []
    seen: set[str] = set()
    for _, name, code in hits:
        if code in seen:
            continue
        seen.add(code)
        companies.append(code)
        company_names.append(name)
    return companies, company_names, []


def _fuzzy_companies(text: str) -> tuple[list[str], list[str], list[str]]:
    """公司名近似匹配：只处理“一个字写错”的场景，避免把普通词当公司。"""
    candidates: list[tuple[float, str, str]] = []
    for name, code in scope_companies():
        length = len(name)
        if length < 3:
            continue
        threshold = 0.66 if length == 3 else 0.75
        for start in range(0, len(text) - length + 1):
            window = text[start : start + length]
            if window == name or window.isdigit():
                continue
            ratio = difflib.SequenceMatcher(None, window, name).ratio()
            if ratio >= threshold:
                candidates.append((ratio, name, code))
    if not candidates:
        return [], [], []
    candidates.sort(reverse=True)
    ratio, name, code = candidates[0]
    return [code], [name], [f"公司名近似匹配：按「{name}」理解（相似度 {ratio:.2f}）"]


def _metric_hit(text: str) -> Metric | None:
    best: tuple[int, Metric] | None = None
    for metric in load_metric_dict():
        for candidate in (metric.metric_name, *metric.aliases):
            compact = candidate.replace(" ", "")
            if compact and compact in text and (best is None or len(compact) > best[0]):
                best = (len(compact), metric)
    return best[1] if best else None


def _operation(question: str) -> str:
    if "同比增速" in question or "同比" in question:
        return "yoy"
    if "最高值比最低值" in question:
        return "range"
    if "合计" in question or "总计" in question:
        return "sum"
    if "增加" in question or "增长" in question:
        return "diff_add"
    if "减少" in question or "下降" in question:
        return "diff_sub"
    return "single"


def _coverage(question: str, u: Understanding) -> list[str]:
    """显式时间词/相对时间没被解析 → 返回未覆盖片段。"""
    unresolved: list[str] = []
    for label, pattern in _TIME_PRECISION:
        match = pattern.search(question)
        if not match:
            continue
        if label == "季度":
            covered = any(period[4:] in ("Q1", "Q3", "Q1-Q3") for period in u.periods)
        elif label == "半年":
            covered = any(period[4:] == "H1" for period in u.periods)
        else:
            covered = False
        if not covered:
            unresolved.append(match.group(0))
    return list(dict.fromkeys(unresolved))


def _document_hint(text: str) -> tuple[str, str] | None:
    """识别“2026 年一季报/2025 年报”这类文档限定词，返回 (年份, 期间后缀)。"""
    match = _DOC_PATTERN.search(text)
    if not match:
        return None
    return match.group("y"), _DOC_SUFFIX[match.group("t")]


@lru_cache(maxsize=512)
def _rules(question: str) -> Understanding:
    text = question.replace(" ", "")
    u = Understanding()
    u.companies, u.company_names, u.notes = _company_hits(text)
    if not u.companies:
        u.companies, u.company_names, fuzzy_notes = _fuzzy_companies(text)
        u.notes.extend(fuzzy_notes)
    u.periods, u.period_spans = _parse_periods(question)
    doc = _document_hint(question)
    if doc:
        doc_year, doc_suffix = doc
        # “2026 年一季报里 2025 年第一季度”：文档年份不是查询期间，先剔除。
        if any(period[:4] != doc_year for period in u.periods):
            kept = [(p, s) for p, s in zip(u.periods, u.period_spans) if p[:4] != doc_year]
            u.periods = [p for p, _ in kept]
            u.period_spans = [s for _, s in kept]
        u.document_period = f"{doc_year}{doc_suffix}"
        if len(u.periods) == 1 and u.companies:
            u.source_doc = f"{u.companies[0]}_{doc_year}_{_DOC_NAME[doc_suffix]}"
    u.metric = _metric_hit(text)
    u.operation = _operation(question)
    u.qualifiers = [token for token in QUALIFIER_TOKENS if token in text]
    u.unresolved = _coverage(question, u)
    return u


def _llm_enabled() -> bool:
    return os.environ.get("FINKB_LLM_UNDERSTAND", "").strip().lower() in ("1", "true", "yes", "on")


@lru_cache(maxsize=256)
def _llm_call(question: str) -> dict | None:
    """可选的 LLM 读题；失败返回 None，不阻塞规则层。"""
    try:
        from finkb.search.embed import load_env

        load_env()
        import httpx

        base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
        key = os.environ.get("LLM_API_KEY", "")
        model = os.environ.get("LLM_MODEL", "")
        if not (base and key and model):
            return None
        prompt = (
            "你是财务问答的“问题理解器”。只输出 JSON，不要解释。字段："
            '{"entities":[{"raw":"","canonical":""}],"periods":[{"raw":"","normalized":""}],'
            '"metrics":[{"raw":"","metric_id":""}],"intent":"single_value|compare_period|'
            'compare_company|definition|relation|note_detail|narrative|negative",'
            '"qualifiers":[],"uncovered_spans":[]}。'
            "canonical 用 6 位股票代码；normalized 用 2025/2025Q1/2025H1/2025Q3/2025Q1-Q3；"
            "metric_id 只能用 revenue/net_profit/operating_cost/rd_expense/cash/inventory 等科目名；"
            "不确定就留空，不要编造。问题："
            + question
        )
        url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
        with httpx.Client(timeout=60) as client:
            response = client.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception:  # noqa: BLE001 - LLM 是可选增强，失败回退规则
        return None


def _merge_llm(u: Understanding, extra: dict) -> None:
    """LLM 只补规则没识别出的槽位；已识别的不覆盖。"""
    scope = _scope()
    if not u.companies:
        for entity in extra.get("entities") or []:
            raw = str(entity.get("raw") or "")
            code = str(entity.get("canonical") or "")
            if code not in scope.values():
                code = scope.get(raw, "")
            if code:
                u.companies, u.company_names = [code], [raw or code]
                u.notes.append(f"LLM 识别公司：{raw or code}")
                break
    if not u.periods:
        for item in extra.get("periods") or []:
            raw = str(item.get("raw") or "")
            normalized = str(item.get("normalized") or "")
            if normalized and re.fullmatch(r"20\d{2}(H1|Q1|Q3|Q1-Q3)?", normalized):
                u.periods, u.period_spans = [normalized], [raw or normalized]
                break
    if u.metric is None:
        for item in extra.get("metrics") or []:
            metric_id = str(item.get("metric_id") or "")
            for metric in load_metric_dict():
                if metric.metric_id == metric_id:
                    u.metric = metric
                    break
            if u.metric is not None:
                break
    for qualifier in extra.get("qualifiers") or []:
        if qualifier in QUALIFIER_TOKENS and qualifier not in u.qualifiers:
            u.qualifiers.append(str(qualifier))
    for span in extra.get("uncovered_spans") or []:
        if span and str(span) not in u.unresolved:
            u.unresolved.append(str(span))
    u.source = "rules+llm"


def understand(question: str, *, use_llm: bool | None = None, context: dict | None = None) -> Understanding:
    """规则解析 + 可选 LLM 补槽位 + 时间覆盖守卫 + 多轮槽位继承。

    ``context`` 只接收结构化槽位（``previous_slots``），不接收整段历史；
    当前问题里显式写出的值优先于继承值（P5 契约）。
    """
    u = copy.deepcopy(_rules(question))
    if use_llm is None:
        use_llm = _llm_enabled()
    if use_llm:
        extra = _llm_call(question)
        if extra:
            _merge_llm(u, extra)
    u.unresolved = list(dict.fromkeys(u.unresolved + _coverage(question, u)))
    if context:
        apply_context(u, context, question=question)
    return u


def residual_qualifiers(question: str) -> list[str]:
    """剥离公司/指标/期间/数字/填充词后，仍存在的范围限定词。"""
    text = question.replace(" ", "")
    for name in _scope():
        if len(name) >= 2:
            text = text.replace(name, "")
    u = _rules(question)
    if u.metric is not None:
        for candidate in (u.metric.metric_name, *u.metric.aliases):
            if candidate:
                text = text.replace(candidate.replace(" ", ""), "")
    for span in u.period_spans:
        text = text.replace(span.replace(" ", ""), "")
    text = re.sub(r"20\d{2}", "", text)
    for filler in FILLER:
        text = text.replace(filler, "")
    return [token for token in QUALIFIER_TOKENS if token in text]


# ---------------------------------------------------------------------------
# 多轮上下文契约（P5）：宿主只传结构化槽位，项目做确定性继承。
# ---------------------------------------------------------------------------

_AGGREGATE_RE = re.compile(r"哪些|哪个|所有|全部|各公司|各家|排名|最高|最低|超过|低于|大于|小于|几家")
_FOLLOWUP_PREFIX = re.compile(r"^(?:那|那么|还有|然后|它|他|她|这个|那个|这些|那些)+")


def _metric_by_id(metric_id: str) -> Metric | None:
    for metric in load_metric_dict():
        if metric.metric_id == metric_id:
            return metric
    return None


def _context_slots(context: dict | None) -> dict:
    """取出上一轮槽位；只有 previous_question 时用它兜底解析。"""
    if not isinstance(context, dict):
        return {}
    slots = context.get("previous_slots")
    if isinstance(slots, dict) and slots:
        return slots
    previous_question = context.get("previous_question")
    if isinstance(previous_question, str) and previous_question.strip():
        prev = _rules(previous_question.strip())
        return {
            "companies": prev.companies,
            "company_names": prev.company_names,
            "periods": prev.periods,
            "metric": prev.metric.metric_id if prev.metric else None,
            "qualifiers": prev.qualifiers,
        }
    return {}


@lru_cache(maxsize=1)
def _query_metric_aliases() -> tuple[str, ...]:
    """``metrics.yaml`` 里 kind=query 的指标名/别名（SQL 计算指标，不在事实字典里）。"""
    path = FINKB_ROOT / "config" / "metrics.yaml"
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ()
    names: set[str] = set()
    for spec in (raw.get("metrics") or {}).values():
        if not isinstance(spec, dict) or spec.get("kind") != "query":
            continue
        for candidate in (spec.get("name"), *(spec.get("aliases") or [])):
            text = str(candidate or "").replace(" ", "")
            if text:
                names.add(text)
    return tuple(sorted(names, key=len, reverse=True))


def _query_metric_hit(text: str) -> str | None:
    """当前问题是否点名了一个 SQL 计算指标（如毛利率）。"""
    for name in _query_metric_aliases():
        if name in text:
            return name
    return None


def _merge_followup(question: str, u: Understanding) -> str:
    """裸追问（“那 2024 年呢？”）没有可继承槽位时，用上一轮问题补全意图。

    只替换上一轮问题里的第一个期间写法；替换不到就原样返回，不猜。
    """
    if not u.context_question or not u.periods or not _FOLLOWUP_PREFIX.match(question):
        return question
    label = _period_label(u.periods[0])
    merged = re.sub(
        r"20\d{2}\s*年(?:度)?(?:上半年|一季度|前三季度|三季度)?|20\d{2}(?:H1|Q[1-3])",
        label,
        u.context_question,
        count=1,
    )
    return merged if merged != u.context_question else question


def apply_context(u: Understanding, context: dict | None, *, question: str = "") -> Understanding:
    """把上一轮槽位合并进本轮理解结果：缺则继承、新值覆盖、来源留痕。

    继承公司/期间/指标/限定词；**不继承** ``source_doc``（“点名哪份报告”随追问
    通常会变，继承会把 2025 年报错配到 2024 年的追问上）。
    """
    u.context_question = str((context or {}).get("previous_question") or "")
    slots = _context_slots(context)
    if not slots:
        return u
    u.context_used = True

    prev_companies = [str(item) for item in (slots.get("companies") or []) if item]
    prev_names = [str(item) for item in (slots.get("company_names") or []) if item]
    if prev_companies:
        if not u.companies:
            u.companies = list(prev_companies)
            u.company_names = list(prev_names) or list(prev_companies)
            u.inherited.append("company")
        elif u.companies != prev_companies:
            u.overridden.append("company")

    prev_periods = [str(item) for item in (slots.get("periods") or []) if item]
    if prev_periods:
        if not u.periods:
            u.periods = list(prev_periods)
            u.period_spans = []
            u.inherited.append("period")
        elif u.periods != prev_periods:
            u.overridden.append("period")

    prev_metric_id = slots.get("metric")
    if prev_metric_id and not _query_metric_hit(question or u.context_question):
        prev_metric = _metric_by_id(str(prev_metric_id))
        if prev_metric is not None:
            if u.metric is None:
                u.metric = prev_metric
                u.inherited.append("metric")
            elif u.metric.metric_id != prev_metric.metric_id:
                u.overridden.append("metric")

    prev_qualifiers = [str(item) for item in (slots.get("qualifiers") or []) if item]
    if prev_qualifiers:
        if not u.qualifiers:
            u.qualifiers = list(prev_qualifiers)
            u.inherited.append("qualifier")
        elif u.qualifiers != prev_qualifiers:
            u.overridden.append("qualifier")

    if u.inherited:
        u.notes.append("继承上一轮槽位：" + "、".join(u.inherited))
    return u


def resolved_question(question: str, u: Understanding) -> str:
    """把继承来的槽位还原成一句完整问题；没有继承时原样返回。"""
    if not u.inherited:
        return _merge_followup(question, u)
    company = "、".join(u.company_names or u.companies)
    period = "、".join(_period_label(item) for item in u.periods)
    head = " ".join(part for part in (company, period) if part)
    if "附注" in question and u.metric is not None:
        return f"{head}{u.metric.metric_name}的附注页是哪一页？".strip()
    for word in ("子公司", "股东", "担保", "关联方"):
        if word in question:
            return f"{head}的{word}有哪些？".strip()
    if u.metric is not None:
        suffix = {
            "single": "是多少？",
            "yoy": "同比增速是多少？",
            "sum": "合计是多少？",
            "diff_add": "增加了多少？",
            "diff_sub": "减少了多少？",
            "range": "最高值比最低值相差多少？",
        }.get(u.operation, "是多少？")
        qualifier = (" " + "、".join(u.qualifiers)) if u.qualifiers else ""
        body = f"{qualifier} {u.metric.metric_name}{suffix}".strip()
        return f"{head}{body}".strip()
    rest = _FOLLOWUP_PREFIX.sub("", question).strip()
    return f"{head} {rest}".strip() if head and rest else (head or question)


def _clarify_message(missing: list[str]) -> str:
    if "company" in missing and "period" in missing:
        return "请补充公司名称和期间（例如：比亚迪 2025 年营业收入）。"
    if "company" in missing:
        return "请补充公司名称（例如：比亚迪、美的集团）。"
    return "请补充期间（例如：2025 年、2025 年半年度、2025 年第一季度）。"


def context_meta(question: str, u: Understanding) -> dict:
    """给宿主的结构化多轮信息；``clarify`` 非空表示项目不猜、请宿主追问。"""
    aggregate = bool(_AGGREGATE_RE.search(question))
    missing: list[str] = []
    if u.metric is not None and not aggregate:
        if not u.companies:
            missing.append("company")
        if not u.periods:
            missing.append("period")
    clarify = None
    if missing:
        candidates: dict[str, list[str]] = {}
        if "company" in missing:
            candidates["company"] = [name for name, _ in scope_companies()]
        clarify = {"missing": missing, "candidates": candidates, "message": _clarify_message(missing)}
    return {
        "resolved_question": resolved_question(question, u),
        "resolved_slots": {
            "companies": list(u.companies),
            "company_names": list(u.company_names),
            "periods": list(u.periods),
            "metric": u.metric.metric_id if u.metric is not None else None,
            "qualifiers": list(u.qualifiers),
            "source_doc": u.source_doc,
            "operation": u.operation,
        },
        "inherited": list(u.inherited),
        "overridden": list(u.overridden),
        "missing": missing,
        "clarify": clarify,
        "context_used": bool(u.context_used),
    }
