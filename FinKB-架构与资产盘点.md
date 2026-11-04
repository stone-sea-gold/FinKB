# FinKB 项目架构与资产盘点

> 扫描时间：2026-11-03 19:20（Asia/Shanghai）
> 代码状态：`main` 分支 HEAD `bd5734e`（89 次提交）；工作树有 3 个未提交改动（`eval/README.md`、`eval/check.py`、`eval/gold_qa.json`，金标题自然键改造）。
> 本文目的：给出一张"现在到底有什么、各部分怎么连、验证到什么程度、缺什么"的全量底图，作为与外来计划（Neuro-Symbolic GraphRAG 蓝图）做取舍的事实依据。
> 权威性说明：状态以 `finkb/DEVLOG.md` 为准；本文数字均来自 2026-11-03 的实测或文档逐项核对，凡引用文档数字的地方会注明。

---

## 0. 一页纸速览

| 维度 | 现状 |
|---|---|
| 项目定位 | 单人专业分析工作台 / 作品集 Demo-First 导向（方案 v2 §5 R1） |
| 架构基线 | 「L0 凭证 → L1 事实 → L2 知识 → L3 交互」四层知识栈 + L0.5 本体层（v2.3） |
| 核心不变量 | **数字只由 SQL 计算；LLM 只做语义对齐与查询/叙述**（ADR-001、R4） |
| 代码规模 | `src/` 10,704 行（7 个包 + cli）、`tests/` 6,351 行 / **406 项离线测试**、`tools/` 3,298 行、`eval/check.py` 437 行 |
| 语料 | **29 份 / 14 家公司 / 4,791 页 / 10 类版式**；5,117 张物理表 → 6,130 张逻辑表 |
| L0 质量 | 有表头 93.5%；守恒律 **359/359**；数字回查 36,026 抽样仅 6 个查不到（99.98%） |
| L1 事实 | **facts 1,830 条 / 36 指标 / 0 未匹配**；`segment_facts` 255；`relations` 621；`conflicts` 1 |
| L2 知识 | **153 页 active**（29 摘要 + 107 探针 + 14 实体 + 3 概念）；claims **1,578 条**；FTS5 索引 153 页 |
| L0.5 本体 | **62 概念 / 71 关系 / 10 约束（5 error / 5 warn）**；3 维度 / 58 成员；六跳链条已跑通 |
| 评测 | 15 题离线复算 **15/15**；金标事实 **72/72**；语料基线 29 份；lint 0 错 0 警 |
| 未做 | L3 交互层与检索路由、向量检索、jieba 分词、06–08 探针、跨期对比页、编译幂等与 token 成本 |

**三句话判断：**

1. **地基比方案文档更成熟。** L0 解析、L1 事实、L2 知识、L0.5 本体四层都有产物、有门禁、有实测数字；任何后续路线都能复用其中约 **70–80%**（解析产物、事实库、知识页、claims、评测、406 项测试）。
2. **缺口集中在"检索与交互"。** 现在只有 FTS5 词面检索（153 页知识页），没有向量/语义通道、没有查询路由、没有前端，也没有"用文字回答问题"的能力；外来计划与 RAG 设计真正补的是这一块。
3. **外来计划的成本大头不在数据层，而在新增运行时与规模假设。** OWL 推理机、Neo4j、Qdrant、LlamaIndex，以及"100+ 年报 / 120 科目 / 全市场"都超出当前资产与定位；且与 4 条已记录决策（ADR-002/003/006/052）直接冲突。

---

## 1. 项目本意与版本脉络

### 1.1 目标与定位（方案 v2 §1、§5）

- 目标：把 A 股年报 PDF 变成**可溯源、可积累、可审计、可维护**的财报知识库；检索能力（RAG）+ 知识沉淀（LLM Wiki）结合。
- 用户定位：**单人专业分析工作台，作品集 / Demo-First 导向**；不做多租户鉴权、不做多人协同、不做线上审核流。
- 语料范围：严格锁定 A 股标准年报/季报 PDF（不引研报、公告、新闻）。
- 三条硬约束（贯穿全部代码）：
  1. **数字只由 SQL 计算**，LLM 不得产出数值（ADR-001 / R4）；
  2. 每一条数字/定性主张必须能指回事实行或 PDF 页码（`[Ref: doc#P{N}]`）；
  3. 所有测试离线可跑，不依赖网络与 API key。

### 1.2 版本演进

| 版本 | 状态 | 核心内容 |
|---|---|---|
| v1 | ❌ 已推倒 | 把数值与定性知识混在一层、让 LLM 直接生成带数字的 Markdown，算术不可信（v2 §3） |
| **v2.0 → v2.3** | ✅ **当前基线** | 四层知识栈 + 工程补丁 P1–P9；v2.3 增补 L0.5 本体层（§7.5） |
| v3「轻解析路线」 | 📄 文档已写（397 行），**未实施** | 三分法：解析外包 Docling/MinerU、数字改走结构化接口、L2/L3 沿用；主张"PDF 只管说了什么，接口管是多少"；指标覆盖约 39%，但绕过 80% 的解析难度 |
| 外来计划 | 📋 评估中（未入库） | 《基于本体驱动的 A 股年报智能问答与推理知识库（Neuro-Symbolic GraphRAG）》：五层架构、Owlready2/Pellet/SWRL、Neo4j、Qdrant、LlamaIndex、8 周计划 |

v2/v3 的关系：v3 是 v2 的**对照实验分支**，不是替代方案；两套并行会重复投入，需二选一或明确分工（文档层面目前 v2 是基线，v3 仍是待决策）。

### 1.3 文档分工

