# eval — FinKB L2 评测集（方案 §10 第一步：15 题）

这个目录放的是 FinKB 的**评测集**：一组“像真实用户会问”的财务问题 + 标准答案 + 证据页码 + 可复算 SQL。
标准答案不靠人抄、也不靠大模型生成，而是由 `finkb/data/facts.duckdb`（L1 事实库）用 SQL **确定性算出来**，
再用 `check.py` 自动复算核对。这样后面做消融实验（Naive RAG / 混合 RAG / 三层栈）时，
数值题对错可以机器判定，不需要 LLM judge。

## 文件

| 文件 | 作用 |
|---|---|
| `gold_qa.json` | 15 道题：问题、标准答案、证据（doc_id + 物理页码 + 指标 + 报告期）、复算 SQL |
| `check.py` | 离线校验脚本：复算 SQL、核对答案、核对证据与物理页原文 |
| `README.md` | 本文件（字段说明 / 运行方法 / 扩充方法） |

## 题型分布（15 题）

| 类型 | 数量 | 说明 |
|---|---|---|
| `single` 单点题 | 8 | 一份报告、一个指标、一个报告期 |
| `cross_period` 跨期题 | 4 | 同一家公司两个报告期的差值或增速（方案 R2 口径） |
| `cross_company` 跨司题 | 3 | 三家公司同一年、同一口径指标的合计 / 极差 |

覆盖指标：营业收入、归母净利润、研发费用、经营活动现金流量净额、加权平均净资产收益率、
基本每股收益。覆盖公司：万科A、美的集团、长安汽车、比亚迪、招商银行、恒瑞医药、
贵州茅台、长江电力、中国神华、赛力斯、广汽集团。

### 15 题一览（答案单位见 JSON）

| id | 类型 | 问题概要 | 答案 | 证据（doc / 物理页） |
|---|---|---|---|---|
| S01 | single | 万科A 2025 年营业收入 | 2334.33 亿元 | 000002_2025_年报 / p10 |
| S02 | single | 贵州茅台 2025 年归母净利润 | 823.20 亿元 | 600519_2025_年报 / p6 |
| S03 | single | 恒瑞医药 2025 年研发费用 | 69.61 亿元 | 600276_2025_年报 / p135 |
| S04 | single | 中国神华 2025 年经营现金流净额 | 750.59 亿元 | 601088_2025_年报 / p8 |
| S05 | single | 长江电力 2025 年加权平均 ROE | 15.90 % | 600900_2025_年报 / p7 |
| S06 | single | 美的集团 2025 年基本每股收益 | 5.80 元/股 | 000333_2025_年报 / p9 |
| S07 | single | 广汽集团 2025 年归母净利润（负值） | -87.84 亿元 | 601238_2025_年报 / p11 |
| S08 | single | 招商银行 2025 年上半年营业收入 | 1699.69 亿元 | 600036_2025_半年报 / p7 |
| CP01 | cross_period | 比亚迪 2025 年营业收入比上年增加额 | 268.63 亿元 | 002594_2025_年报 / p11（2025、2024 两列） |
| CP02 | cross_period | 长安汽车 2025 年归母净利润比上年减少额 | 32.46 亿元 | 000625_2025_年报 / p6（2025、2024 两列） |
| CP03 | cross_period | 赛力斯 2026H1 营业收入比上年同期减少额 | 49.09 亿元 | 601127_2026_半年报 / p6（本期、上年同期两列） |
| CP04 | cross_period | 长安汽车 2026H1 营业收入同比增速 | -9.71 % | 000625_2026_半年报 / p6（本期、上年同期两列） |
| CC01 | cross_company | 美的+茅台+长电 2025 年营业收入合计 | 7115.32 亿元 | 三家 2025_年报 |
| CC02 | cross_company | 三家 2025 年归母净利润极差 | 478.17 亿元 | 三家 2025_年报 |
| CC03 | cross_company | 三家 2025 年加权平均 ROE 极差 | 16.63 个百分点 | 三家 2025_年报 |

## 运行方法

在仓库根目录执行：

```bash
.venv/bin/python eval/check.py
```

- 全对：打印每题 `PASS`，最后 `通过 15/15`，**退出码 0**；
- 有错：打印 `FAIL` 和具体差异（例如答案与 SQL 复算不一致、页码越界、原文对不上），**退出码 1**。

脚本是**离线**的：只读 `finkb/data/facts.duckdb` 和本地的 `finkb/data/parsed/`，不联网、不调 LLM、不写任何文件。
如果事实库正被其它模块以写模式占用（DuckDB 单写锁），脚本会自动等待最多 60 秒；仍打不开会给出明确错误并退出 1。

