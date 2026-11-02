"""期间口径标注（ADR-055）。

率值（本书只管 ROE）在半年报和季报里的披露口径不统一：招行半年报的
「加权平均净资产收益率 13.85%」是全半年年化后的值，而年报的同一位置是
年度值。两者塞进同一列，排名和同比就会拿 13.85 去比 10.6。

判定用反算法而不是关键词：同一 (公司, 期间, 口径) 的净利润 / 期末净资产
× 100% 就是「未年化」的参考值，看披露值是不是它的 2 倍（半年）或 4 倍
（一季）。实测招行 2025H1：披露 13.85，反算 749.0 / 12350 ≈ 6.06，
13.85 / 6.06 ≈ 2.29 —— 落在 2 倍附近，判为 annualized。

用的是期末净资产而不是加权平均净资产，所以倍数不会精确落在 2 或 4 上；
阈值取保守值（半年 1.5、一季 2.5），宁可漏标也不误标。
"""

from __future__ import annotations

import re

from finkb.fact_engine.store import FactStore

# 期间后缀 -> 判定阈值。期间 id 的形态是 2025 / 2025H1 / 2025Q1 / 2025Q1-Q3。
# Q1-Q3 是「年初至报告期末」的累计值，准则不要求年化，阈值只防异常放大。
_INTERIM = {
    "Q1": 2.5,
    "H1": 1.5,
    "Q1-Q3": 1.1,
}
_PATTERN = re.compile(r"(Q1-Q3|H1|Q1)$")

_RATE_METRIC = "roe_weighted"


def _interim_kind(period: str) -> str | None:
    match = _PATTERN.search(str(period or ""))
    return match.group(1) if match else None


def annotate_period_basis(store: FactStore) -> dict[str, int]:
    """给 period_basis 打标，返回 {"annualized": n, "period": n} 统计。"""
    counts = {"annualized": 0, "period": 0}
    # 用标量子查询而不是 LEFT JOIN：同一 (公司,期间,口径) 在年报和半年报里各有一条
    # 同指标事实，JOIN 会把行数乘起来（实测 64 行被数成 89 行）。
    rows = store.conn.execute(
        """SELECT f.company, f.period, f.caliber, f.value_norm,
                  (SELECT max(n.value_norm) FROM facts n
                    WHERE n.company = f.company AND n.period = f.period
                      AND n.metric_id = 'net_profit' AND n.caliber = f.caliber),
                  (SELECT max(a.value_norm) FROM facts a
                    WHERE a.company = f.company AND a.period = f.period
                      AND a.metric_id = 'net_assets' AND a.caliber = f.caliber)
             FROM facts f WHERE f.metric_id = ?""",
        [_RATE_METRIC],
    ).fetchall()
    for company, period, caliber, value, net_profit, net_assets in rows:
        basis = "period"
        kind = _interim_kind(period)
        if kind and value is not None and net_profit and net_assets:
            reference = net_profit / net_assets * 100
            if reference and abs(value / reference) >= _INTERIM[kind]:
                basis = "annualized"
        counts[basis] += 1
        store.conn.execute(
            "UPDATE facts SET period_basis = ? WHERE company = ? AND period = ? "
            "AND metric_id = ? AND caliber = ?",
            [basis, company, period, _RATE_METRIC, caliber],
        )
    return counts


__all__ = ["annotate_period_basis"]