| 文档 | 行数 | 回答什么问题 |
|---|---|---|
| `FinKB-方案v2.md` | 1,252 | 架构基线：为什么这样分层、补丁 P1–P9、路线图与验收 |
| `FinKB-方案v3-轻解析路线.md` | 397 | 对照实验：能否用接口替代自研解析、工时能省多少 |
| `FinKB-科目对照表.md` | 112 | 指标字典与口径/别名的人工对照表 |
| `finkb/DEVLOG.md` | 2,939 | **唯一权威状态**：ADR（56 条）、坑（80+）、未验证项、变更记录 |
| `finkb/任务日志.md` | 995 | 实现流水（T-NN）、文件地图、未完成与阻塞 |
| `finkb/PDF解析细节.md` | 743 | 解析/切分的实测问题集（坑的依据） |
| `finkb/AGENTS.md` | 97 | 接手约束：门禁、汇报纪律、硬性禁止项 |
| `finkb/README.md` | 318 | 怎么用（命令、产物、已知限制） |

> 注意：README 与任务日志里的数字**明显滞后**（见 §9），判断状态时以 DEVLOG §2 与本文实测为准。

---

## 2. 分层架构：设计 vs 实现

### 2.1 分层总图

```
┌──────────────────────────────────────────────────────────────────────┐
│ L3 交互层（未实现）                                                    │
│   三栏工作台 / 路由徽标 / 三视图 / [Ref] 跳页 / 前端                   │
│   查询路由：数值→SQL ｜ 已知知识→Wiki 直读 ｜ 长尾文字→混合检索        │
├──────────────────────────────────────────────────────────────────────┤
│ L2 知识层（已实现）                                                    │
│   Markdown Wiki（153 页 active）+ frontmatter 代码生成 + 类型化链接     │
│   claims 数字账本 1,578 条 + lint R1–R10 + _draft 隔离                 │
│   检索：SQLite FTS5 trigram + 指标别名词表 + BM25                     │
├──────────────────────────────────────────────────────────────────────┤
│ L1 事实层（已实现）                                                    │
│   DuckDB：facts 1,830 / segment_facts 255 / relations 621 / conflicts 1│
│   两段式抽取：LLM 只摘录 Observation → Python 逐字核对 + 单位归一       │
│   派生比率只走命名 SQL（wiki_queries.yaml）                            │
├──────────────────────────────────────────────────────────────────────┤
│ L0 凭证层（已实现）                                                    │
│   PDF 原文 + 1-based 物理页码锚 + 页眉页脚清洗 + 跨页表格缝合           │
│   物理表 → 逻辑表切分（表头继承）+ 守恒律/回查/基线门禁                 │
├──────────────────────────────────────────────────────────────────────┤
│ L0.5 本体层（已实现，仅校验不推理）                                     │
│   四层概念栈（L0 元概念 / L1 FIBO 骨架 / L2 CAS 科目 / L3 派生指标）    │
│   62 概念 / 71 关系 / 10 约束 / 3 维度 58 成员；六跳可追溯链条           │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.2 每层"谁生成、怎么验证、出错后果"

| 层 | 谁生成 | 怎么验证 | 出错后果 |
|---|---|---|---|
| L0 | 无人"生成"（原始 PDF + 确定性解析） | 文件 hash、页数、守恒律、数字回查 PDF | 被审计/基线立刻拦住 |
| L1 | LLM 摘录（只选单元格）+ 代码校验归一 | 单元测试 + 逐字反查 + 金标事实集 | 抽取率下降、金标变红，不会静默污染 |
| L2 | LLM 编译 + 人策展 | lint（锚点/claim 对账/断链/数字覆盖）+ draft 隔离 | 停留 `_draft`，不污染 active |
| L0.5 | YAML 单一真相源（工具生成，可手改） | 约束求值 + 链条体检 audit + CAS 元素存在性测试 | 约束报错/断点非零，`check.sh` 失败 |
| L3 | — | 评测集（规划中） | — |

### 2.3 关键设计取向（与常见 RAG 项目的差异）

1. **FTS 为主、向量为辅**（ADR-002）：BM25 命中是"词"，可解释；科目/代码精确匹配强于向量。
2. **不做 GraphRAG**（ADR-003）：Markdown 双向链接本身是图；多跳关系从 `relations` 边表确定性生成，不让 LLM 抽三元组。
3. **本体层只校验不推导**（ADR-052）：四层概念栈落 DuckDB，做"可追溯 + 约束体检"，不引 OWL/RDF 运行时、不引 reasoner。
4. **每个数字都有账本**（ADR-044）：L2 正文数字只写 `{{cN}}` 占位符，渲染时由 claims 表算；三类 claim（fact / segment / query）各自可重算比对。

---

## 3. 代码地图

### 3.1 仓库顶层

```
reporting/
├── FinKB-方案v2.md / v3 / 科目对照表.md    # 架构与方案文档
├── CLAUDE.md                               # 对话/协作约定
├── raw/                                    # 原始 PDF + manifest.csv（cninfo 来源）
├── eval/                                   # 15 题金标题 + 离线复算脚本
└── finkb/                                  # 可运行项目
    ├── src/finkb/                          # 7 个包 + cli，10,704 行
    ├── tests/                              # 406 项离线测试，6,351 行
    ├── tools/                              # 13 个审计/校验工具，3,298 行
    ├── config/                             # 指标字典/本体/范围/命名 SQL/基线/金标
    ├── data/                               # parsed / facts.duckdb / wiki.duckdb / _search / snapshots
    ├── wiki_vault/                         # L2 知识页（153 active + index/log + reports）
    └── scripts/check.sh                    # 一条命令六道门禁