## `gold_qa.json` 字段说明

顶层：

| 字段 | 说明 |
|---|---|
| `schema_version` | 数据结构版本，当前为 1 |
| `description` | 评测集用途说明 |
| `generated_at` | 生成日期 |
| `fact_db` | 答案来源的事实库路径（仓库相对路径） |
| `answer_rule` | 答案口径：`answer` 是复算值四舍五入两位；`answer_exact` 是库内精确值 |
| `companies` | 股票代码 → 公司简称映射（校验问题文本里确实出现了公司名） |
| `questions` | 题目数组 |

每道题：

| 字段 | 说明 |
|---|---|
| `id` | 题号，全文件唯一（S=单点、CP=跨期、CC=跨司） |
| `type` | `single` / `cross_period` / `cross_company` |
| `question` | 题面（中文，像真实用户提问，必须含公司简称） |
| `answer` | 标准答案，**四舍五入到 2 位小数**（与题面“保留两位小数”一致） |
| `answer_exact` | 事实库精确值（`value_norm` 或 SQL 计算结果，未四舍五入） |
| `unit` | 答案单位：`亿元` / `%` / `个百分点` / `元/股` |
| `evidence_unit` | 可选，证据 fact 的单位（缺省等于 `unit`）。派生的比率题（如 CP04 增速，答案是 %、输入是亿元）用它 |
| `metric_id` | 指标口径 id，对应 `finkb/config/metric_dict.yaml`（如 `revenue`、`net_profit`） |
| `companies` | 涉及公司代码数组 |
| `periods` | 涉及报告期数组（`2025` / `2024` / `2025H1` / `2026H1`） |
| `caliber` | 报表口径，当前全部为 `合并报表` |
| `evidence` | 证据数组，每条 `{doc_id, page, metric_id, period}`：解析产物目录名 + **物理页码** + 指标 + 报告期（自然键，不用事实库行号，见坑-81） |
| `sql` | 复算这道题的只读 SQL，必须返回 1 行 1 列数值 |
| `note` | 人工备注：该页表名、列位置、跨期口径等 |

## `check.py` 校验了什么

1. **结构**：字段齐全、id 唯一、类型分布恰好 `single=8 / cross_period=4 / cross_company=3`、共 15 题。
2. **答案可复算**：每题 SQL 在事实库（只读）上执行，必须返回 1 行 1 列数值，且
   - `|SQL 结果 - answer_exact| <= 1e-6`；
   - SQL 结果四舍五入两位（ROUND_HALF_UP）必须等于 `answer`。
3. **证据真实**：每条 `evidence` 按 `doc_id + page + metric_id + period`（合并报表）在 `facts` 表唯一命中，且 `source_doc` / `page_num` /
   `metric_id` / `company` / `period` / `caliber` / 单位与题目一致。
4. **页码可核对**：`page` 必须在 `finkb/data/parsed/<doc_id>/report.json` 的页数范围内，
   `page_anchored.md` 中存在该页的 META，并且该 fact 的 `raw_text_matched`（原表里的数字字符串）
   确实出现在这一页的文本里。这一步保证“答案能从 PDF 对应页找到”，而不是库里一条孤立的数。
5. **口径约束（方案 R2，已锁定）**：
   - 跨期题只允许两种来源：① 2025 年报内同期/上年列（`<代码>_2025_年报`，`period` ∈ {2025, 2024}）；
     ② 2025 vs 2026 半年报（`<代码>_2026_半年报`，`period` ∈ {2026H1, 2025H1}）。
     **显式禁止** 2023/2024 年报；
   - 跨司题必须是 3 条证据、3 家不同公司、全部 2025 年报、同一 `metric_id`。

> 自检小技巧：把 `answer` 或 `page` 故意改错再运行，脚本应退出码 1 并指出差异；
> 改回后退出码 0。两条负例路径已实测。

## 如何扩充题目（15 → 50）

1. **先查数据，再出题**：用只读连接在事实库里找真实存在的 fact，确认指标、报告期、页码：

   ```bash
   .venv/bin/python -c "
   import duckdb
   con = duckdb.connect('finkb/data/facts.duckdb', read_only=True)
   print(con.execute(\"SELECT fact_id, company, period, metric_id, value_norm, norm_unit, source_doc, page_num FROM facts WHERE metric_id='revenue' AND period='2025' ORDER BY company\").fetchall())
   "
   ```

2. **写 SQL 复算**：单点题一般是 `SELECT value_norm FROM facts WHERE source_doc = ... AND page_num = ... AND metric_id = ... AND period = ... AND caliber = '合并报表'`；
   跨期/跨司用差、比、SUM、MAX-MIN 组合（见现有题）。
