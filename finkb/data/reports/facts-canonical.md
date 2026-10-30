# 唯一事实库（L1）说明

*生成时间：2026-10-30 晚；代码：本文件所在提交；模型 `deepseek-v4.1-flash`（OpenCode Go）*

## 是什么

同一版代码、同一套提示词、同一份 21 项指标字典，对**确定的 29 份文档**一次性生成的事实库。
与之前的多个 `facts_*.duckdb` 快照不同，它只有一个；旧快照已归档到 `.tmp/db_archive/`。

- 文件：`data/facts.duckdb`（`*.duckdb` 被 git 忽略，不入库）
- 文档清单：`data/parsed/` 下 29 份（14 家公司，2021–2026Q1 共 15 个期间）
- 抽取入口：`finkb.fact_engine.ingest.ingest_documents`（workers=4）

## 统计

| 项 | 值 |
|---|---|
| 文档 | 29 |
| 公司 | 14 |
| 事实 | **1064 条** |
| 未匹配（`unmatched_pool`） | **0** |
| `match_level` | 全部 `exact`（每个数字都能在所引页码逐字找到） |
| 同一文档内重复 (期间, 指标) | 0（重复在写库时先到先得，计入 `duplicated`） |
| 单位分布 | 元 801 / 千元 104 / 万元 43 / 百万元 116 |

**逐文档条数**：赛力斯 205、长安 199、比亚迪 195、长城 62、广汽 45、茅台 45、招行 45、
美的 44、恒瑞 41、长电 41、神华 39、中信 37、万科 34、平安 32。

## 校验

- **资产负债表恒等式**：同一文档、同一期间内，`total_assets = total_liabilities + total_equity`
  共 42 组，**全部差额为 0**（阈值 0.5 亿元）。
- **单位**：全量重灌暴露并修复了两类误判（比亚迪 summary 元 / 三表千元；长城 summary 万元 /
  资产负债表元），规则见 DEVLOG ADR-037。
- **期间覆盖**（15 个）：2021、2022、2023、2023H1、2024、2024H1、2024Q1、2024Q1-Q3、
  2025、2025H1、2025Q1、2025Q1-Q3、2025Q3、2026H1、2026Q1。
- **金标事实集**：72 条（3 家车企 × 8 指标 × 3 年），从 PDF 文本层独立抄录，
  `tools/gold_check.py` 按 `source_doc + period + metric_id` 比对，**72/72 通过**（ADR-038）。

## 复现

```bash
cd finkb
PYTHONPATH=src ../.venv/bin/python - <<'PY'
import re, time
from pathlib import Path
from finkb.fact_engine.ingest import ingest_documents
from finkb.fact_engine.extractor import LLMClient, LLMConfig
from finkb.fact_engine.metric_dict import load_metric_dict
from finkb.fact_engine.selector import load_manifest
from finkb.fact_engine.store import FactStore

docs = sorted(p for p in Path('data/parsed').iterdir()
              if p.is_dir() and re.match(r'^\d{6}_', p.name) and (p/'report.json').exists())
metrics = load_metric_dict(Path('config/metric_dict.yaml'))
store = FactStore('data/facts.duckdb')
client = LLMClient(LLMConfig.from_env())
print(ingest_documents(store, client, docs, metrics, load_manifest(), {}, workers=4))
store.close(); client.close()
PY
```

实测约 6 分钟（29 份、并发 4），成本按 token 计约几美分；端到端偶发读超时，需按文档补跑。

## 口径与局限

- 同一文档内同一 (期间, 指标) 只留一条：summary 先写，三表的重复项跳过（`duplicated` 是正常重叠）。
- 单位按「文档 + 表」记录：`unit_of_account` 是原文单位，`value_norm` 统一到指标默认单位（亿元/%/元每股）。
- 只抽合并报表，不抽母公司报表；同一页并列合并/母公司的情况仍未处理（DEVLOG §8 已记录）。
- 银行/保险/券商的专属科目尚未入字典（备忘见任务日志 T-56），因此这三类的行业指标覆盖不全。
- 金标只覆盖 3 家车企的 8 个指标，不是全量正确性证明；其余科目仍依赖恒等式与逐字反查。