```

### 3.2 `src/finkb` 包职责（行数为 2026-11-03 实测）

| 包 | 文件（行数） | 职责 | 不该做什么 |
|---|---|---|---|
| `common/` | `models.py` 335 | IR：`DocMeta/Block/Page/TableBlock/StitchedTable/LogicalTable` | 不含解析或业务逻辑 |
| `parser/` | `mineru_client` 1,087 / `markdown_blocks` 772 / `section_splitter` 616 / `table_stitcher` 332 / `noise_filter` 257 / `pipeline` 77（合计 3,141） | 唯一接触外部格式的适配层；页眉页脚清洗；跨页缝合；物理表→逻辑表切分 | 不承载业务规则、不改写单元格 |
| `fact_engine/` | `selector` 540 / `store` 424 / `ingest` 345 / `segment` 344 / `extractor` 300 / `relations` 168 / `verifier` 161 / `metric_dict` 134 / `basis` 71 / `conflicts` 68（合计 2,555） | 选表 → LLM 摘 Observation → 逐字核对入库；分部/关系/冲突；`facts_dedup` 视图 | 不算数、不猜缺失值、不做语义推断 |
| `wiki/` | `compiler` 536 / `claims` 282 / `vault` 231 / `frontmatter` 163 / `concepts` 197 / `entities` 139（合计 1,548） | L2 编译（1 摘要 + 5 探针）、claim 账本、原子写/锁、实体页、概念页 | 不直接写正文数字、不自算差值 |
| `lint/` | `rules.py` 481 | R1–R10 规则 + 单页转正前 `page_issues` | 不自动改内容（除 `--fix` 断链） |
| `search/` | `index.py` 217 | FTS5 索引：trigram + 别名扩展 + bm25 | 不改页面、不调 LLM |
| `ontology/` | `chain` 342 / `cli` 299 / `model` 275 / `expr` 222 / `check` 194 / `audit` 164 / `loader` 131 / `derive` 82 / `schema` 74（合计 1,783） | 四层概念栈、六跳链条、约束求值、L3 派生、维度 | 只校验不推导；不改事实 |
| `cli.py` | 330 | `finkb parse` 入口、`.env` 注入、产物落盘 | 不做解析算法 |

### 3.3 依赖方向（单向，禁止反向）

```
pipeline → noise_filter / table_stitcher / markdown_blocks → models
mineru_client → markdown_blocks / models
fact_engine → models（只读 report.json，不 import parser 内部）
wiki/compiler → fact_engine（读 facts/segment_facts）+ wiki/{frontmatter,claims,vault} + lint/rules
wiki/entities → fact_engine（只读 relations）+ wiki/vault
wiki/concepts → fact_engine + wiki/{compiler,claims,vault} + lint/rules
search/index → wiki/vault（只读页面）
ontology → 事实库/知识库（只读 + 独立表）
```

`noise_filter` 与 `table_stitcher` **不得** import `mineru_client`——这是格式解耦的硬边界（ADR-008）。

### 3.4 可用命令与工具

| 入口 | 命令 | 用途 |
|---|---|---|
| `finkb` CLI | `finkb parse <pdf> [--auto-split/--pages/--offline/--reuse-content-list]` | PDF → 页码锚定 MD + 逻辑表 |
| `wiki` | `python -m finkb.wiki init / index / compile --doc ...` | 知识页骨架/索引/编译（compile 需 LLM key） |
| `lint` | `python -m finkb.lint` | 十条规则，eport 写 `wiki_vault/reports/`，有 error 退出 1 |
| `search` | `python -m finkb.search "查询词" [--rebuild/--limit]` | FTS5 检索（索引不存在时自动建） |
| `ontology` | `python -m finkb.ontology load/tree/audit/chain/check/derive/concept/dims` | 本体灌库/链条/约束/派生/维度 |
| `eval` | `python eval/check.py` | 15 题离线复算（不联网、不调 LLM） |
| `check.sh` | `finkb/scripts/check.sh` | 测试 + 语料守恒律/基线 + 本体 audit + 每文档事实数 + 金标 |
| `tools/audit.py` | 13 个子命令：`corpus/baseline/invariants/verify-cells/recall/page-spans/numbers/rowspan/headers/snapshot/diff/manual-check/unit-map` | 解析与数字审计（只读） |
| 其它 tools | `build_ontology.py`、`facts_baseline.py`、`gold_check.py`、`kill9_check.py`、`section_split_review.py` | 本体生成 / 每文档事实数门禁 / 金标 / 崩溃一致性 / 分节审核 |

---

## 4. 数据资产清单（本文的核心）

### 4.1 语料

| 项 | 值 |
|---|---|
| 文档数 | 29 份（`finkb/data/parsed/` 下 29 个目录，另有 2 个对比产物 `_vlm_P43-44`、`_prev_pipeline_P43-44` 应排除） |
| 公司数 | 14 家：比亚迪、赛力斯、长安、长城、广汽、招商银行、中国平安、中信证券、万科 A、中国神华、贵州茅台、美的集团、恒瑞医药、长江电力 |
| 页数 | 4,791 页 |
| 版式 | 10 类（整车、银行、保险、券商、地产、能源、白酒、家电、医药、电力） |
| 报告类型 | 年报、半年报、一季报、三季报（3 家整车各 6 份，时间跨 2023–2026Q1） |
| 文本覆盖 | `page_anchored.md` 覆盖 4,355 页；**436 页（9%）在解析产物中没有任何可检索文本**（空表格块，`tables/` 也无内容；不是读漏） |
| 内容构成 | 表格 5,146 张、3,514,921 字符（约 66%）；纯文本约 179 万字符 |
| 页字符分布 | mean 374 / p50 177 / p90 1,039 / p99 1,706 / max 2,541；>1,500 字符的页 111 个 |

### 4.2 L0 解析产物（`data/parsed/<doc_id>/`，890 MB）

| 文件 | 内容 |
|---|---|
| `content_list.json` | MinerU 原始输出（在线模式） |
| `page_anchored.md` | 页码锚定 Markdown：每块前 `<!-- META: {"doc","page","section"} -->`，表格已转管道表 |
| `report.json` | 统计、噪音签名、缝合审计记录 |
| `logical_tables/` | 逻辑表（L1 的直接输入） |
| `tables/` | 每张逻辑表一个 Markdown 文件，便于人工核对 |

实测质量（DEVLOG §2/§8 与 `config/corpus-baseline.json`）：

| 指标 | 结果 |
|---|---|
| 物理表 → 逻辑表 | 5,117 → **6,130** |
| 有表头 | 5,729（**93.5%**）；裸表 401（6.5%，其中约一半是键值表本无列名） |
| 跨页缝合 | 28 张；列数不一致拒绝 21 条（逐条带原因，不猜对齐） |
| 守恒律 | **359/359**（年报 16 条 / 半年报 13 条 / 季报 10 条） |
| 数值回查 | 36,026 个抽样中 6 个查不到（**99.98%**，均为 MinerU 粘格） |
| 原文数字召回 | 18 份实测 19,782 个数字全部可解释（205 个"对不上"逐个核实都是 PDF 文本层粘连） |

### 4.3 L1 事实库（`data/facts.duckdb`，13 MB）

| 表 | 行数 | 说明 |
|---|---|---|
| `facts` | **1,830** | 唯一事实库；21 列（company/period/metric_id/value_norm/caliber/source_doc/page_num/...） |
| `facts_dedup` | 1,549（视图） | 去重口径（ADR-007；唯一键含 `source_doc`） |
| `segment_facts` | 255 | 分产品 81 / 分行业 87 / 分地区 87；全部带 `member_id` |
| `relations` | 621 | 担保 423 / 股东 122 / 子公司 39 / 关联方 37 |
| `conflicts` | 1 | 002594 2025Q1 `eps_basic` 3.12 vs 1.0391（两份文档口径冲突） |
| `unmatched_pool` | 10 | 未匹配行池 |
| `concepts` / `concept_relations` | 62 / 71 | L0.5 本体概念与关系 |
| `ontology_constraints` | 10 | 5 error / 5 warn |
| `dimensions` / `dimension_members` | 3 / 58 | 分产品/分行业/分地区；成员带 parent_id/aliases |
| `metric_dict` | **0** | 空表；指标字典的真相源是 `config/metric_dict.yaml`（36 指标/80 别名） |
| `meta` | 8 | 字典版本/hash 等 |

事实分布（按公司，合计 1,830）：

| 公司 | 条数 | 公司 | 条数 |
|---|---|---|---|
| 002594 比亚迪 | 369 | 600030 中信证券 | 55 |
| 601127 赛力斯 | 363 | 600036 招商银行 | 59 |
| 000625 长安汽车 | 365 | 600276 恒瑞医药 | 65 |
| 601633 长城汽车 | 100 | 600519 贵州茅台 | 69 |
| 601238 广汽集团 | 68 | 600900 长江电力 | 71 |
| 601088 中国神华 | 67 | 601318 中国平安 | 51 |
| 000002 万科 A | 56 | 000333 美的集团 | 72 |

其它关键口径：

- 期间：15 种（2021 / 2022 / 2023 / 2023H1 / 2024 / 2024H1 / 2024Q1 / 2024Q1–Q3 / 2025 / 2025H1 / 2025Q1 / 2025Q1–Q3 / 2025Q3 / 2026H1 / 2026Q1）。
- **caliber 全部是「合并报表」**（1,830/1,830），没有母公司口径。
- `metric_id` 36 个，全部是**财报直接披露的报表科目**（revenue / net_profit / ... / accounts_receivable），没有销量、单价、研发人员等业务指标。
- `match_level`：exact 1,822 / repaired 8。
- 抽取方式：`selector` 选表 → `extractor` 调 LLM 只输出 Observation（单元格坐标+值）→ `verifier` 回单元格逐字核对 + 单位归一 → `ingest` 写库；**LLM 不产出一个数字**。

### 4.4 L1 结构数据的质量细节

- `segment_facts` 255 条全部落到 `dimension_members`；成员靠报表行名抽取，新增公司零配置（ADR-055）。
- `relations` 621 条：**src_type 全部是 company，没有 company→company 边**；363 个不同 dst；有边的只有 5 家公司（002594 245 / 000333 160 / 000002 142 / 000625 61 / 601088 13）；15 条把表格行标签当实体（如"担保总额超过净资产 50% 部分的金额(F)"）；**无实体消歧**（同一实体跨文档重复）。
- `conflicts` 只做 facts 级同科目跨文档数值不一致；文本语义矛盾未做（ADR-046）。

### 4.5 L2 知识库（`wiki_vault/` + `data/wiki.duckdb` + `data/_search/`）

| 项 | 值 |
|---|---|
| active 页 | **153** = 摘要 29 + 探针 107 + 实体 14 + 概念 3 |
| `_draft` | 0（当前无草稿） |
| 正文规模 | 约 **305,962 字符**（摘要 72,990 / 探针 195,454 / 实体 27,277 / 概念 10,241）；最大页面约 9.7K 字符 |
| claims 账本 | **1,578 条**：fact 1,118 / segment 283 / query 177（`data/wiki.duckdb`） |
| 探针主题 | 5 类：01 核心财务 / 02 分产品结构 / 03 研发投入 / 04 海外收入 / 05 现金流与资本结构 |
| 页面结构 | frontmatter（type/title/company/period/sources/tags/status/updated_at）+ 正文 + `[Ref: doc#P{N}]` 页锚 + `[^cN]` 脚注 |
| 实体页 | 14 张，由 `relations` 生成（无数字、无 LLM），列子公司/主要股东并带页锚 |
| 概念页 | 3 张跨公司对比，claim 来自多份文档、走同一账本与门禁 |
| 检索索引 | `data/_search/search.db` 2.5 MB；`pages` 153 行 + FTS5 虚表 `pages_fts`（trigram）；BM25 标题权重 10；draft 排 active 之后 |
| lint | R1–R10，当前 **0 错 0 警**（报告写 `wiki_vault/reports/`） |

已知的检索边界（写进 README「已知限制」）：

- trigram 对 3 字以上词有效；**2 字词必须靠 `metric_dict.aliases` 别名扩展**才稳。
- 没有向量兜底、没有检索路由（属 L3）。
- 索引只覆盖 153 页知识页，**不索引 L0 原文**——所以"文字类问题"目前只能在知识页里找，找不到时无兜底。

### 4.6 L0.5 本体层

| 项 | 值 |
|---|---|
| 概念 | 62：L0 元概念 7 / L1 科目骨架 13 / L2 国标科目 36 / L3 派生指标 6 |
| 关系 | 71：`is_a` 55 / `derived_from` 14（6 个公式） / `disjoint_with` 1 / `part_of` 1 |
| 约束 | 10：acct_equation 3 / range 5 / unit_check 1 / required_period 1；severity 5 error / 5 warn |
| 维度 | 3 种（product / industry / region）/ 58 个成员 |
| 标准对齐 | 依据 CAS 2015（4,317 个元素）；L2 的 36 个概念中 33 个有标准依据（18 个 `cas_*`、15 个 `ifrs-full_*`），3 个 `basis=self`（扣非净利 / 租赁负债 / 加权 ROE） |
| 链条体检 | 去重事实 1,549：①概念登记 0 / ②祖先链 0 / ③约束覆盖 0 / ④证据字段 0（四条结构性断点全 0，`check.sh` 卡死）；⑤未被页面引用 1,059（覆盖率）；⑥L3 派生缺参数 138 组（覆盖率） |
| 约束体检 | 10 条 × 73 组（公司,期间）→ **error 0 / warn 0** |
| 六跳链条 | 概念 → 标准定位 → 祖先链 → 约束 → 事实 → 证据（页码/表号/原文命中）→ 页面引用，真实库零断点 |

> 关键边界：本体层**只做校验，不做推理**（ADR-052）：没有 OWL/RDF 运行时、没有 reasoner、没有因果/驱动边；`derived_from` 只表达公式依赖（杜邦式），不表达因果。

### 4.7 评测与基线资产

| 资产 | 位置/规模 | 用途 |
|---|---|---|
| 金标题 | `eval/gold_qa.json`，15 题（single 8 / cross_period 4 / cross_company 3） | 题面 + 标准答案 + 证据（doc/page/metric/period 自然键）+ 参考 SQL |
| 离线复算 | `eval/check.py`（437 行） | 重跑 SQL、比对答案、核对证据锚点/原文命中/页码范围；**当前 15/15 PASS** |
| 金标事实集 | `config/gold_facts.yaml`，72 条（3 家车企 × 8 指标 × 3 年，人工从 PDF 抄录） | 抽取正确性的独立对照；**72/72** |
| 语料基线 | `config/corpus-baseline.json`，29 文档 × 5 指标（pages/logical/header/invariants/verify） | 解析退步检测，`audit.py corpus --baseline` |
| 每文档事实数门禁 | `tools/facts_baseline.py` | 防"聚合总数增长掩盖单文档归零"（坑-80） |
| 故障注入 | `finkb/.tmp/accept_faults.py`（脚本在 .tmp） | 假锚点/断链/无源数字/截断页 4/4 被拦 |
| 崩溃一致性 | `tools/kill9_check.py` | 原子写窗口内 SIGKILL，页面仍是完整旧版 |

### 4.8 磁盘与规模汇总

| 路径 | 大小 | 说明 |
|---|---|---|
| `finkb/data/parsed/` | 890 MB | 29 份解析产物（不进 git） |
| `finkb/data/facts.duckdb` | 13 MB | L1 事实 + 本体 + 结构表 |
| `finkb/data/wiki.duckdb` | 2.3 MB | claims 账本 |
| `finkb/data/_search/` | 2.5 MB | FTS5 索引 |
| `finkb/data/snapshots/` | 8.9 MB | 判据改动前后的快照（进 git，用于对照） |
| `finkb/data/reports/` | 1.1 MB | 审计报告 |
| `finkb/wiki_vault/` | 1.2 MB（含 reports） | 153 页知识页 + index/log + 30 份审计报告 |

已测算的向量规模（做语义检索时用）：按"页内文本合并到约 300 字、表格 1,200 字切并重复表头"模拟，全语料约 **16,177 块**（9,726 文本 + 6,451 表格），块长 mean 342 / p50 263 / p90 967；1024 维 float32 约 **63 MB**——现有 numpy/DuckDB 足以暴力检索，不需要向量数据库。

---

## 5. 流水线与数据契约

### 5.1 L0：PDF → 逻辑表

```
PDF
 ├─[生产] MinerU 在线 API（vlm 后端；>200 页自动分批）
 │     content_list.json
 │        │ content_list_to_pages() ← 唯一 MinerU 格式适配层
 └─[兜底] pypdf 逐页（无表格结构，仅链路验证）
          │ OfflinePdfParser
          ▼
   Page / Block / TableBlock（本项目 IR，层内唯一真相）
          │ strip_running_noise()  统计签名打标噪音（只打标不删除）
          ▼
   stitch_tables()  四条件缝合（页末表 + 次页页首表 + 列数一致 + 表头性质）
          ▼
   StitchedTable
          │ section_splitter.py  物理表 → 逻辑表（多层表头/空首格/期间标签/表头继承）
          ▼
   LogicalTable（含 page_start/page_end/header/rows/section_path/kind）
```

强制顺序：**先清洗、后缝合**；清洗与缝合并不知道数据来自哪个解析器（ADR-008）。落盘形态是 `page_anchored.md`（每块一条 `META` 注释），既是人工核对入口，也是 L2 页锚的物理依据。

### 5.2 L1：LogicalTable → facts

```
selector.py        父表分组；按内容锚点挑"主要会计数据 + 三表"；期间映射
   ▼
extractor.py       LLM 只输出 Observation：行/列坐标 + 原文值（不计算、不换算）
   ▼
verifier.py        回单元格逐字核对；单位归一（亿元/百万元/元）；match_level=exact/repaired
   ▼
ingest.py          按文档/按页推断单位；并发抽取、串行写库；接 segment/relations/conflicts
   ▼
store.py           DuckDB facts / facts_dedup 视图 / segment_facts / relations / conflicts
```

配套：
- `segment.py`：分产品/分行业/分地区结构表 → `segment_facts`（确定性解析、单位反推、对比期剔除）。
- `relations.py`：四类结构化关系表（子公司/关联方/股东/担保）→ `relations`。
- `conflicts.py`：同公司+科目+期间+口径的跨文档数值冲突 → `conflicts`。
- `basis.py`：反算判定率值是否年化（`period_basis`，ADR-056）。

### 5.3 L2：facts/relations → 知识页

```
wiki/compiler.py   每份文档：1 摘要 + 5 探针（结构页/海外页无数据时跳过）
   ▼
claims.py          正文数字写成 {{cN}} 占位符 → 生成 claim（fact/segment/query）+ 脚注
   ▼
frontmatter.py     由代码拼装（LLM 只输出正文，ADR-044/P7）
   ▼
vault.py           原子写 + 文件锁 + index 重算 + scope
   ▼
lint/rules.py      R1–R10：断链/孤儿/登记缺失/必填字段/锚点存在/claim 对账/数字覆盖/定性页锚
   ▼
wiki_vault/        转正后进入 active；失败留 _draft
```

### 5.4 L0.5：指标字典 + CAS → 本体

```
config/metric_dict.yaml（36 指标/80 别名）
        + CAS 2015 元素表（4,317 个，label linkbase）
config/ontology.yaml   ← tools/build_ontology.py 生成（可手改；YAML 是单一真相源）
        │
        ▼
ontology/loader.py → DuckDB concepts / concept_relations / ontology_constraints / dimension_members
        │
        ├─ check.py  10 条约束 × 73 组 → error/warn
        ├─ chain.py  六跳链条（概念→标准→祖先→约束→事实→证据→页面）
        ├─ derive.py L3 公式计算（6 个派生指标，×100 单点缩放）
        └─ audit.py  逐跳断点体检（①②③④ 门禁，⑤⑥ 覆盖率）
```

### 5.5 关键契约（跨模块，不可随意改）

| 契约 | 内容 |
|---|---|
| 页码 | 全项目 1-based 物理页码；锚点格式 `[Ref: {doc_id}#P{N}]`；MinerU `page_idx` 在适配层 +1 |
| 数字 | 只由 SQL / 代码计算；LLM 输出只能是单元格位置与原文值；派生比率只走 `wiki_queries.yaml` 命名 SQL（ADR-047） |
| claims | kind = fact（指向 facts 行）/ segment（指向 segment_facts 行）/ query（重跑命名 SQL）；显示值必须与重算结果一致 |
| L2 正文数字 | 只能写 `{{cN}}` 占位符，渲染时绑定 claim；lint R9 逐句核对"数字没有 claim 覆盖" |
| frontmatter | 由代码拼装；LLM 不输出 frontmatter（P7） |
| facts 唯一性 | `facts_dedup` 唯一键含 `source_doc`：重述视为知识库特性，不覆盖（ADR-007） |
| 格式解耦 | 只有 `mineru_client.py` 碰外部解析格式（ADR-008） |
| 噪音 | 只打标 `is_noise`，不删除块（ADR-010） |
| 本体 | YAML 单一真相源，DuckDB 只是投影；只校验不推导（ADR-052） |

---

## 6. 质量门禁与验证体系

### 6.1 门禁清单（`check.sh` 五道 + 评测独立一道）

1. **单元测试**：`pytest -q` → 406 项，离线，exit 0（2026-11-03 实测）。
2. **全语料守恒律 + 基线对照**：`tools/audit.py corpus --baseline` → 29 份产物不得退步。
3. **本体链条完整性**：`finkb.ontology audit --strict` → ①登记/②祖先/③约束/④证据任一非零即失败（当前全 0）。
4. **每份文档事实数门禁**：`tools/facts_baseline.py --check` → 防单文档静默归零（坑-80）。
5. **金标事实集**：`tools/gold_check.py` → 72/72。
6. **（独立命令）评测集**：`eval/check.py` → 15/15（不进 `check.sh`，避免测试链依赖 facts 库的历史顺序）。

### 6.2 其它验证手段

| 手段 | 实测结果 |
|---|---|
| 数字回查 PDF 原文 | 36,026 抽样 / 6 个查不到（99.98%） |
| 原文数字召回 | 18 份 / 19,782 个数字全部可解释 |
| 守恒律 | 359/359 |
| lint（L2） | 0 错 0 警 |
| 故障注入 | 假锚点/断链/无源数字/截断页 4/4 被拦 |
| 真实 kill -9 | 原子写窗口内 SIGKILL，目标页仍是完整旧版、可解析 |
| 语料基线 | 29 文档 × 5 指标 |
| 金标事实 | 72/72 |
| 评测 | 15/15（S01–S08 / CP01–CP04 / CC01–CC03；2026-11-03 实测） |

### 6.3 评测题的现状

- 15 题覆盖三类：单指标 8、跨期 4、跨公司 3；每题有标准答案、参考 SQL、证据自然键（doc/page/metric/period）、页锚与原文命中校验。
- 当前复算方式：`eval/check.py` 按自然键重跑 SQL 并比对；近期已把 evidence 从自增 `fact_id` 改为自然键（原问题：重灌后 fact_id 整体错位导致 14/15 失败）。
- **未做**：负例测试（故意改错答案应报错）；路由正确率评测；文字类问题（Recall@5）评测；扩题到 50–60 题。

---

## 7. 设计决策地图（ADR 摘要）

完整 56 条见 `finkb/DEVLOG.md` §5。按主题归并：

| 主题 | ADR | 内容一句话 |
|---|---|---|
| 分层与数字 | 001 / R4 | 按数据类型分层；数字只由 SQL 计算 |
| 检索 | **002 / 003 / 006** | FTS5 为主、向量为辅；不做 GraphRAG（用链接图+relations 边表）；向量用 numpy + `.npy`，不引 DuckDB vss |
| L1 抽取 | 004 / 005 / 007 / 029–040 | 两段式抽取；宽表视图 + 枚举注入；唯一键含 source_doc；直接消费 LogicalTable；列级召回/单位反推/金标验收 |
| 解析 | 008–028 | 格式解耦；统计签名噪音只打标；缝合四条件；物理表≠逻辑表；vlm 后端；自动分批；表头/期间/单位判据 |
| L2 | 041–051 | 来源分层计数；lint 范围；锚点用 report.json；正文占位符+三类 claim；指标字典 v2；关系/冲突只做结构化部分；draft 隔离；R10 定性页锚；批量编译；探针只做方案定义的 01–05 |
| 本体 | 052–056 | YAML 单一真相源、落 DuckDB、只校验不推导；约束 error/warn 两档；check 与 audit 分开；维度种类封闭成员开放；率值期间口径 |

**哪些决策是"可翻案"的**（每条 ADR 都写了失效条件）：

- ADR-002（FTS 为主）失效条件：语料单主题 10 万+ 文档（向量在大语料上更必要）。
- ADR-003（不做 GraphRAG）失效条件：文档到千级 / 需要跨文档实体消歧。
- ADR-006（numpy 不引向量库）失效条件：块数到 10 万级。
- ADR-052（只校验不推导）失效条件：语料到 H 股/IFRS，或概念数破千导致递归 CTE 变慢。

这四条正是外来计划主要撞击的点；它们不是"不能改"，而是"改了要说清为什么、并在新规模上重新验收"。

---

## 8. 缺口与已知限制（诚实清单）

### 8.1 能力缺口（架构有位置、代码没做）

| 缺口 | 现状 | 影响 |
|---|---|---|
| L3 交互层 | 未做 | 没有前端、没有路由徽标、没有 [Ref] 跳页，无法演示"问答" |
| 检索路由 | 未做 | 数字题/知识题/长尾文字题没有自动分流 |
| 向量/语义检索 | 设计已定（bge-m3 + reranker + numpy），**代码未写** | 无法回答"董事长致辞说了什么""海外风险"这类文字问题 |
| 中文分词 | jieba 未装；trigram 替代 | 2 字词无别名时查不到 |
| 跨期对比页 | 未做 | 同一公司多年并列视图缺失 |
| 06–08 探针 | 方案只有编号、无定义 | 按"二期"处理，未自行编造（ADR-051） |
| 因果/驱动关系 | 本体无因果边；facts 无业务驱动指标 | 不能做因果推理；可做公式拆解/六跳追溯 |
| 文本语义矛盾 | 未做 | 只检测 facts 级数值冲突 |

### 8.2 数据质量缺口

| 缺口 | 现状 | 影响 |
|---|---|---|
| 436 页无可检索文本 | 空表格块，`tables/` 也无内容 | 文本通道的召回盲区；需确认是否 OCR 补 |
| 纯文字表头 | 裸表 401 张中约一半是键值表；真实缺口候选约 55 张（1.1%） | 表头可信度低于数据本身；需人工标注 |
| 宽表逐页丢空列 | 列数 16 vs 15 时拒绝缝合 + 告警 | 数据不丢，但分片无列名；不猜对齐（坑-40） |
| 关系数据 | 621 条仅 4 家；15 条行标签噪音；无实体消歧 | 图/关系类问题覆盖有限 |
| 同页并列合并/母公司表 | 选表可能选错（当前 29 份未出现） | 保留为已知风险 |
| 指标范围 | 36 个全是报表科目；无销量/单价/研发人员 | 量价拆分、业务归因做不了；杜邦可做 |
| 分部覆盖 | 13/29 份文档有三列表（银行/保险/季报本身不披露） | segment 事实覆盖不全 |

### 8.3 工程缺口

| 缺口 | 现状 |
|---|---|
| 编译不幂等 | 重编会重写 claims；LLM 正文每次不同 |
| token 成本 | L2 编译首批 29 份 1,338 秒（约 22 分钟），补新探针 442 秒；token 成本未量化 |
| vlm 后端性能/成本 | 只知"更慢更贵"，无量化倍率 |
| 评测负例 | 未做"故意改错答案应报错" |
| 文档漂移 | README/任务日志数字滞后（见 §9） |
| eval 改动未提交 | 金标题自然键改造（3 个文件）在工作树里 |

---

## 9. 文档体系与状态漂移

| 文档 | 定位 | 与实测的偏差（2026-11-03） |
|---|---|---|
| `finkb/DEVLOG.md` §2 | **权威状态** | 基本准确（406 测试、1,830 事实、153 页），个别旧处已标注 |
| `finkb/README.md` | 使用说明 | 滞后：写"384 项测试、1717 条事实、116 页、15/15"，实际 406 / 1,830 / 153 / 15/15 |
| `finkb/任务日志.md` §1 | 实现流水 | 明显滞后：写 331 passed、1,064 条事实（2026-10-30 快照）；§7 已承认 DEVLOG 有陈旧断言 |
| `FinKB-方案v2.md` §11 | 路线图 | 实施状态注记写"29 份/116 页、探针 01/02/05"，实际 153 页、探针 01–05 |
| `eval/README.md` | 评测说明 | 已同步（本轮修改，未提交） |

**结论**：读项目先读 DEVLOG §2 + 本文；README 只当"怎么用"，任务日志只当历史流水。

---

## 10. 与外来计划的取舍坐标

> 外来计划的完整评估另见会话记录与（未来）文档；这里只给"基于现有资产做取舍"需要的事实。

### 10.1 外来计划要什么

| 层 | 计划要求 | 关键点 |
|---|---|---|
| L1 解析 | Docling/MinerU、跨页表格、**报表↔附注双向锚点** | 验收：跨页表 ≥95% |
| L2 本体与推理沙箱 | Owlready2 TBox、Pellet 描述逻辑、SWRL、前向推导物化 | 验收：覆盖 100%、2 秒内推导、无逻辑冲突 |
| L3 混合存储 | Neo4j+n10s/APOC、DuckDB/PG、Qdrant | 验收：100+ 年报、图/时序主键 100% 一致 |
| L4 联合计算 | LlamaIndex Agent 路由、符号依赖图求值、图谱锚点受限向量检索 | 三工具：OntologyFormula / GraphTraversal / AnchoredVector |
| L5 应用 | 结构化 prompt、确定性结论、页码/表格引用 | 数值 100%、实体科目定位 ≥95%、引用有效 ≥90% |
| 规模假设 | 120 个标准科目、100+ 年报、全市场 | — |
| 工期 | 8 周（4 个 Phase） | — |

### 10.2 现有资产对照（复用 / 改造 / 新建）

| 计划层 | 现有资产 | 判断 |
|---|---|---|
| L1 解析 | 自研 MinerU 管线 3,141 行 + 29 份产物 + 80+ 坑；93.5% 表头、99.98% 回查 | **复用**；只需增量"报表↔附注双向锚点"，**不要重建** |
| L2 本体 | 62 概念 / 71 关系 / 10 约束 / CAS 对齐 / 六跳链条 / 维度层 | **半复用**；缺 OWL 运行时、因果边、驱动因素层 |
| L3 混合存储 | 621 边 + 16,177 块（约 63 MB）+ DuckDB | **暂不需要**；NetworkX/递归 CTE/numpy 足够，触发条件见 §7 |
| L4 联合计算 | 无 Agent；事实通道已有规则槽位原型（15/15） | **新建**；路由可先规则、后 LLM |
| L5 应用 | claims 账本、页锚、数字闸门、lint | **复用** |
| 规模 | 29 文档 / 14 公司 / 36 指标 | **不匹配**；扩到 100+ 文档/120 科目需另算成本 |
| 评测 | 15 题精确复算 + 72 金标 | **可扩**到 60 题；但"实体科目定位 ≥95%"需先定义口径 |

### 10.3 与现有决策的冲突

| 冲突 | 涉及决策 | 级别 | 说明 |
|---|---|---|---|
| Owlready2 / Pellet / SWRL / 前向推导 | ADR-052、v2 §7.5.6 | **高** | "只校验不推导"是明确决策；本机无 Java、未装 owlready2，且破坏离线可复现 |
| Neo4j + n10s/APOC | ADR-003、ADR-052 | **高** | 621 条边、14 家公司；"全市场拓扑"对当前语料不成立 |
| Qdrant 向量库 | ADR-006 | 中 | 16,177 块 / 63 MB，numpy 暴力检索足够；触发条件是 10 万块 |
| LlamaIndex Agent | 无直接 ADR | 低 | 能力可做，但先规则路由更可控 |
| Docling 重建解析 | ADR-016 / 现有管线 | **高（浪费）** | 现有解析产物与 29 份验证是沉没成本；重建不带来验收提升 |
| 100+ 年报 / 120 科目 / 全市场 | v2 §5 R2 语料锁定 | 中 | 与"单人作品集"定位冲突；扩规模会显著抬高维护成本 |

### 10.4 成本结构（哪些是沉没成本，哪些是增量）

- **已沉没、且高度可复用**：L0 解析与清洗/缝合/切分（3,141 行 + 29 份产物 + 80 坑教训）、L1 抽取与门禁（2,555 行 + 1,830 事实 + 72 金标）、L2 编译/claims/lint（1,548 + 481 行 + 153 页）、评测与基线（15 题 + 基线 + 406 测试）。
- **增量较小**：文本/语义检索（约 16K 块、63 MB，numpy + bge-m3 即可）、命名路由、评测扩题。
- **增量较大**：OWL 推理栈（新运行时 + Java + 2 个 reasoner 配置）、Neo4j/Qdrant（两个常驻服务 + 数据同步）、规模扩张（100+ 文档的解析/抽取/维护成本线性增长）。
- **外来计划里"看起来新"但本项目已有等价物**：可溯源生成（claims/页锚/数字闸门）、概念层（四层本体栈）、审计（lint + audit + 门禁）。

### 10.5 做取舍前建议先定的四件事

1. 这份外来计划的定位是**目标蓝图**还是**8 周施工图**？（两者验收口径不同）
2. 是否要推翻 ADR-052（只校验不推导）与 ADR-003（不做 GraphRAG）？推翻需要新规模与新验收理由。
3. 语料是否扩到 100+ 年报、指标是否扩到 120 科目？这决定图库/向量库是否有必要。
4. 优先级是"作品集展示全栈技术"还是"问答质量与可维护性"？两者会选到不同的技术栈。

---

## 11. 附：快速上手与文件索引

### 11.1 最短验证路径（5 分钟）

```bash
cd /home/generalblack/code/works/reporting/finkb
../.venv/bin/python -m pytest -q                 # 406 项，应 exit 0
cd .. && .venv/bin/python eval/check.py          # 15/15
cd finkb && ../.venv/bin/python -m finkb.search "比亚迪 毛利率"
../.venv/bin/python -m finkb.ontology --db data/facts.duckdb tree | head
```

### 11.2 关键文件索引

| 想知道什么 | 看哪里 |
|---|---|
| 当前状态/决策/坑/未验证项 | `finkb/DEVLOG.md`（§2 / §5 / §7 / §8） |
| 接手约束与汇报纪律 | `finkb/AGENTS.md` |
| 架构为什么这样设计 | `FinKB-方案v2.md`（§4 / §6 / §7） |
| 轻解析路线是否值得走 | `FinKB-方案v3-轻解析路线.md` |
| 解析问题集 | `finkb/PDF解析细节.md` |
| 事实库表结构 | `finkb/src/finkb/fact_engine/store.py` |
| 知识页生成与门禁 | `finkb/src/finkb/wiki/compiler.py`、`lint/rules.py` |
| 本体层实现 | `finkb/src/finkb/ontology/`、`config/ontology.yaml` |
| 指标字典 | `config/metric_dict.yaml`（36 指标 / 80 别名） |
| 命名 SQL | `config/wiki_queries.yaml`（5 条） |
| 评测 | `eval/gold_qa.json`、`eval/check.py` |

---

> 本文由 2026-11-03 全量扫描生成；所有数字均可由 `finkb/scripts/check.sh`、`eval/check.py`、`tools/audit.py corpus`、`finkb.ontology audit` 复现（个别历史数字注明来源）。若后续改代码，请同步更新 `finkb/DEVLOG.md` 的对应章节。