3. **填证据**：`doc_id` 取 fact 的 `source_doc`，`page` 取 `page_num`（物理页码），
   并打开 `finkb/data/parsed/<doc_id>/page_anchored.md` 看该页 META 与表格文字，确认行标签没取错。
4. **跑校验**：`answer` 先填 `answer_exact` 的两位小数，运行 `check.py`，按报错修到全过。
5. **同步预期数量**：扩题后要改 `check.py` 顶部的 `EXPECTED_TYPE_COUNTS` 和 `EXPECTED_TOTAL`，
   否则脚本会因“题目数与预期不符”退出 1——这是有意设计的，防止评测集被悄悄改小。

## 边界与注意

- 当前只用了 **2025 年报** 和 **2025/2026 半年报**；2026Q1 等报告期没有入题（样本少、口径不同）。
- 原表单位可能是万元/百万元，事实库已归一成 `norm_unit`（亿元 / % / 元/股）；题目单位以 `norm_unit` 为准。
- 答案按“四舍五入两位”判定；如果以后题面改成“保留一位/整数”，需要同步调整 `answer` 和脚本的取整规则。
- `check.py` 只证明“答案与事实库/原页一致”，不评价题目好坏；新题仍建议人工过一眼题面是否像真实提问。

## 合成子系统评测（15 题，需真实模型）

`eval/synth_qa.json` + `eval/check_synth.py` 评测“模式 A：项目内受约束生成”，与 `check_qa.py`
（抽取式回答）互补：

```bash
# 需要先配置 FINKB_LLM_SYNTH=1 与 FINKB_LLM_SYNTH_API_KEY（可覆盖 BASE_URL/MODEL）
.venv/bin/python eval/check_synth.py
```

- 正例（10 文档 + 2 本体）要求：`synthesis.used=true`、V1–V7 全过、答案含 `[Ref: …#P页码]`、长度 ≥20；
- 负例（3 题）要求：拒答且不调用模型；
- 退出码：0=15/15，1=有失败，2=未配置模型（跳过）；明细写 `finkb/data/_eval/synth_results.json`；
- 该脚本**不进** `scripts/check.sh` 的离线门禁（需要网络与模型 key），只用于人工评测与抽检。
- 2026-11-08 基线：连续两轮 **15/15**（`synthesis_used=12`、`refused=3`）。此前 13/15 的两题
  （S04 神华煤炭产销、S08 长城海外）是“引用写在段末”导致 V4 判句子缺引用；现已按 ADR-073
  允许段落引用继承（句子必须能在所引原文里找到），并在 prompt 中澄清“facts 为空时用片段作答”。

## 多轮上下文验收（12 组宿主模拟，离线）

`eval/context_qa.json` + `eval/check_context.py` 验收 P5 的“会话归宿主、语义归项目”契约：
每轮把上一轮的 `resolved_slots`（或只有 `previous_question`）传回 `pipeline.ask`，
检查槽位继承/覆盖、补全问题、澄清输出，以及不带 context 时行为不变。

```bash
.venv/bin/python eval/check_context.py            # 12 组 24 轮
.venv/bin/python eval/check_context.py --case C09 # 只跑一组
```

- 覆盖：换期间/换公司/换指标、三连追问、换话题、澄清后补槽位、文档追问、
  本体定义类追问、附注追问、关系边追问、`previous_question` 兜底、无 context 兼容；
- 判定：`tier`、`refused`、`inherited`/`overridden`、`resolved_question` 关键词、
  `resolved_slots`、`clarify.missing`；
- 离线、确定性，已加入 `scripts/check.sh` 门禁；退出码 0=全部通过，1=有失败。

## 跨期对比验收（6 道，离线）

`eval/compare_qa.json` + `eval/check_compare.py` 验收 P7 的结构化对比能力：
同一公司、同一指标、多个期间并排返回数值/单位/引用，数字只来自事实库 SQL。

```bash
.venv/bin/python eval/check_compare.py            # 6 道
.venv/bin/python eval/check_compare.py --case P01 # 只跑一道
```

- 覆盖：区间解析（`2023-2025`）、三期间、半年报与年报并排、两期间、
  单期间拒答、缺指标拒答；
- 判定：`refused`、`data.periods` 顺序、行数、数值/账本/引用是否齐全、
  文案关键词，以及 `schema_version`/`disclaimer`；
- 离线、确定性，已加入 `scripts/check.sh` 门禁；退出码 0=6/6，1=有失败。
