"""调用 LLM 把一张表读成 Observation 列表。

模型只做一件事：把「行/列指标名」对到指标字典的 ``metric_id``。
它不计算、不换算单位、不补缺失值——数字由 :mod:`finkb.fact_engine.verifier`
回到原始单元格逐字核对，单位换算也在那边用代码做。

端点按 OpenAI 兼容协议；本仓库实测的 opencode Go 端点还要求
``x-opencode-session`` 请求头，见 DEVLOG §9.2.1。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass

import httpx

from finkb.fact_engine.metric_dict import Metric
from finkb.fact_engine.selector import SourceTable, norm_label
from finkb.fact_engine.verifier import parse_number

SYSTEM_PROMPT = """你是财务报表结构化助手。你只做一件事：把表格里“明确写出来的数字”对到给定指标字典。

规则：
1. 只输出字典里出现的 metric_id；字典里没有的指标，一律不要输出。
2. 只输出表格单元格里真实存在的数字；不要计算、不要推断、不要用“同比增减”列。
3. row 是行号（表格最左侧的数字），col 是列号（表头里的数字）；只允许取给出“期间”的列。
4. value_raw 原样抄该单元格的文本（保留千分位逗号）。
5. 同一指标在同一列只输出一次；空单元格、横杠、括号注释都不要输出。
6. 只输出 JSON，不要解释。格式：{"observations":[{"metric_id":"revenue","row":3,"col":2,"value_raw":"803,964,958"}]}"""


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    session_id: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "LLMConfig":
        from finkb.cli import DEFAULT_ENV_FILE, load_env_file

        values = load_env_file(DEFAULT_ENV_FILE)
        values.update({k: v for k, v in os.environ.items() if k in {"LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"}})
        values.update(env or {})
        key = values.get("LLM_API_KEY", "")
        if not key:
            raise RuntimeError("缺少 LLM_API_KEY（config/.env）")
        return cls(
            api_key=key,
            base_url=values.get("LLM_BASE_URL", "https://opencode.ai/zen/go").rstrip("/"),
            model=values.get("LLM_MODEL", "deepseek-v4.1-flash"),
            session_id=f"finkb-{uuid.uuid4().hex[:12]}",
        )


class LLMClient:
    def __init__(self, config: LLMConfig, timeout: float = 120.0) -> None:
        self.config = config
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def chat_json(self, system: str, user: str) -> dict:
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 16000,
        }
        resp = self._client.post(
            f"{self.config.base_url}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "x-opencode-session": self.config.session_id,
                "User-Agent": "finkb/0.1 (financial-report extraction)",
            },
            json=payload,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(_strip_fence(content))


def _normalize_item(item: dict) -> dict:
    """模型偶尔把 ``metric_id`` 写成 ``metric``，统一回规范键名。"""
    if isinstance(item, dict) and "metric_id" not in item and "metric" in item:
        item["metric_id"] = item.pop("metric")
    return item


def _strip_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s


def flow_period(doc_period: str) -> str:
    """三季报的利润表/现金流量表是 1–9 月累计，期间标签必须与 Q1/H1 区分。"""
    if doc_period.endswith("Q3"):
        return f"{doc_period[:4]}Q1-Q3"
    return doc_period


def column_periods(header: list[str], doc_period: str, kind: str = "other") -> dict[int, str]:
    """把表头列映射成规范期间；映射不上的列（附注、增减）不返回。"""
    year0 = int(doc_period[:4])
    suffix = doc_period[4:]
    flow = flow_period(doc_period)
    prev_flow = f"{year0 - 1}" + ("Q1-Q3" if suffix == "Q3" else suffix)
    labels = [norm_label(h) for h in header]
    has_cumulative = any(k in t for t in labels for k in ("年初至", "前三季度", "前三季", "1-9月"))
    out: dict[int, str] = {}
    for i, raw in enumerate(header):
        if i == 0:
            continue
        t = norm_label(raw)
        if not t or any(k in t for k in ("增减", "变动", "占比", "附注")):
            continue
        if any(k in t for k in ("年初至", "前三季度", "前三季", "1-9月")):
            m = re.search(r"(19\d{2}|20\d{2})", t)
            out[i] = f"{m.group(1)}Q1-Q3" if m and int(m.group(1)) != year0 else flow
            continue
        if "年初" in t or "期初" in t:
            m = re.search(r"(19\d{2}|20\d{2})", t)
            out[i] = f"{int(m.group(1)) - 1}" if m else f"{year0 - 1}"
            continue
        if "年末" in t or "期末" in t:
            m = re.search(r"(19\d{2}|20\d{2})", t)
            out[i] = m.group(1) if m and int(m.group(1)) != year0 else doc_period
            continue
        m = re.search(r"(19\d{2}|20\d{2})", t)
        if "12月31日" in t and m:
            out[i] = m.group(1)  # 年末余额是年度时点，不属于 H1/Q3
            continue
        if m:
            year = int(m.group(1))
            out[i] = doc_period if year == year0 else f"{year}{suffix}"
            continue
        if any(k in t for k in ("本报告期", "本年")):
            out[i] = doc_period
            continue
        if "本期" in t or "上期" in t or "本期发生额" in t:
            # 三季报的利润表/现金流量表不写「年初至」，但口径就是 1–9 月累计
            if suffix == "Q3" and kind in ("is", "cf"):
                out[i] = flow if "本期" in t else prev_flow
            else:
                out[i] = doc_period if "本期" in t else f"{year0 - 1}{suffix}"
            continue
        if any(k in t for k in ("上年同期", "上年")):
            out[i] = prev_flow if (suffix == "Q3" and (kind in ("is", "cf") or has_cumulative)) else f"{year0 - 1}{suffix}"
    return out

KEY_ROWS = {
    "summary": ("营业收入", "营业总收入", "净利润", "资产总计"),
    "bs": ("资产总计", "资产合计", "负债合计", "所有者权益合计", "股东权益合计"),
    "is": ("营业收入", "营业总收入", "净利润"),
    "cf": ("经营活动产生的现金流量净额",),
}


def missing_columns(table: SourceTable, periods: dict[int, str], obs: list[dict]) -> list[int]:
    """有数、但一条观测都没落到的期间列——静默漏列的检测面。

    只按「关键行」判断，避免把整列空白的附注列当成漏抽。
    """
    hit: set[int] = set()
    for item in obs:
        try:
            hit.add(int(item["col"]))
        except (KeyError, TypeError, ValueError):
            continue
    keys = KEY_ROWS.get(table.kind, ())
    if not keys:
        return []
    key_rows = [r for r in table.rows if any(k in norm_label(str(r[0])) for k in keys)]
    gaps = []
    for col in periods:
        if col in hit:
            continue
        rows = key_rows
        if not rows:  # 关键行名不匹配时退回「这一列有没有数」：至少 3 个数值才算有数据
            rows = table.rows
            if sum(1 for r in rows if col < len(r) and parse_number(r[col]) is not None) < 3:
                continue
        if any(col < len(row) and parse_number(row[col]) is not None for row in rows):
            gaps.append(col)
    return gaps


def build_prompt(table: SourceTable, metrics: list[Metric], periods: dict[int, str]) -> str:
    metric_lines = "\n".join(
        f"{m.metric_id} | {m.metric_name} | {'/'.join(m.aliases) or '无'}" for m in metrics
    )
    col_lines = "\n".join(f"col {i} = {label}（{periods[i]}）" for i, label in enumerate(table.header) if i in periods)
    rows = "\n".join(
        f"{i}\t" + "\t".join(c.replace("\t", " ") for c in row) for i, row in enumerate(table.rows)
    )
    return (
        f"公司：{table.company_name}（{table.company}）\n"
        f"报告期：{table.period}（公告日 {table.report_date}）\n"
        f"表种：{table.kind}；页码：{table.page}\n\n"
        f"指标字典：\n{metric_lines}\n\n"
        f"可用列：\n{col_lines}\n\n"
        f"表头：{table.header}\n"
        f"表格行（行号<TAB>单元格）：\n{rows}"
    )


def extract_table(
    client: LLMClient,
    table: SourceTable,
    metrics: list[Metric],
    only_cols: list[int] | None = None,
    nudge: str | None = None,
) -> tuple[list[dict], dict[int, str]]:
    """返回 (observations, 列期间映射)。解析失败时抛 ``ValueError``。"""
    periods = column_periods(table.header, table.period, table.kind)
    if not periods and table.rows:
        # 少数表（尤其中报的主要会计数据）把表头并进了第一行，header 为空：
        # 就地提升首行为表头并削掉该行，行号随之平移，写库时用的是同一个对象。
        table.header = [str(c) for c in table.rows[0]]
        table.rows = table.rows[1:]
        periods = column_periods(table.header, table.period, table.kind)
    if not periods:
        raise ValueError(f"{table.logical_id}: 表头没有可识别的期间列 {table.header}")
    user = build_prompt(table, metrics, periods)
    if only_cols:
        user += "\n\n上一次输出漏掉了这些有数据的列，请只补这些列的观测：" + "、".join(
            f"col {c}（{periods[c]}）" for c in only_cols
        )
    if nudge:
        user += "\n\n" + nudge
    payload = client.chat_json(SYSTEM_PROMPT, user)
    obs = payload.get("observations")
    if not isinstance(obs, list):
        raise ValueError(f"{table.logical_id}: 返回体缺 observations")
    return [_normalize_item(item) for item in obs], periods
