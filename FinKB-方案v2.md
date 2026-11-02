# FinKB 项目方案 v2.3

> **状态**：已批准架构基线（评审通过），完成第二轮参考实现调研与吸收，并新增本体层
> **文档版本**：v2.3　｜　**上一版**：v2.2
> **本版变更**：新增 **§7.5 L0.5 本体层**（四层概念栈 + 六跳可追溯链条 + 三张主表），并引入三个扩展补丁 **O1–O3**。同时对「不引入 FIBO 本体」这一结论做收窄修正：不引入的是 OWL 推理栈，不是语义层。
> **约束等级**：第 5 章（决议）、第 6 章（补丁 **P1–P9**）与 **§7.5.6（本体层边界）** 为**不可违背的工程约束**，实现时不得绕过。

---

## 0. 速览卡

| 项 | 最终定义 |
|---|---|
| **项目定位** | 单人专业分析工作台，**作品集 / Demo-First 导向**；不做多租户、不做线上审核流 |
| **语料范围** | 严格锁定 **A 股标准年报 PDF**；首批 3 家车企（比亚迪 002594 / 赛力斯 601127 / 长安汽车 000625） |
| **语料年份** | **2025 年**（使用工作区 `raw/` 已有数据，不补拉历史年份） |
| **MVP 单文档** | `002594_2025_年报`（比亚迪 2025 年年度报告） |
| **MVP 探针** | `01_核心财务指标`、`02_主营业务分产品结构`、`05_现金流与资本结构` |
| **架构** | L0 凭证层 / L1 事实层 / L2 知识层 / L3 交互层 |
| **核心不变量** | **数字只由 SQL 计算；LLM 只做「语义对齐」与「查询生成 / 结论叙述」** |
| **实现约束** | `_draft` 流转与 `lint` 全部由**本地目录状态 + 轻量 Python 脚本**实现，无服务端 |
| **技术栈** | MinerU（解析）／DuckDB（事实）／SQLite FTS5（主检索）／numpy（向量兜底）／Markdown（知识） |
| **参考实现** | [OpenKB](https://github.com/VectifyAI/OpenKB)（同范式，**只抄知识层工程实现**）／[FIBO](https://github.com/edmcouncil/fibo)（**静态导出类骨架做 L1 本体**）／[CAS 2015 国标](http://m.mof.gov.cn/tzgg/201504/t20150407_1213301.htm)（**只做 L2 科目名参考**） |
| **工时基线** | 全量 31–35 有效工作日（v2.3 加本体层 2–3 天）；Phase 0+1 约 13–14 天；MVP 约 11 天可演示 |

---

## 1. 项目本意（不变的起点）

传统 RAG 在「知识库」这个应用场景下有两个本质缺陷：

1. **知识不沉淀**：每次提问都现场检索、现场拼凑、现场生成，回答质量不稳定，知识无法随交互与文档更新实现复利。
2. **知识不可维护**：向量是黑盒，用户无法直观审阅、组织、修正知识；没有人类可读、可追溯、可视化的知识库实体。

**目标**：把传统 RAG 的检索能力与 LLM Wiki 的沉淀能力结合，构建一个**会积累、可审计、可维护**的财报知识库系统。

从原计划继承、本方案全部保留的痛点判断：

- 知识碎片化与失忆（No Accumulation）
- 跨期跨表分析脆弱（切片打散表格行列关系）
- 检索概率性、不可解释
- 单位混乱与大模型心算误差

---

## 2. 调研结论：这个范式已有名字，且有生产级实现

### 2.1 范式名称：知识编译（Knowledge Compilation / LLM Wiki）

由 Karpathy 提出（原始 gist：https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f）。
Google Cloud 已将其工程化为 **OKF v0.1（Open Knowledge Format）** 规格：
https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf

范式对比核心结论（来源：https://pieteams.github.io/piekbs/zh-CN/ecosystem/paradigm-comparison）：

| 维度 | RAG | LLM Wiki（知识编译） |
|---|---|---|
| 核心操作 | 查询时检索 | **写入时编译** |
| 存储 | 向量库 + 原始 chunk | 结构化 Markdown + SQLite FTS |
| 搜索方式 | ANN 相似度 | FTS5 BM25 + 图遍历 |
| 知识形态 | 隐式（向量，不透明） | **显式（可读 Markdown）** |
| 可审计性 | 低 | **高（git diff、lint）** |
| 知识积累 | 无（静态索引） | **随时间复利增长** |
| 查询时 Token | 高 | 低（预编译页面） |
| 延迟 | 较高（ANN + LLM） | 较低（FTS 亚毫秒） |
| 最适合 | 宽泛问答、动态数据 | **长期知识管理、Agent 记忆** |

**关键结论：没有任何一个生产系统用纯 Wiki 替代 RAG。**
典型混合模式：RAG 管道宽泛摄入 → 高置信度结果编译为 Wiki 页面供长期复用。

### 2.2 直接对标的参考实现（不在 awesome-llm-apps 仓库内）

| 项目 | 形态 | 对本项目的参考价值 |
|---|---|---|
| [PieKBS](https://github.com/pieteams/piekbs) | 本地优先，文档→结构化 Markdown，MCP 接口 | 整体范式；OKF 兼容；lint / draft / 空白分析机制 |
| [Sage Wiki](https://github.com/xoai/sage-wiki) | Go 单二进制；分层编译（Tier 0–3）；FTS5 411µs + 向量 81ms + 图谱 1µs，RRF 融合 | 分层编译策略；三路检索实测数据 |
| [llm_wiki](https://github.com/nashsu/llm_wiki) | Tauri 桌面；增量编译；Obsidian 兼容 | 增量编译与人类在环 |
| [Onyx](https://github.com/onyx-dot-app/onyx) / Outline / Confluence+Rovo | 企业知识平台 | 权限、多源接入（本项目不需要） |
| [STORM / Co-STORM](https://github.com/stanford-oval/storm) | 多视角提问 → 检索 → 大纲 → 带引用词条 | 探针编译器的原始出处 |
| [PageIndex](https://github.com/vectifyai/pageindex) | 无向量结构树推理检索，FinanceBench 报告 98.7%（二手引用，需核实原论文） | 强结构文档的长尾兜底第三档 |
| [WikiChat](https://github.com/stanford-oval/WikiChat) | 先检索验证再生成 | 稳定性机制参考 |
| Cognee / Mem0 / Letta / MemOS | Agent 记忆框架 | 记忆操作原语，本项目暂不依赖 |
| **[OpenKB](https://github.com/VectifyAI/OpenKB)** | PageIndex 团队；文档→编译→互链 Wiki；OKF 兼容；Obsidian 兼容（4794★，Apache-2.0） | **同范式参考实现**，知识层工程细节的主要吸收源（见 2.4、P5–P8） |
| **[FIBO](https://github.com/edmcouncil/fibo)** | EDM Council 金融业务本体（OWL/RDF，450MB，MIT，765★） | 借「金额三维建模」思想（见 2.4、P9）+ **静态导出类骨架做轻量本体（见 7.5）**；**不引入 OWL 推理栈** |

其他学术参照：KAG（https://arxiv.org/abs/2409.13731）、RAPTOR（ICLR 2024）、HippoRAG 2（https://proceedings.mlr.press/v267/gutierrez25a.html）、ACE（ICLR 2026）、Anthropic Contextual Retrieval（https://www.anthropic.com/engineering/contextual-retrieval）。

### 2.3 对本方案影响最大的三条实证

1. **生产级 LLM Wiki 的主检索引擎都是 SQLite FTS5 + BM25，不是向量库**（FTS 411µs vs 向量 81ms；图谱遍历 1µs）。
2. **知识库之所以是知识库，靠 6 个维护机制**（见 7.3），而不是「LLM 生成的 Markdown 堆积」。
3. **数字不该由 LLM 生成后事后校验，而应该由 SQL 计算得出**（语义层对 text-to-SQL 准确率有实证提升：https://ieeexplore.ieee.org/document/11627365）。

### 2.4 两个参考仓库的实地调研结论（已拉取源码逐项核对）

#### OpenKB（VectifyAI，4794★，Apache-2.0，2026-04 建仓）

由 PageIndex 团队实现的 CLI 知识库，与本项目**同范式**：把原始文档**编译**成结构化、互链的 Wiki。已核对的事实：

- wiki 页面遵循 **Google OKF v0.1**；明确声明 **Obsidian 兼容**（纯 `.md` + `[[wikilink]]`）
- **短/长文档分流**：`pageindex_threshold: 20`，≥20 页走 PageIndex，落盘为按页 JSON
- 自带 **lint 七条规则** + `--fix`、**自包含图谱 HTML**、**原子写 + 文件锁**
- 依赖栈：`pageindex` / `markitdown` / `litellm` / `openai-agents` / `portalocker`

> **采用策略：抄它的「知识层工程实现」，不抄它的「摄入与检索」。**
>
> 两条硬理由：① 它用 `markitdown` 转 PDF，**表格保真度弱于 MinerU**，而财报表格是本项目的命门；② 它**没有结构化数值层**（`grep duckdb|sqlite|SELECT` 零命中，数值仅存在于文本与页面 JSON 中）——这恰恰是本项目 L1 的价值所在。

吸收清单 → **P5（目录骨架）、P6（按页 JSON 载体）、P7（frontmatter 代码化）、P8（lint 七条 + 原子写）**。

#### FIBO（EDM Council，765★，MIT，450MB）

金融行业业务本体，OWL/RDF 共 468 个文件，域划分：`FND`(基础) / `BE`(业务实体) / `FBC`(金融业务) / `SEC` / `DER` / `LOAN` / **`IND`(指标)** / `MD`。

**先给一条明确的负面结论，避免在错误期待上投入时间：**

> `IND/Indicators/Indicators.rdf` 的类目是 `market rate`、`market spread`、`quoted price`、`term structure`、`volatility`、`implied volatility`……**全部是市场行情指标，完全没有财务报表科目**（营业收入、毛利率、研发费用）。`IND/EconomicIndicators` 是宏观经济指标。
>
> **结论：FIBO 提供不了 `metric_dict`。本项目的 19 个财务指标枚举必须自建。**

可借鉴的是**金额建模的三条思想**（→ P9）。**不建议引入的是 OWL 推理栈**：它需要图数据库 + 推理机（GraphDB/Stardog），14 家年报规模下是纯负担，且与「单人作品集项目」的定位不匹配。

> **v2.3 修正**：上一句「不引入本体」需要收窄——不引入的是**推理运行时**，不是**语义层**。经全库核实（298 个 rdf 文件逐个清点），FIBO 的财务类骨架（`Income` / `EBITDA` / `OwnersEquity` / `CashFlow` / `Report` 等）依赖闭包只有 **20 个文件 / 291 个类**，取其中约 40 个即可当 L1 骨架；而 CAS 国标（2015-03-31 版，3418 个数据项 / 5008 条中文标签）提供 L2 科目名。两者**互补而非二选一**：FIBO 回答「是什么类型」，CAS 回答「报表里叫什么」。落地方式见 **§7.5 L0.5 本体层**。

---

## 3. v1 的根本性错配（为什么推倒重来）

v1 按「模块」分层（解析→检索→探针→路由→前端），但**真正决定成败的是数据类型的划分**。v1 把三种性质完全不同的东西混在同一层：

| 数据类型 | 本质 | v1 的处理 | 后果 |
|---|---|---|---|
| **数值事实** | 确定性、可计算、必须 100% 准确 | LLM 生成 Markdown 表格，事后正则校验 | 幻觉、心算错误、单位混乱、校验误杀、重试死循环 |
| **定性知识** | 需综合与解释，允许表述多样 | 与数值混在一起 | 无法区分「数字错了」和「说法不同」 |
| **原始凭证** | 不可变、需页码定位 | 只当检索料 | 溯源能力被浪费 |

**核心错误：把数字交给了最不擅长数字的东西（LLM），然后花整个 Write-Gate 模块去补救。**

v1 由此产生、在本方案中被直接消除的 7 个具体陷阱：

1. **数字存在性校验大面积误杀**：归一化到「亿元」后，`6023.15` 在原文（`602,315,143` 千元）中根本不存在，纯字符串 `in` 匹配必然失败。
2. **Write-Gate 死锁**：「缺失率 >20% 拒绝入库」+「无源数字自动重试」，检索质量差时形成循环，整篇财报写不进去。
3. **「Wiki 命中 → 1.2 秒」自相矛盾**：一级路由仍要「由 LLM 整理回答」，延迟由 token 生成速度决定。
4. **消融实验的对照数字是预估值**，未实测，会成为硬伤。
5. **rank-bm25 不产出可持久化稀疏向量**：存入 Qdrant 需自建 term→id 词表映射（100–150 行），成本被低估。
6. **排期不成立**：14 天 vs 实际 26–36 工作日。
7. **Sink 直接写入 vault，无隔离闸门**：首个错误被永久固化并被后续回答引用。

---

## 4. 架构总览：四层知识栈

### 4.1 分层结构

```
┌──────────────────────────────────────────────────────────────┐
│  L3  交互层：三栏工作台 / 路由徽标 / 三视图 / MCP（二期）    │
│      查询路由：数值→SQL ｜ 高频→Wiki 直读 ｜ 长尾→混合检索   │
├──────────────────────────────────────────────────────────────┤
│  L2  知识层：Markdown Wiki（git 追踪，Obsidian 兼容）        │
│      目录骨架（P5）：summaries/ concepts/ entities/          │
│                      explorations/ reports/ _draft/          │
│      frontmatter 由代码生成（P7）＋ 类型化链接 ＋ lint（P8） │
│      主检索：FTS5 + jieba + 别名扩展 + 链接图 BFS            │
├──────────────────────────────────────────────────────────────┤
│  L1  事实层：DuckDB 结构化数值（★ 关键新增）                 │
│      两段式抽取：LLM 语义对齐 → Python 反查归一入库          │
│      金额建模（P9）：unit_family/currency/unit_of_account    │
│      宽表供 Text-to-SQL；重述按 report_date 排序             │
├──────────────────────────────────────────────────────────────┤
│  L0  凭证层：PDF 原文 + 按页 JSON 载体（P6）+ 向量兜底       │
│      跨页表格缝合；bge-m3 + bge-reranker 服务定性长尾        │
└──────────────────────────────────────────────────────────────┘
```

### 4.2 每层的生成方式与验证方式（可靠性的来源）

| 层 | 谁生成 | 怎么验证 | 出错后果 |
|---|---|---|---|
| L0 | 无人生成（原始文件） | 文件 hash + 页数 | 无 |
| L1 | LLM 摘录 + 代码校验归一 | **单元测试断言数值 + 字面量反查** | 立刻被测试或反查拦住 |
| L2 | LLM 编译 + 人策展 | lint + 锚点核验 + draft 隔离 | 停留在 draft，不污染 |
| L3 | — | Ragas / 评测集 | 可量化 |

v1 的「Write-Gate 数字核验」被拆解为两件更简单的事：

- 数字正确性 → 由 L1 的两段式抽取（补丁 P1）+ 单元测试保证
- 定性主张可信性 → 由 L2 的「每条主张指向 facts 行或 L0 页」+ lint 保证

### 4.3 查询路由（回答稳定性的来源）

| 问题类型 | 路径 | 延迟 | 确定性 |
|---|---|---|---|
| 数值 / 计算 / 对比 | LLM 生成 SQL → DuckDB → LLM 叙述 | ~1–3s | **数字 100% 确定** |
| 高频知识 | FTS 命中 Wiki → 直接返回页面 | <1s | 页面内容已固化 |
| 高频知识（要解释） | Wiki 页 → LLM 摘述 | 3–8s | 叙述可变，事实不变 |
| 长尾定性细节 | FTS → 图 BFS → 向量兜底 → RRF → LLM | 5–8s | 概率性，但带页码溯源 |

> 「直读返回」与「LLM 摘述」是两种不同的延迟承诺，前端必须用路由徽标区分展示。这修正了 v1「1.2 秒」的自相矛盾。

### 4.4 检索方向反转：FTS 为主，向量为辅

| 层 | 技术 | 延迟 | 可解释性 |
|---|---|---|---|
| 主检索 | SQLite FTS5 + BM25（jieba + 别名扩展） | 亚毫秒 | **可解释：能展示命中了哪些词** |
| 结构导航 | Markdown 链接图 + BFS | 微秒 | 可解释：链接是人/LLM 提议+人确认 |
| 数值查询 | DuckDB | 毫秒 | 确定性 |
| 语义兜底 | 稠密向量 + rerank（**MVP 必备，见补丁 P3**） | ~80ms + rerank | 黑盒，仅服务长尾 |
| 融合 | RRF | — | — |

理由：

- v1 抱怨「检索概率性不可解释」，只有把主语换成 FTS 才能解决——BM25 命中的是词，可以展示；向量命中的是数，无法解释。
- 会计科目、股票代码、高管姓名等精确匹配，BM25 天然强于向量（向量会把「应收账款」和「应收票据」算得很近）。
- v1 的 jieba + 稀疏字典 + Qdrant 词表映射整条链路被 FTS5 替代，省去 100–150 行高风险原创代码。

### 4.5 不做知识图谱（GraphRAG）的理由

- 语料仅 3 家公司、6–18 份报告，实体关系图谱投入产出比极差。
- GraphRAG 索引成本高是公认问题（微软为此推出 LazyGraphRAG；token 成本对照见 https://aclanthology.org/2025.findings-emnlp.321.pdf）。
- **Markdown 显式双向链接本身就是一张图**，6 份文档规模下质量远高于自动抽取的三元组。
- 多跳关系（如「三家车企共享哪些供应商」）从 L1 `relations` 边表生成，而非 LLM 抽三元组：确定性、可校验、零成本。

---

## 5. 四项关键决策（R1–R4，已锁定）

> 本章为评审批复结论，取代 v2.0 第 9 章的「待确认问题」。

| 编号 | 决策项 | 锁定结论 | 对实现的直接约束 |
|---|---|---|---|
| **R1** | 用户定位与权限 | **单人专业分析工作台（求职作品集导向）** | 不做多租户鉴权、不做线上审核流、不做多人协同；`_draft` 与 `lint` 用**本地目录状态 + 轻量 Python 脚本**实现 |
| **R2** | 目标文档范围 | **严格锁定 A 股标准年报 PDF**；首批 3 家（002594 / 601127 / 000625） | MVP 不引入研报、公告、高频新闻；充分利用 A 股年报强制章节结构（第二节财务指标、第三节管理层讨论）降低解析噪声 |
| **R3** | 交付优先级 | **作品集可演示导向（Demo-First）**，执行 10 天 MVP 砍法 | ① 先以 `002594_2025_年报` 单文档跑通；② 仅配 3 个探针；③ L0→L1→L2→L3 全链路打通后再横向扩 |
| **R4** | 计算前提 | **完全接受「数值由 SQL 计算、LLM 仅负责查询生成与结论叙述」** | L1 必须按补丁 P1 重构抽取与表结构；任何模块不得让 LLM 直接产出数字 |

### 5.1 R2 语料清单（以工作区 `raw/` 实际文件为准）

| 公司 | 代码 | 2025 年报 | 2025 半年报 | 2025 季报 | 2026 半年报 | 2026 一季报 |
|---|---|---|---|---|---|---|
| 比亚迪 | 002594 | ✅ **MVP 主文档** | ✅ | ✅ | ✅ | ✅ |
| 赛力斯 | 601127 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 长安汽车 | 000625 | ✅ | ✅ | ✅ | ✅ | ✅ |

- 主数据源：`raw/manifest.csv`（含 cninfo 原始链接与公告日期）。
- **不补拉 2023/2024 年报**（R2 已明确）。
- 跨期能力来源：① 2025 年报内的「上年同期」列（2024 数）；② 2025 半年报 vs 2026 半年报。
- 因此**「重述口径冲突」是必然场景而非边缘场景**，补丁 P4 从防御性设计升级为必需项。

---

## 6. 工程补丁（P1–P9，不可违背）

> 分两批：**P1–P4** 来自架构评审批复（数值与事实层的正确性）；**P5–P9** 来自 OpenKB / FIBO 参考实现吸收（知识层的可维护性）。两批同为硬约束。

### 6.1 P1 ｜ L1 两段式抽取：解决「非标年报表格无法纯代码解析」

**背景盲区**：A 股年报表格行列名高度非标（同为汽车毛利率，可能写作「汽车及相关产品」「新能源汽车」「汽车整车」；年份时而在表头、时而在首列），纯硬编码正则无法映射到统一 `metric_id`。

**执行约束**：必须拆成两段，**严禁合并**。

```
阶段 A（LLM，语义对齐 —— 只摘录，不改写）
  输入：缝合后的单表 Markdown + 章节上下文 + metric_dict 枚举清单
  输出：Observation（Pydantic 强类型，见 9.1）
  规则：
    ✅ 允许：把「汽车及相关产品毛利率」对齐到 metric_id=auto_gross_margin
    ✅ 允许：把原文数字字面量原样抄进 raw_text_matched
    ❌ 严禁：单位换算
    ❌ 严禁：四舍五入
    ❌ 严禁：跨列拼接、跨表推断、补全缺失值

阶段 B（Python，确定性 —— 反查 + 归一化 + 入库）
  1. 反查：raw_text_matched 是否真实存在于 page_num 对应原文
  2. 归一化：value_norm = convert(value_raw, unit_raw)   ← 代码计算，非 LLM
  3. 入库：写 facts（含 match_level）
```

**反查的三级匹配策略**（对 P1 的落地补充，已确认接受）：

| 级别 | 规则 | 处理 |
|---|---|---|
| `exact` | 原文中直接 `in`（含千分位、百分号） | 通过 |
| `normalized` | 去千分位 / 全角转半角 / 空白归一后再 `in` | 通过，记标记 |
| `numeric` | 数字级匹配（相对误差 < 1e-6） | 通过，记标记 |
| `fail` | 全部失败 | **拒绝入库**，进 `unmatched_pool`，**禁止静默丢弃** |

理由：`exact` 保证零误杀；`normalized`/`numeric` 兜住 LLM 省略千分位等格式差异，避免大面积拒绝。质量报告按级别统计占比，**`exact` 占比即解析质量的量化指标**（验收阈值 ≥80%）。

**验收**：`tests/test_fact_extraction.py` 断言 ≥10 个关键数值与原文绝对一致，且 `exact` 匹配率 ≥80%。

---

### 6.2 P2 ｜ 预建宽表视图 + 枚举注入：防 Text-to-SQL 在 EAV 纵表上翻车

**背景盲区**：`facts` 为 EAV 纵表，若让 LLM 直接面对纵表写多指标跨期对比，需大量 Self-JOIN / `CASE WHEN` 聚合，易出语法错误或猜错 `metric_id` 导致空集。

**执行约束**：

1. 基于 `facts` 预建**宽表视图**，将核心指标透视展开为固定列：

```sql
CREATE VIEW v_company_annual_wide AS
SELECT company, period,
       max(CASE WHEN metric_id='revenue'           THEN value_norm END) AS revenue_yi,
       max(CASE WHEN metric_id='net_profit'        THEN value_norm END) AS net_profit_yi,
       max(CASE WHEN metric_id='rd_expense'        THEN value_norm END) AS rd_expense_yi,
       max(CASE WHEN metric_id='auto_gross_margin' THEN value_norm END) AS auto_gross_margin_pct
FROM facts_dedup
GROUP BY company, period;
```

2. Text-to-SQL 的 System Prompt **强制附带完整 `metric_dict` 枚举清单**（`metric_id` + 中文名 + 默认单位 + 类别），**禁止 LLM 自行臆造指标名**。
3. Prompt 明确分流：**高频对比只查宽表**（单表 `SELECT`），纵表仅用于长尾单指标。

**验收**：给定「三家车企 2025 毛利率对比」，生成的 SQL 只引用宽表且一次执行成功。

---

### 6.3 P3 ｜ 保留轻量云端向量检索（MVP 禁止砍掉向量）

**背景盲区**：若 MVP 彻底剔除向量仅用 FTS5，面对无精确关键词的定性长尾提问（如「管理层对海外关税壁垒与供应链安全的应对规划」）召回率将严重下滑；且在求职面试中缺失向量与重排的实操说服力。

**执行约束**：

| 层 | 检索方式 | 说明 |
|---|---|---|
| **L2 知识层** | **SQLite FTS5 + BM25 + 链接图** 为主 | 保障亚毫秒响应与高可解释性 |
| **L0 凭证层** | **必须保留** 稠密向量 + 重排 | 服务定性长尾兜底 |

实现口径（已确认）：

- 向量：硅基流动云端 API **`BAAI/bge-m3`**（1024 维）
- 重排：硅基流动 **`BAAI/bge-reranker-v2-m3`**
- 存储：**numpy 暴力检索 + `.npy` 持久化**，**不使用 DuckDB `vss` 扩展**
  - 理由：预计 3k–6k 个子块 × 1024 维 float32 ≈ 12–24MB，点积毫秒级完成；`vss` 需联网 `INSTALL`，破坏离线可复现性
  - 语料扩到 10 万块以上时再评估 vss / HNSW
- 链路规模约 50 行，**不得推迟至二期**

**验收**：一个不含任何专有名词的定性问题，能召回到正确的页码并给出 rerank 后的 Top-3。

---

### 6.4 P4 ｜ 修正唯一约束与重述（Restatement）处理

**背景盲区**：2025 年报中包含「2024 年同期数（重述后）」，与 2024 年报披露的原数据口径可能不一致；v2.0 的 `UNIQUE (company, period, metric_id, caliber)` 会触发主键冲突而丢失信息。

**执行约束**：

1. 唯一约束修正为：
   `UNIQUE (company, period, metric_id, caliber, source_doc)`
2. 新增去重视图 `facts_dedup`：按 `source_doc` **降序**取最新披露口径（年报 > 半年报 > 季报，同年内取后发布者）。
   > **⚠️ 本条已被 P9 修正**：字符串降序不等于时间序，最终实现按 `report_date DESC, source_doc DESC` 排序，见 6.9。
3. 冲突检测：同一 `(company, period, metric_id, caliber)` 在新旧 `source_doc` 中 `value_norm` 超出容差 → **自动生成 L2 的 `contradicts` 标记**，并在词条中注明，例如：

   > 「比亚迪 2024 年研发费用在 2025 年报中发生会计口径重述：原 2024 年报披露 X，2025 年报同期列披露 Y。」

**验收**：人为构造两条不同 `source_doc` 的同期间数值，插入后 `facts_dedup` 取到新值，且自动产出 `contradicts` 记录。

---

### 6.4.1 分界线：第一批 P1–P4 到此结束

以下 P5–P9 为第二批补丁，全部来自 [OpenKB](https://github.com/VectifyAI/OpenKB) 与 [FIBO](https://github.com/edmcouncil/fibo) 的源码级吸收（调研记录见 2.4）。

**两批补丁解决的问题不同**：

| 批次 | 解决什么 | 若不做的后果 |
|---|---|---|
| **P1–P4** | 数值的正确性（不幻觉、不心算、口径不丢） | 数字会错，且错得难以察觉 |
| **P5–P9** | 知识的可维护性（不漂移、不写坏、可核验） | 三个月后 vault 变成无人敢碰的垃圾场 |

---

### 6.5 P5 ｜ L2 目录骨架对齐 OKF 惯例（取代自定义 companies/ comparisons/）

**背景盲区**：v2.1 用 `companies/{BYD}/` + `comparisons/` 作目录，是**按业务自定义的分法**，与 OKF v0.1 及 Obsidian/Dataview 生态的通用惯例不一致；且 `companies/` 与 `concepts/` 语义重叠（公司页本质是一个 entity）。

**执行约束**：`wiki_vault/` 采用如下骨架，**目录名不可改**：

```
wiki_vault/
├── index.md                 # 四段式 TOC（由代码维护，见 P7）
├── log.md                   # 追加式操作时间线
├── summaries/<doc>.md       # 每份年报一份综述
├── concepts/<slug>.md       # 跨文档综合、横向对比（如「三大车企2025毛利率对比」）
├── entities/<slug>.md       # 命名实体：公司页 type=organization + 供应商/客户
├── explorations/<slug>.md   # 问答沉淀（Sink）产物
├── reports/                 # lint 报告（自动生成）
└── _draft/                  # ★本项目的隔离区，同构镜像上述目录结构
```

> **修订（ADR-029 / ADR-043）**：`sources/` 已取消，不产出按页 JSON。L1 直接读
> `data/parsed/<doc>/report.json` 的 `tables` / `logical_tables`；L2 的锚点校验用
> `report.json` 的 `pages` 总页数 + `page_anchored.md` 的逐块 META，不再依赖
> `sources/<doc>.json`。下方 P6 保留为历史设计，**不执行**。

**类型映射（v2.1 → v2.2）**：

| v2.1 页面类型 | v2.2 落点 | frontmatter `type` |
|---|---|---|
| `source-note` | `summaries/<doc>.md` | `Summary` |
| `comparison`（横向对比） | `concepts/<slug>.md` | `Concept` |
| `companies/BYD/*`（公司页） | `entities/002594-比亚迪.md` | `Organization` |
| `decision` / `risk` | `concepts/<slug>.md` | `Decision` / `Risk` |
| Sink 沉淀 | `explorations/<slug>.md` → 通过 lint 后可提升为 `concepts/` | `Exploration` |

**理由**：① 与 OpenKB/OKF 一致，可直接复用其 lint 与图谱实现思路；② `entities/` 天然容纳「供应商 / 客户 / 子公司」，为 `relations` 边表的多跳问题提供可点击入口；③ Obsidian 的 Dataview 插件可按 `type`/`company`/`period` 直接生成第 8.3 节的三种树，零成本。

**验收**：目录结构生成正确；同一份内容分别放在 v2.1 与 v2.2 结构下，v2.2 的 lint 与图谱均能正确识别。

---

### 6.6 P6 ｜ 页码载体改为按页 JSON（已作废，见 ADR-029 / ADR-043）

> **状态：不执行。** ADR-029 选择 B：L1 直接以 `report.json` 的 `tables` /
> `logical_tables` 为输入，不产出 `sources/<doc>.json`；锚点校验改为
> `report.json` 的 `pages` + `page_anchored.md`（ADR-043）。本节保留为历史设计，
> 避免重新讨论。

**背景盲区**：v2.1 用行内注释 `<!-- META: {...} -->` 注入页码。问题：① 注释污染 Markdown 正文，LLM 编译时容易被误读；② block 级与 page 级粒度混在一起，表格缝合时需反复解析注释串；③ 无法被 Obsidian 干净渲染。

**执行约束**：页码锚定改由 **按页 JSON 载体**承担（借鉴 OpenKB 的长文档落盘方式），Markdown 正文保持干净。

```
data/parsed/<doc>/
├── full.md                 # 干净 Markdown（无 META 注释），用于人读与 Wiki 编译
└── sources/<doc>.json      # 按页载体，唯一锚定真相
```

`sources/<doc>.json` 结构：

```json
[
  {
    "page": 42,
    "content": "本节完整文本……",
    "blocks": [
      {"type": "heading", "level": 3, "text": "（三）主营业务分产品情况"},
      {"type": "table", "table_id": "002594_2025_年报#P42_T01",
       "rows": [["分产品", "2025年", "2024年"], ["汽车", "7,720.00", "6,023.15"]],
       "has_header": true, "page_range": [42, 43], "stitched": true}
    ],
    "images": ["images/p42_img1.png"]
  }
]
```

**双粒度约定（关键）**：

- **对外（Wiki / 前端 / 锚点）**：只暴露 **page 级**，锚点恒为 `[Ref: {doc_id}#P{n}]`
- **对内（L1 抽取）**：使用 `blocks` 中的 **block 级**信息（表格边界、`table_id`）

**生成路径**：MinerU `content_list.json`（block 级 `page_idx`）→ 按 `page_idx` 聚合 → 写入 `sources/<doc>.json`。`physical_page = page_idx + 1`。

**理由**：page 级载体是「锚点的唯一真相」，block 级信息是「表格结构的唯一真相」，两者分离后，跨页缝合只需操作 JSON，不必在 Markdown 里做字符串手术。

**验收**：给定任意 `[Ref: 002594_2025_年报#P42]`，能在 `sources/<doc>.json` 中 O(1) 定位到对应页；`full.md` 中不含任何 `<!-- META` 残留。

---

### 6.7 P7 ｜ frontmatter 由代码生成，LLM 只输出正文

**背景盲区**：v2.1 的 9.2 节要求 LLM 产出完整页面（含 YAML frontmatter）。实践中 LLM 极易写坏 YAML（引号、缩进、中文冒号、列表格式），导致 `find_invalid_frontmatter` 大面积失败 → **draft 永远转不了正**。这是 OpenKB 明确避开的坑，其 schema 文件里写死了这条规矩：

> *Do not include YAML frontmatter (---) in generated content; it is managed by code.*

**执行约束**：

1. **LLM 只输出 JSON**，不输出 `---` 块：

```json
{
  "type": "Concept",
  "title": "三大车企 2025 年毛利率对比",
  "description": "一句话摘要",
  "company": ["002594", "601127", "000625"],
  "period": "2025",
  "body_markdown": "## 1. 核心数据摘要表\n| ... | ... |\n..."
}
```

2. **代码负责**：① 由 JSON 拼装 YAML frontmatter；② 写入 `sources` / `updated_at` / `status: draft`；③ 追加 `index.md` 与 `log.md` 条目。
3. **`index.md` 同样由代码维护**（`_update_index`），LLM 不得直接改写——否则索引必然漂移。
4. 校验：若 LLM 输出中出现 `---` 开头行，直接判为违规并重试。

**理由**：把「结构化元数据」与「自然语言正文」的生成责任彻底分开。前者机器保证，后者 LLM 发挥。这是 L2 从「三天可用」变成「三个月仍可维护」的分水岭。

**验收**：100 次编译中，`find_invalid_frontmatter` 失败次数为 0；人为让 LLM 输出含 `---` 时被拦截并重试。

---

### 6.8 P8 ｜ lint 七条规则 + 原子写 + 文件锁

**背景盲区**：v2.1 只说「lint 校验 frontmatter / 断链 / 缺失锚点」，未定义具体规则，也未定义写入的原子性。后者是真缺口——**写到一半崩溃会留下半截 Markdown，Obsidian 打开即坏文档**。

**执行约束（规则清单，共 7+2 条）**：

| # | 规则 | 检查内容 | 失败处理 |
|---|---|---|---|
| 1 | `find_broken_links` | `[[wikilink]]` 目标不存在 | 报错 |
| 2 | `fix_broken_links` / `strip_ghost_wikilinks` | `--fix` 自动清除无效链接 | 自动修复 |
| 3 | `find_orphans` | 无任何入链的孤儿页 | 警告 |
| 4 | `find_missing_entries` | `raw/` 下每份文件都应有对应 wiki 条目 | 报错（覆盖度检查） |
| 5 | `check_index_sync` | `index.md` 与磁盘实际文件是否一致 | 报错 |
| 6 | `find_invalid_frontmatter` | YAML 能否解析 | 报错 |
| 7 | `find_missing_okf_fields` | OKF 必填：非空 `type` + `description`（`index.md`/`log.md`/`sources/` 豁免） | 报错 |
| **8** | **`check_anchor_resolvable`（本项目扩展）** | 每个 `[Ref: doc#P{n}]`：`doc` 在 `data/parsed/` 存在，`1 ≤ n ≤ report.json` 的 `pages`，且 `page_anchored.md` 有该页 META 块（ADR-043） | 报错 |
| **9** | **`check_metric_refs`（本项目扩展）** | 含数字的主张必须带 `metric_refs` 且指向存在的 `facts` 行 | 报错 |

报告统一写入 `reports/lint-{timestamp}.md`。

**写入纪律（不可绕过）**：

- 所有 wiki 文件写入必须经过 `atomic_write()`：
  `tempfile.mkstemp` → 写入 → `os.replace` → **`fsync` 父目录**
- 进程级互斥使用 `portalocker` 文件锁（`.finkb.lock`）
- `_draft → active` 的「提升」操作必须是**单次原子移动**，不得复制后删除

**理由**：规则 4 同时承担了 v2.1 里「知识空白分析（二期）」的 MVP 版本——它直接告诉你「哪份原文还没被编译进知识库」。

**验收**：`python -m finkb.lint` 零错误；人为植入断链、假锚点、无源数字，三条分别被 1/8/9 捕获；`kill -9` 中断写入后文件仍可被 YAML 解析。

---

### 6.9 P9 ｜ 金额三维建模 + 重述按真实日期排序

**背景盲区（两个）**：

1. **概念混淆**：v2.1 的 `unit_raw ∈ {元, 千元, 万元, 亿元, %}` 把三种完全不同维度的东西塞进一个字段——「元/千元/万元」是**记账单位的缩放**，「人民币」是**货币**，「%」根本不是货币。后果：`value_norm` 一列里混着 `7720.00`（亿元）与 `25.10`（%），宽表聚合出的列语义混乱，且无法在 SQL 层声明「这一列必须是百分比族」。
2. **重述排序脆弱**：P4 的 `facts_dedup` 按 `source_doc` **字符串降序**取最新口径。但 `"002594_2025_年报"` 与 `"002594_2026_半年报"` 的字符串序 **不等于时间序**——2026 半年报披露的是 2026 中期数，不是 2025 年报的重述版。**真出问题时取错口径且极难发现。**

**执行约束**：

1. `facts` 表按 FIBO 的 `AmountOfMoney` 建模思想拆分为正交维度：

| 字段 | 语义 | 来源思想 |
|---|---|---|
| `unit_family` | `money` / `percent` / `ratio` / `count` | FIBO `PercentageMonetaryAmount` 独立成类 → 百分比不应与金额同列 |
| `currency` | `CNY`（ISO 4217） | FIBO `Currency` / `hasCurrency` |
| `unit_of_account` | `元` / `千元` / `万元` / `亿元` | FIBO `UnitOfAccount`（记账单位 ≠ 货币） |
| `norm_unit` | `亿元` / `%` / `倍` / `个` | 归一化后的目标单位，供宽表列名自解释 |
| `report_date` | 公告日期（**取自 `raw/manifest.csv`，已有现成数据**） | FIBO `Reporting: has report date` |
| `reporting_party` | 合并范围主体 | FIBO `Reporting: reporting party` |

2. `facts_dedup` 排序改为 **`report_date DESC, source_doc DESC`**（日期优先，文件名兜底）。
3. 宽表视图的每一列必须声明所属 `unit_family`，并在 lint 中断言（如 `auto_gross_margin_pct` 必须为 `percent` 族）。

**理由**：这一条把 P4 从「能跑」提升到「跑对」。`report_date` 在 `manifest.csv` 里已经有了，成本几乎为零，但避免了未来一个极难排查的口径错误。

**验收**：插入同一 `period` 但 `report_date` 不同的两条记录，`facts_dedup` 取到日期更晚者；人为打乱 `source_doc` 字典序后结果不变；`unit_family` 断言在插入百分比指标到金额列时失败。

---

## 7. 各层详细设计

### 7.1 L0 凭证层

**解析链路**

- MinerU 解析，产出 `full.md` + `content_list.json`（含 `page_idx`）。
- **端点可配置 + 离线 fallback**：`mineru_client` 的 API base / 路径 / 轮询参数全部走配置；无 token 或网络不可用时，降级为 `pypdf` 逐页抽取（质量较低但流程可跑通，用于测试）。

**物理页码锚定**（按 P6 执行，不注入 META 注释）

- `physical_page = page_idx + 1`
- 页码真相：`data/parsed/<doc>/report.json` 的 `pages`（总页数）+ `page_anchored.md`
  的逐块 META（ADR-043）；不产出 `sources/<doc>.json`（ADR-029）
- `data/parsed/<doc>/full.md` 保持干净（无任何 `<!-- META -->` 残留）
- 对外锚点恒为 `[Ref: {doc_id}#P{n}]`，对内抽取使用 `blocks[].table_id` 与 `page_range`

**跨页表格缝合**（原创算法，`awesome-llm-apps` 仓库内 0 参照）

- 触发条件：第 N 页末尾为表格块，且第 N+1 页起始为表格块。
- 判定为「续接」需同时满足：列数一致 + 首行为数据行（无表头/无分隔行）。
- 处理：以第 N 页表头为准，拼接第 N+1 页数据行；两页表格绑定为**同一父节点**，记录 `page_range`。
- 若第 N+1 页表格**重复了表头**：丢弃重复表头，只拼数据行。
- 三页及以上链式拆分需支持递归缝合。
- 输出 `TableBlock`（结构化 rows + has_header + page_range），供 L1 直接消费。

**向量兜底**（补丁 P3 的落点）

- 子块切分：父块 800–1500 字，子块 250–300 字，保留 `parent_id` 与 `page`。
- 向量化：`bge-m3` → `.npy` + 子块元数据 JSON。
- 检索：numpy 余弦暴力 Top-K → `bge-reranker-v2-m3` 精排 → 回溯父块。

**扫描件兜底**：PyMuPDF 逐页渲染（`page.get_pixmap(dpi=150)`，参考 `vision_rag`），用于页码存在性校验。

**验收**：`pytest tests/test_table_stitcher.py` — 3 张典型跨页表行列正确；3 页链式缝合正确；列数不一致时**不**误缝。

---

### 7.2 L1 事实层

**表结构**（DuckDB 单文件，零运维）

```sql
-- ① 指标字典（P2 枚举注入源）
CREATE TABLE metric_dict (
  metric_id    VARCHAR PRIMARY KEY,   -- revenue / net_profit / rd_expense / auto_gross_margin
  metric_name  VARCHAR,               -- 营业收入
  unit_default VARCHAR,               -- 亿元 / %
  category     VARCHAR,               -- 盈利 / 结构 / 研发 / 产能 / 现金流 / 供应链 / 风险
  aliases      VARCHAR[]              -- 非标别名，供 LLM 对齐与 FTS 别名扩展
);

-- ② 事实表（P1 两段式产物；P4 唯一键含 source_doc；P9 金额三维建模）
CREATE TABLE facts (
  fact_id           BIGINT PRIMARY KEY,
  company           VARCHAR,      -- 002594
  period            VARCHAR,      -- 2025 / 2024（年报内同期列）
  metric_id         VARCHAR,
  -- ↓ P9：FIBO AmountOfMoney 的四维正交拆分
  unit_family       VARCHAR,      -- money | percent | ratio | count
  currency          VARCHAR,      -- CNY（ISO 4217）
  unit_of_account   VARCHAR,      -- 元 / 千元 / 万元 / 亿元（记账单位）
  norm_unit         VARCHAR,      -- 归一化目标单位：亿元 / % / 倍 / 个
  -- ↑
  value_raw         DOUBLE,       -- 字面量对应数值
  raw_text_matched  VARCHAR,      -- 原文形象量，如 "77,201,000" / "25.10%"
  value_norm        DOUBLE,       -- 归一化值（代码计算）
  caliber           VARCHAR,      -- 合并报表 / 母公司 / 扣非
  reporting_party   VARCHAR,      -- P9：合并范围主体
  report_date       DATE,         -- P9：公告日期（取自 manifest.csv）
  source_doc        VARCHAR,      -- 002594_2025_年报
  page_num          INTEGER,      -- 物理页码 → L0 锚点
  table_id          VARCHAR,      -- 002594_2025_年报#P42_T01
  match_level       VARCHAR,      -- exact / normalized / numeric
  extracted_at      TIMESTAMP,
  UNIQUE (company, period, metric_id, caliber, source_doc)
);

-- ③ 去重视图（P4 + P9：按真实公告日期排序，而非文件名字符串序）
CREATE VIEW facts_dedup AS
SELECT * FROM (
  SELECT *, row_number() OVER (
    PARTITION BY company, period, metric_id, caliber
    ORDER BY report_date DESC, source_doc DESC
  ) AS rn FROM facts
) WHERE rn = 1;

-- ④ 宽表视图（P2：Text-to-SQL 主入口）
--    定义见 6.2

-- ⑤ 关系边表（替代 GraphRAG，确定性多跳）
CREATE TABLE relations (
  src_type VARCHAR, src_id VARCHAR,
  rel_type VARCHAR,          -- supplier_of / customer_of / joint_venture
  dst_type VARCHAR, dst_id VARCHAR,
  source_doc VARCHAR, page_num INTEGER
);

-- ⑥ 未匹配池（P1：禁止静默丢弃）
CREATE TABLE unmatched_pool (
  id BIGINT PRIMARY KEY,
  observation_json JSON,
  fail_reason VARCHAR,       -- literal_not_found / metric_not_in_dict / schema_invalid
  created_at TIMESTAMP
);
```

**单位归一化**：换算系数表 + 单元测试，**不是 prompt 约束**（元→亿元 ÷1e8，千元→亿元 ÷1e5，万元→亿元 ÷1e4）。

**衍生指标**：同比、占比、差额一律由 SQL 视图定义，LLM 只写查询。

**验收**：

- `pytest tests/test_fact_extraction.py`：≥10 个关键数值与原文绝对一致
- 一条 SQL 出三家车企 2025 毛利率对比，数值可与 L0 对账
- `exact` 匹配率 ≥80%

---

### 7.3 L2 知识层

**6 个维护机制**（v1 仅有第 5 条）

| # | 机制 | 作用 | 实现 |
|---|---|---|---|
| 1 | **YAML frontmatter Schema** | 知识单元身份证 | 字段见 9.2 |
| 2 | **类型化链接** | `related_to` / `supports` / **`contradicts`** / `derived_from` | Markdown 链接 + 显式标注 |
| 3 | **Draft 隔离** | 来源 < 2 的页面进 `_draft/`，不进索引 | 目录状态 + 脚本 |
| 4 | **Lint** | frontmatter / 断链 / 缺失锚点 / 数字对账 | 轻量 Python 脚本 |
| 5 | **Git + 时间轴** | `git diff`/`blame` = 知识变更审计 | `log.md` 程序追加 |
| 6 | 知识空白分析 | 识别覆盖不足主题 | 二期 |

**页面类型**（OKF 兼容；目录骨架按 P5，映射表见 6.5）

| 页面 | 目录 | `type` | 内容职责 |
|---|---|---|---|
| 文档综述 | `summaries/` | `Summary` | 每份年报一份，含 key_claims、实体标注、对外链接 |
| 概念 / 对比 | `concepts/` | `Concept` | 跨文档综合与横向对比（如「三大车企 2025 毛利率对比」）——**数字来自 L1 SQL，不由 LLM 生成** |
| 公司 / 供应商 | `entities/` | `Organization` | 命名实体页；为 `relations` 边表提供可点击入口 |
| 问答沉淀 | `explorations/` | `Exploration` | Sink 产物；通过 lint 后可提升为 `concepts/` |
| 决策 / 风险 | `concepts/` | `Decision` / `Risk` | 按需 |

> 页面正文一律由 LLM 产出 **JSON**（见 P7），frontmatter 由代码拼装，LLM 不得输出 `---` 块。

**探针编译器**

- MVP 仅 3 个探针（R3）：`01_核心财务指标`、`02_主营业务分产品结构`、`05_现金流与资本结构`
- 二期扩至 8 个（01–07 + 风险展望）——**注：本文只给出编号，03/04/06/07/08 没有定义；
  v3 §4 只定义到 05（研发投入/产能出海/现金流资本结构）。二期开工前必须先补这 5 个探针的语义。**
  （2026-11-01 现状：01–05 已实现并跑通 29 份；03 研发投入、04 海外收入于当日补齐）
- **Agentic 编译循环**：检索 → 草稿 → 评估 → 补充检索 → 再生成，直到稳定（参考 `corrective_rag` 的 grade-retry 图）
- 定性主张的锚点核验复用 `agentic_typed_rag_pydanticai` 的 `validate_grounded_answer()` 思路
- 产物一律先进 `_draft/`，lint 通过后转正

**主检索**：SQLite FTS5，jieba 预分词 + `metric_dict.aliases` 别名扩展。

**lint 与写入纪律**：按 P8 实现 7+2 条规则与 `--fix`；所有写入走 `atomic_write()` + `portalocker` 文件锁；`_draft → active` 为单次原子移动。

**验收**：`python -m finkb.lint` 零错误；人为植入假锚点 / 断链 / 无源数字，分别被规则 8 / 1 / 9 拦截；来源 <2 的页面确实停在 `_draft/`。

---

### 7.4 L3 交互层

- **三栏工作台**：左栏目录树 + 变更时间轴；中栏 Markdown 渲染 + `[Ref]` 可点击胶囊；右栏对话 + PDF iframe 页锚跳转
- **路由徽标必须显式**：`SQL` / `Wiki 直读` / `Wiki 摘述` / `深度检索`
- **`[Ref]` 胶囊** → `iframe /pdfs/{doc_id}.pdf#page={n}`
- **「沉淀至 Wiki」** → 进 `_draft/`（不是直接进 vault）→ lint 通过 → Toast 反馈路径
- 三视图见第 8 章
- MCP 接口（二期）：`kb_search` / `kb_page` / `kb_add`

---

### 7.5 L0.5 本体层：概念栈与可追溯链条（v2.3 新增）

**定位**：L1 存「数值」，L2 存「页面」，中间缺一层「含义」。本体层不参与检索排序，只做三件事——**归一化**（同一指标在不同文档的不同写法归到一个概念）、**校验**（会计等式是否成立）、**溯源**（这个概念从哪个标准来）。

它回答三个问题：这个指标**是什么类型的东西**（`Income` 是一种金额，且必须恰好带一个期间）、**在报表里叫什么**（CAS 的「营业收入」）、**和谁有约束关系**（归母净利润 + 少数股东损益 = 净利润）。

#### 7.5.1 四层概念栈

| 层 | 来源 | 内容 | 规模 | 变更频率 |
|---|---|---|---|---|
| **L0 元概念** | CMNS | 金额、表达式、期间、币种、主体、情境 | ~8 | 十年不变 |
| **L1 科目骨架** | FIBO `FND` 域静态导出 | `Income` / `EBITDA` / `OwnersEquity` / `Asset` / `CashFlow` / `Report` / `Ownership` / `Holding` | ~10 | 极低 |
| **L2 国标科目** | CAS 2015 裁剪 | 营业收入、营业成本、净利润、每股收益…… | ~40 → 可扩至 600 | 随准则，年/季 |
| **L3 分析指标** | 自建 | 毛利率、净利率、资产负债率、研发强度 | ~10 → 随时增 | 随需求 |

**关键判断：膨胀发生在 L2/L3，L0/L1 是稳定地基。** 这正是分层的意义——FIBO 靠「域可增、层不变」扛住 298 个文件，本项目照抄这条策略，而不是照抄它的规模。

#### 7.5.2 链条契约：一个指标怎么从本体走到原文

六跳，每跳都可单独修改、单独校验：

| 跳 | 起点 | 终点 | 存储 | 怎么改 |
|---|---|---|---|---|
| 1 | `metric_id` | `concept_id` | `concepts.metric_id` | 改 `config/ontology.yaml` |
| 2 | `concept_id` | 父概念（上溯至 L0） | `concepts.parent_id` | 改 YAML 的 `parent` |
| 3 | `concept_id` | 约束 | `ontology_constraints` | 改 YAML 的 `constraints` |
| 4 | `concept.metric_id` | `facts` 行 | `facts(metric_id, dict_version)` | 重跑抽取 |
| 5 | `fact` | 证据 | `facts(source_doc, page_num, table_id, raw_text_matched)` | 只读，由 P4 保证 |
| 6 | `fact` | 页面断言 | `claims(fact_id) → claims.page` | 重编译页面 |

反查同样成立：从 `page_anchored.md` 的一个页码 → `table_id` → `facts` → `concept` → L0 元概念。

**可机器阅读的定义**：`python -m finkb.ontology chain --metric revenue --format json` 输出整条链（概念 / 约束 / 事实 / 证据 / 页面），供下游程序消费；`--format text` 供人读。

#### 7.5.3 表结构（三张主表 + 一张成员表）

```sql
-- 概念：四层栈的节点。layer 决定它是元概念还是科目，domain 预留扩展域。
CREATE TABLE concepts (
  concept_id     VARCHAR PRIMARY KEY,  -- 'cmns:MonetaryAmount' / 'cas:营业收入' / 'self:gross_margin'
  label          VARCHAR NOT NULL,
  layer          VARCHAR NOT NULL,     -- L0 / L1 / L2 / L3
  domain         VARCHAR NOT NULL,     -- meta / finance / segment / esg
  basis          VARCHAR,              -- 标准来源：cmns / fibo / cas / self
  basis_uri      VARCHAR,              -- 标准内可定位地址（CAS 元素名 / FIBO 类 IRI）
  definition     TEXT,
  parent_id      VARCHAR REFERENCES concepts(concept_id),
  metric_id      VARCHAR,              -- 链接 metric_dict（L2 多数有，L3 为 NULL）
  unit_default   VARCHAR,
  formula        VARCHAR,              -- L3 派生公式（DSL）
  expression_args VARCHAR[],           -- L3 参数概念
  source_uri     VARCHAR,              -- 标准文件出处（可追溯）
  valid_from     VARCHAR,              -- 版本有效期（补丁 O3）
  valid_to       VARCHAR
);

-- 概念间关系：通用边表，rel_type 是开放字符串（这是最重要的扩展预留）
CREATE TABLE concept_relations (
  src_concept VARCHAR NOT NULL,
  rel_type    VARCHAR NOT NULL,  -- is_a / derived_from / equivalent_to / disjoint_with / part_of
  dst_concept VARCHAR NOT NULL,
  attrs_json  VARCHAR,
  source_uri  VARCHAR
);

-- 约束：只做校验，不做推导。表名不能叫 constraints——那是 SQL 关键字
CREATE TABLE ontology_constraints (
  constraint_id VARCHAR PRIMARY KEY,
  concept_id    VARCHAR NOT NULL,
  rule_type     VARCHAR NOT NULL,  -- acct_equation / range / required_period / unit_check
  expression    VARCHAR NOT NULL,
  tolerance     DOUBLE,            -- 空则用校验器默认容差
  source_uri    VARCHAR,           -- 优先从 CAS 计算链接库导入，不手写
  note          VARCHAR,
  severity      VARCHAR DEFAULT 'error'  -- error=会计恒等式 / warn=允许合法例外
);

-- 维度种类：封闭词表，不是每份年报配一份。标记载荷决定「哪张表是分部表」
CREATE TABLE dimensions (
  dim_kind VARCHAR PRIMARY KEY,   -- product / industry / region / customer
  label    VARCHAR NOT NULL,      -- 分产品 / 分行业 / 分地区
  markers  VARCHAR,               -- 表头与章节里出现的判定词，JSON 数组
  note     VARCHAR
);

-- 维度成员：开放集合，从报表行名自动抽取，跨公司可归并（补丁 O1）
CREATE TABLE dimension_members (
  member_id  VARCHAR PRIMARY KEY,  -- 'product:新能源汽车'（种类:归一化标签）
  dim_kind   VARCHAR NOT NULL,
  label      VARCHAR NOT NULL,
  parent_id  VARCHAR,              -- 成员层级：境内/境外 → 细分区
  aliases    VARCHAR,              -- 见过的所有原始写法，`|` 分隔
  first_seen VARCHAR,              -- 首次出现的 source_doc
  facts      INTEGER DEFAULT 0     -- 被引用次数
);
```

> **落地状态（v2.3.2，2026-11-02）**：概念 / 关系 / 约束三表已实现于
> `finkb/src/finkb/ontology/schema.py`，YAML 生成器在 `finkb/tools/build_ontology.py`，
> 真相源是 `finkb/config/ontology.yaml`（**62 概念 / 71 关系 / 10 约束**）。
> 维度两表落在 L1 侧（`finkb/src/finkb/fact_engine/store.py`），因为它们是**事实的坐标**，
> 不是概念的定义——首批 **3 种维度 / 49 个成员 / 228 条分部事实全部带 member_id**。
> 四处与本节设计不同，均被真实数据逼出来：
> ① 表名 `constraints` → **`ontology_constraints`**（SQL 关键字）；
> ② 约束新增 **`severity`** 两档——原设计默认「违反即错误」，但实测 `归母净利 ≤ 利润总额`
> 在 17 组上违反，核对后发现是**约束本身错**（所得税为负或少数股东损益为负时合法反转），
> 见 DEVLOG ADR-053。现 error 只放会计恒等式，其余降为 warn；
> ③ `dimension_members` 从「(dimension, member_code) 联合主键 + 裸维度名」改为
> **单 `member_id` 主键 + `dim_kind` 外键 + `parent_id` 层级 + `aliases` 别名列**——
> 因为同一个成员在不同年报里有多种印法（「新能源汽车」/「其中：新能源汽车」），需要别名收敛；
> ④ `facts` 另加 `period_basis`（期间口径记号，见 ADR-056），与本体无关但同属「一个数字
> 必须带上它成立的条件」。

#### 7.5.4 三个扩展补丁（对抗膨胀，v2.3 新增）

| 补丁 | 缺什么 | 怎么补 | 成本 |
|---|---|---|---|
| **O1 维度成员标准化** ✅已落地 | `segment_facts.dimension` / `segment_name` 是裸字符串，跨公司无法归并（比亚迪「汽车业务」vs 赛力斯「新能源汽车」） | 新增 `dimensions` + `dimension_members` 两表，`segment_facts` 加 `member_id` | 0.5 天 |
| **O2 概念等价关系** | `facts_dedup` 只按 `metric_id` 去重，不认「两个 metric 是同一概念」；引入 IFRS 后会重复计数 | `concept_relations` 增 `equivalent_to` / `disjoint_with`；查询层加概念归并视图 | 1 天 |
| **O3 事实版本标记** | `facts` 无 `dict_version`，口径变更后新旧事实不可比 | `facts` 加 `dict_version INTEGER`；`constraints` 加 `source_uri` | 0.5 天 |

#### 7.5.5 扩展情景下的表结构影响（回答「会不会膨胀」）

| 情景 | 概念数 | 表结构改动 | 真正成本 |
|---|---|---|---|
| 现状（14 家 / 34 指标） | ~70 | — | — |
| 扩到全 A 股财报科目 | 400–600 | **0 张**（YAML 增长） | 别名需从 CAS 5008 条标签自动生成 |
| 扩到多维交叉（地区/渠道/客户） | +500 成员 | **+1 张**（`dimension_members`） | 维度标准化（补丁 O1） |
| 扩到多准则（CAS + IFRS） | ×2 | **+1 张**（等价关系） | 等价映射（补丁 O2） |
| 扩到非财务（ESG/治理/股权） | +200 | **+1 列**（`domain`） | 新开域，不动老域 |

最坏情况：8 张表 → 11 张表，**增量 3 张，不重构**。概念数从 34 涨到 600 是同一 schema 的 600 个实例，不是 600 段逻辑。

#### 7.5.6 边界：不做什么

- **不引入 OWL/RDF 运行时**：无 reasoner、无图数据库。`concept_relations` 是普通边表，遍历用 SQL 递归 CTE。
- **不做自动推理**：约束只做**校验**（写入时检查等式是否成立），不做**推导**（不自动填充缺失值）。
- **不复制标准文件**：CAS 有明确版权声明（`All rights reserved`，未经书面许可不得复制），只把**科目名**作为 `basis` 参考写入配置，不打包 xsd/xml 分发。
- **不做全量导入**：FIBO 闭包 291 个类里只取约 10 个种子；CAS 3418 项里只取约 40 个科目。

#### 7.5.7 为什么维度不需要「每份年报配一份配置」

常被问：维度表能解决膨胀吗？如果每来一份新年报都要重写一份维度定义，那它反而成了新负担。

**不会，因为维度的种类和成员是两种性质的东西**：

| | 维度**种类**（dim_kind） | 维度**成员**（member） |
|---|---|---|
| 谁决定 | 会计准则的披露要求 | 公司当年怎么写 |
| 规模 | **封闭**：分产品 / 分行业 / 分地区 / 分客户，个位数 | **开放**：每家公司每家年报都不同，无上界 |
| 存储 | `dimensions` 表，人工登记一次 | `dimension_members` 表，抽取时自动新增 |
| 新公司/新年度要不要改 | 不要 | 不要 |

抽取链是三步，全自动：

1. **认表**——表头、`section_path`、行内标记任一命中 `dimensions.markers` 即认定是分部表；
2. **认成员**——取该表第一列（行名）作为成员原文；
3. **收敛**——`_norm_member` 去符号、剥「其中」前缀得到 `member_id`，原文进 `aliases`。

所以新增 100 家公司的年报，`dimensions` 一行不动，`dimension_members` 自己长。
真正需要人判断的只有「跨公司成员归并」（比亚迪「汽车业务」vs 赛力斯「新能源汽车」该不该算同一个），
那是**数据治理**，不是**每份文档的配置**——把它做成配置项才会真膨胀。

**边界**：如果将来出现「一行一成员的矩阵表」（分行业 × 分地区交叉），当前 `segment_facts`
的一行一成员表达不了，需要升级成 `fact × dim` 关联表。这是唯一预判到的维度级重构点。

---

## 8. 知识沉淀模型与可视化（本方案与 LLM Wiki 的核心差异）

### 8.1 沉淀的不是「一种东西」，而是「三类可分别验证的凭据」

| 沉淀物 | 沉到哪 | 权威源 | 验证方式 |
|---|---|---|---|
| **数值事实** | DuckDB `facts` 表 | 数据库 | 一条 SQL 可复算 |
| **实体关系** | DuckDB `relations` 表 | 数据库 | 可查表、可溯源到页 |
| **定性叙述** | Markdown 页面（frontmatter + 类型化链接） | 页面 | 每条主张挂锚点，可反查原文 |

**根本差别**：在 Obsidian 式 LLM Wiki 中，数字是写在正文里的文字；在本项目中，**数字不进入文本层，页面里只有指向数据库的指针**。

### 8.2 与 LLM Wiki / Obsidian 的逐项对比

| 维度 | LLM Wiki / Obsidian | FinKB |
|---|---|---|
| 沉淀物形态 | 单一：Markdown 页面 | 三类：数值表 / 关系表 / 页面 |
| 数字在哪 | 页面正文（可被改写、可幻觉） | DuckDB，页面只放 `[Ref]` 指针 |
| 关系表达 | `[[链接]]`，只表达「相关」 | 类型化：`related_to`/`supports`/`contradicts`/`derived_from` + `relations` 边表 |
| 结构来源 | 目录层级 + 链接，**自动派生** | frontmatter 的 `type`/`company`/`period`，**显式声明** |
| 图谱是什么图 | 一张**关联图**（页 ⟷ 页） | **三图叠加**（见 8.3） |
| 沉淀门槛 | 写入即沉淀 | **写入 ≠ 沉淀**：`draft → lint → active` |
| 机器可校验性 | 弱（只能查断链） | 强（数字可 SQL 复算、锚点可反查原文） |
| 影响面分析 | 做不到 | 可做（见 8.4） |
| 口径冲突 | 无概念 | P4：自动生成 `contradicts` |

### 8.3 三视图：显式投影，而非自动派生

1. **树**——不是目录层级，而是 frontmatter 多维轴，可切三种：
   - 按公司：`002594 / 601127 / 000625`
   - 按财务主题：盈利 / 结构 / 研发 / 现金流 / 供应链 / 风险
   - 按时间：`2025 年报 → 2025 半年报 → 2026 半年报`（含重述标记）

2. **图**——三张图叠加：
   - **链接图**：Markdown 链接（与 Obsidian 等价）
   - **实体关系图**：来自 `relations` 表，**确定性**，非 LLM 抽取的三元组
   - **证据依赖图**：`结论 → facts 行 → 页码 → PDF`。**这张图 Obsidian 做不到**——它的节点只能是文件，而这里的节点是「数据行」与「页」

3. **时间轴**——`git log` + `log.md` + 重述记录

### 8.4 证据依赖图的价值：影响面分析

它可以回答：**「如果第 42 页这个数错了，哪些结论会跟着错」**。
这是溯源与影响面分析，不是关联浏览，也是本项目相对纯 Wiki 方案最实质的增量。

### 8.5 Obsidian 兼容策略：做超集，不做替代

`wiki_vault/` 采用**标准 Markdown + `[[双向链接]]` + YAML frontmatter**，因此：

- **Obsidian 可直接打开，图谱与树免费获得**（零成本）
- Dataview 插件可按 frontmatter 动态生成 8.3 的「三种树」
- FinKB 额外提供它没有的两样：**证据依赖图**（连到数据库行与 PDF 页）、**lint 门禁**（数字对账 + 锚点反查）

```
Obsidian  =  L2 的可视化前端之一（看叙述、看链接图、看多维树）
FinKB     =  它下面的机器可验证结构（事实表、关系表、证据链、门禁）
```

**一句话**：LLM Wiki 把知识沉淀成「人能读的文本」；FinKB 把知识沉淀成「人能读、且机器能验的三类凭据」——文档是脸，数据库与证据链是骨架。

---

## 9. 数据契约（跨模块唯一真相）

### 9.1 Observation（P1 阶段 A 的 LLM 输出契约）

```python
class Observation(BaseModel):
    company: Literal["002594", "601127", "000625"]
    doc_id: str                      # 002594_2025_年报
    period: str                      # 2025 | 2024
    metric_id: str                   # 必须 ∈ metric_dict，否则拒绝
    raw_text_matched: str            # 原文形象量，含千分位/百分号
    value_raw: float                 # 字面量数值，禁止换算
    unit_raw: Literal["元", "千元", "万元", "亿元", "%"]   # 原文所写字面单位
    caliber: Literal["合并报表", "母公司", "扣非", "其他"]
    page_num: int
    table_id: str
```

> **P9 约定**：`unit_raw` 仅记录**原文照抄的写法**。`unit_family` / `currency` / `unit_of_account` / `norm_unit` / `report_date` / `reporting_party` 六个字段**一律由 Python 侧派生**，LLM 不得输出，避免把判断责任交回给模型。

### 9.2 Wiki 页面 frontmatter（**由代码生成，LLM 不输出** —— P7）

```yaml
---
type: Concept              # Summary | Concept | Organization | Exploration | Decision | Risk
description: 三大车企 2025 年汽车业务毛利率对比与归因
title: 三大车企 2025 年毛利率对比
company: ["002594", "601127", "000625"]
period: "2025"
sources: ["002594_2025_年报", "601127_2025_年报", "000625_2025_年报"]
tags: ["毛利率", "分产品"]
status: draft              # draft | active | outdated
updated_at: 2026-01-01
---
```

**字段来源分工**：

| 字段 | 谁写 | 何时 |
|---|---|---|
| `type` / `description` / `title` / `body` | **LLM（以 JSON 形式）** | 编译时 |
| `sources` / `updated_at` / `status` | **代码** | 写入时自动填充 |
| `company` / `period` / `tags` | 代码校验后回填（LLM 可提议，需与 `facts` 对得上） | 编译时 |

`type` 与 `description` 为 OKF 必填项（P8 规则 7 强制）。

### 9.3 锚点格式（贯穿四层，不可变）

```
[Ref: {doc_id}#P{物理页码}]
例：[Ref: 002594_2025_年报#P42]
```

### 9.4 探针输出契约

每条主张 = `{claim_text, anchor, metric_refs[]}`

- 含数字 → 必须有 `metric_refs`（指向 `facts` 行）
- 定性 → 必须有 `anchor`（指向 L0 页）
- lint 校验：任一为空 → 该主张退回 `_draft/`

---

## 10. 评测与消融

- 评测集：`eval/gold_qa.json`，先 15 题（单点 8 / 跨期 4 / 跨司 3），再扩 50 题
- **跨期题口径**（R2 已锁定）：仅使用 ① 2025 年报内同期列；② 2025 vs 2026 半年报。**不使用 2023/2024 年报**
- 每题人工标注**答案物理页码**（Context Precision 的前提，约 1 天工时）
- 三组消融：
  - A：Naive RAG（切块 + 向量）
  - B：混合 RAG（FTS + 父子索引 + rerank，无 Wiki/SQL 层）
  - C：完整方案（三层栈）
- 指标：Faithfulness、Context Precision、数值题准确率（**确定性断言，不依赖 LLM judge**）、跨司对比成功率、分档延迟
- **所有对照数字以实测为准，禁止预估**
- 失败归因：`rag_failure_diagnostics_clinic` 的 P01–P12 分类（P02 chunk boundary 即跨页表格问题）

---

## 11. 路线图与验收

| 阶段 | 内容 | 工时 | 验收标准 |
|---|---|---|---|
| **0. 数据地基** | MinerU 解析 → **按页 JSON 载体（P6）** → 跨页缝合 → TableBlock | 4.5–5.5 天 | 3 张跨页表缝合正确；3 页链式正确；列数不一致不误缝；`full.md` 无 META 残留 |
| **1. 事实层** | DuckDB 建表（**P9 三维建模**）+ 两段式抽取（P1）+ 宽表与枚举（P2）+ 唯一键与重述（P4/P9） | 4.5–5.5 天 | ≥10 数值绝对一致；`exact` ≥80%；一条 SQL 出三家 2025 对比；乱序 `source_doc` 下 `facts_dedup` 结果不变 |
| **2. 知识层** | **目录骨架（P5）** + Schema + 探针编译（3 个）+ **frontmatter 代码化（P7）** + **lint 七条与原子写（P8）** + draft 隔离 + Git | 6.5–7.5 天 | lint 零错误；假锚点/断链/无源数字分别被规则 8/1/9 拦截；`kill -9` 后文件仍可解析 |

| **3. 检索路由** | FTS5+jieba+别名 → BFS → **向量兜底（P3）** → RRF；三级路由 | 4–5 天 | 15 题路由正确率 >90%；定性长尾能召回正确页 |
| **4. 交互层** | 三栏工作台 + 页锚跳转 + 三视图 | 5 天 | 点 `[Ref]` 跳 PDF 对应页 |
| **5. 评测消融** | 15→50 题 + Ragas + 三组消融 | 4 天 | 三组真实数字 |

**合计 ≈ 29–33 个有效工作日**（含 P1–P9 全部补丁增量；全职 6 周，业余 9–11 周）

> **实施状态（2026-11-01）**：知识层（第 2 阶段）已完整跑通，见 `finkb/DEVLOG.md`
> ADR-041～050 与 T-60/T-62。与本文的差异：
> ① P6 已作废，锚点改用 `report.json` 页数 + `page_anchored.md`（ADR-029/043）；
> ② lint 实际为 **10 条**规则（新增 R8 锚点、R9 claim 对账、R10 定性页锚）；
> ③ 页面数字用「正文占位符 + 脚注 + claims 账本」（ADR-044），不是纯 Markdown；
> ④ 已编译 **29 份 / 116 页**（29 摘要 + 70 探针 + 14 实体 + 3 概念），不是单文档；
> ⑤ 检索做了 **FTS5 trigram + 别名扩展**（未装 jieba，无向量兜底），检索路由属第 3 阶段；
> ⑥ 评测集 15 题已建并 15/15 通过（`eval/`）；探针只实现 01/02/05，03/04/06/07/08
> 本文只有编号、没有定义，按二期处理；
> ⑦ `kill -9` 验收只做到「截断页仍可解析」的模拟，未真实 kill 进程。

### 11.1 MVP 砍法（R3，约 11 天可演示）

- **文档**：仅 `002594_2025_年报`
- **探针**：仅 3 个
- **横向扩展**：跑通后再加赛力斯、长安汽车
- **不可砍**：跨页缝合（Phase 0 核心）、**按页 JSON 载体（P6）**、两段式抽取（P1）、向量兜底（P3）、**frontmatter 代码化（P7，投入产出比最高的一项）**
- **可减配**：P8 的 lint 规则先实现 1/6/7/8 四条，其余随迭代补齐
- **可延后**：三视图中的实体关系图、MCP、空白分析、Ragas 扩量

---

## 12. 素材复用映射（awesome-llm-apps/rag_tutorials）

路径：`/mnt/d/自学路径/awesome-llm-apps/rag_tutorials/`

| 素材 | 位置 | 在本方案中的用途 |
|---|---|---|
| `validate_grounded_answer()` / `Citation`/`Answer` 校验器 | `agentic_typed_rag_pydanticai/agent.py` | L2 定性主张的锚点核验（数值已走 SQL，职责更轻） |
| `extract_pdf_pages()` + `locator=f"p{n}"` | `agentic_typed_rag_pydanticai/rag.py` | L0 页码锚定范式；离线 fallback 参考 |
| 11 个离线测试（TestModel + 禁网） | `agentic_typed_rag_pydanticai/test_typed_rag.py` | L1/L2 测试模板 |
| `similarity_score_threshold` 门限 | `deepseek_local_rag_agent/deepseek_rag_agent.py` | 长尾兜底降级判定 |
| LangGraph grade-retry 图 | `corrective_rag` | Agentic 编译循环骨架 |
| 三栏布局 + FastAPI 骨架 + trace | `multimodal_agentic_rag`（backend/server.py + frontend/App.tsx） | L3 起点（注意：Vite React 非 Next.js，`/ask` 是 JSON 非 SSE） |
| P01–P12 失败模式 | `rag_failure_diagnostics_clinic` | 评测失败归因表 |
| PyMuPDF 逐页渲染 | `vision_rag` | 页码校验、扫描件兜底 |
| rerank 调用范式 | `hybrid_search_rag` | 仅长尾档使用 |
| Dockerfile / compose | `knowledge_graph_rag_citations` | 部署编排 |
| `feedback_log.json` 落盘 | `agentic_rag_math_agent/app/streamlit.py` | Sink 草稿池雏形 |

**必须原创**（仓库内 0 命中，已逐关键词实证）：MinerU 客户端与 `content_list` 解析、跨页表格缝合、数值抽取与单位归一化、中文 FTS 分词与别名扩展、Wiki Schema/lint/draft 机制、三级路由、SQL 生成与执行安全。

> Next.js 骨架与 FastAPI SSE 参考仓库其他目录：`generative_ui_agents/ai-deep-research-agent`（Next.js）、`agent_skills/self-improving-agent-skills/backend/app.py`（StreamingResponse）。

---

## 13. 风险与未决

| 风险 | 影响 | 应对 |
|---|---|---|
| **MinerU 在线 API 端点与配额未实测** | 解析链路可能卡住 | 客户端端点全配置化 + `pypdf` 离线 fallback + mock 传输层单测，保证无网也能验证流程 |
| 硅基流动 API 可用性 | 向量兜底（P3）不可用 | 实现降级：FTS + 图 BFS 仍可工作，仅在 UI 标注「语义兜底不可用」 |
| `bge-m3` 是否返回稀疏输出 | 未知 | 本方案不依赖稀疏，不影响架构；若支持可作为长尾增强 |
| 年报表格非标程度超预期 | `exact` 匹配率偏低 | 三级匹配 + `unmatched_pool` 统计，按失败类型迭代别名表 |
| 重述容差阈值需调 | `contradicts` 误报/漏报 | 初期人工抽查 10 条，确定相对误差阈值 |
| **误把 OpenKB 当依赖引入** | 栈不兼容（pageindex/litellm/markitdown），且**丢失数值层** | **硬约束：只抄设计不抄代码**；不引入其任何运行时依赖 |
| **为对齐 OpenKB 而换掉 MinerU** | 表格保真度下降，跨页缝合失去前提 | 明确禁止；MinerU 是财报表格解析的不可替代项 |
| **引入 FIBO 推理栈** | OWL 需图数据库 + reasoner（450MB），14 家年报规模下纯负担 | 只做**静态导出**：取闭包中约 10 个类做 L1 骨架，不引入任何 RDF/OWL 运行时（§7.5.6） |
| **本体层随科目扩张而膨胀** | 概念数 34 → 600 时维护成本失控 | 分层隔离膨胀（L0/L1 稳定）+ 概念是数据不是代码 + 别名/约束从 CAS 导入而非手写；表结构增量 ≤ 3 张（§7.5.5） |
| **CAS 版权与版本** | 国标带 `All rights reserved`，且公开最新版是 2015-03-31，缺 2018 新增行项目 | 只做名称参考不打包文件；2018 修订项（研发费用等）用手工补丁并标注 `valid_from` |
| **`report.json` 体积过大** | 单份年报 JSON 约 0.8–1 MB，可整份载入；`page_anchored.md` 813 KB 也不宜反复整份复制 | `report.json` 直接读；`page_anchored.md` 按页切片扫描 |
| **OKF/OpenKB 规范后续演进** | 目录骨架可能需再调整 | 目录名集中在 `schema.py` 常量中，改动收敛到单处 |

**未决（非阻塞）**：

- 三视图中的「实体关系图」是否进入 MVP（当前列为可延后）
- Ragas 评测是否使用 DeepSeek 作 judge（存在自我偏好，需交叉验证）

---

## 14. 版本变更记录

### v2.3（本版）相比 v2.2

本版为「本体层落地轮」，核心是把 §2.4 的 FIBO/CAS 调研结论转成可执行的概念层设计。

| 项 | v2.2 | v2.3 |
|---|---|---|
| 对 FIBO 的结论 | 「不引入本体」（因无财务报表科目） | **收窄为「不引入推理栈」**：闭包实测 20 文件 / 291 类，取约 10 个做 L1 骨架 |
| 概念层 | 仅 `metric_dict.yaml`（34 条平铺） | **新增 §7.5 四层概念栈**（L0 元概念 / L1 FIBO 骨架 / L2 CAS 科目 / L3 派生指标） |
| 可追溯性 | 事实 → 页码（两层） | **六跳链条**：metric → concept → 父概念 → 约束 → fact → 证据 → 页面 |
| 表结构 | 8 张 | **+4 张**（`concepts` / `concept_relations` / **`ontology_constraints`** / `dimension_members`），`facts` 加 `dict_version` |
| 扩展预留 | 无显式设计 | **三个补丁 O1/O2/O3**（维度成员标准化 / 概念等价 / 事实版本标记） |
| 风险 | 「引入 FIBO 本体」为单条风险 | 拆为推理栈、概念膨胀、CAS 版权与版本三条 |

#### v2.3.1 落地修订（2026-11-02，实现完成后回写）

本体层已实现并跑通（`finkb/src/finkb/ontology/`），落地过程中三处与 §7.5 原设计不同，均已回写正文：

| 原设计 | 实际 | 为什么改 |
|---|---|---|
| 表名 `constraints` | `ontology_constraints` | `constraints` 是 SQL 关键字 |
| 约束只有「违反即报错」一档 | `severity` 分 `error` / `warn` | `归母净利 ≤ 利润总额` 在 17 组上违反，核对后发现是**约束本身错**（所得税为负或少数股东损益为负时合法反转，长安 000625 2024 为例），见 DEVLOG ADR-053 |
| L2 共 40 个科目、全部对齐 CAS | 34 个指标，命中 26 个 | 研发费用 / 租赁负债 / 合同负债等属 2018 新准则，2015 版 CAS 里不存在；按处置规则记 `basis=self` 并在 `valid_from` 标版本 |

实测结果：55 概念 / 64 关系 / 10 约束；L2 的 34 个科目中 18 个命中 `cas_*`、13 个命中
`ifrs-full_*`（共 31 个有标准依据），3 个 `basis=self`（扣非净利 / 租赁负债 / 加权 ROE，
2015 版标准确实没有）。

约束体检 10 条 × 73 组 (公司,期间) → error 0、warn 13。六跳链条在真实库零断点跑通
（`002594 / 2025 / revenue` 一路走到 P11 原文命中与 3 个页面引用）。

L3 派生指标的第五跳不是一条事实、而是一次按公式的确定性计算（`self:gross_margin` → 17.74%），
参数各自指回自己的 L2 概念与事实条数，因此「改一条底层事实会影响哪个派生值」反向可查。

**「可追溯」是门禁项，不是口号**：`finkb.ontology audit` 把 1456 条去重事实逐跳体检，
术语为「断点数」而非「错误数」——

| 跳 | 检查 | 实测 | 门禁 |
|---|---|---|---|
| ① | 概念登记（metric 有没有本体身份） | 0 | 结构性，必须为 0 |
| ② | 祖先链（能否走到 L0） | 0 | 结构性，必须为 0 |
| ③ | 约束覆盖（概念身上有没有校验） | 0 | 结构性，必须为 0 |
| ④ | 证据字段（doc/页码/原文命中） | 0 | 结构性，必须为 0 |
| ⑤ | 页面引用（被哪些 L2 页引用） | 966 条未被引用 | 覆盖率，只报数 |
| ⑥ | 派生可算（L3 在该期能否算出） | 138 组缺参数 | 覆盖率，只报数 |

⑤⑥ 不判错的原因：页面只引用支撑结论的事实，未被引用≠不可追溯；派生缺参数可逐条归因到
「该期没抽到某个 L2 科目」，属 L1 抽取覆盖问题，不是链条断裂。

**百分比只有一种口径**：L2 的披露值本身就是百分数（加权 ROE 存 15.87 = 15.87%），
若 L3 公式算出来的小数（0.1774）也标 `%`，两者相差 100 倍且不会报错。故在
`derive.compute` 单点统一 ×100，全库只此一处「由本项目定义而非抄自披露值」的缩放。

#### v2.3.2 告警收口与维度层落地（2026-11-02，本轮）

约束体检 13 条警告逐条定性：**计算错 0 条 / 数据抽取错 2 条 / 约束设计缺陷 11 条**。
按用户批准的顺序 A2 → A1 → B1 → A3 修复，并把维度表提前到本版落地。

| 项 | 前 | 后 |
|---|---|---|
| 现金流恒等式 | 3 项 `ocf+icf+fcf` | **4 项** `ocf+icf+fcf+fx_effect`，缺项时跳过不判（ADR-055 前身） |
| `acct.equity_split` | `net_assets ≤ total_equity` | **恒等式** `net_assets + minority_interest == total_equity`（允许负少数股东权益） |
| `acct.cost_le_revenue` | 营业成本 ≤ 营业收入 | **删除**（金融业无此项，约束本身不普适） |
| `range.gross_margin` | `-100..100` | `-50..100`（超界说明抽错了行） |
| L2 科目 | 34 | **36**（补 `fx_effect` / `minority_interest`） |
| 现金流选表 | 按行数最多 | **优先「合并现金流量表」连续段**，续页缝合（`selector._hit`） |
| 章节标题判据 | 子串误判（`投资活动` 命中 `筹资活动` 页） | 拼接全文再判起止锚点 |

结果：约束 10 条 × **73 组** → **错误 0 / 警告 0**；全库守恒律与保全额金标 72/72 不变。

**维度层（ADR-057）提前落地**：`dimensions`（`dim_kind` 封闭词表：industry / product / region）
+ `dimension_members`（成员表，带 `parent_id` 与 `aliases`）+ `facts.dim_member_id` 可空外键。
`segment_facts` 加 `member_id`，255 条分部事实**全部**挂上成员（22 行业 / 21 产品 / 15 地区）。
**不需要为每份年报写维度配置**：维度「种类」由会计准则封闭（CAS 35 分部报告就那几类），
维度「成员」由行标签自动抽取并归一化（`其中:` 前缀剥离、别名合并、父行建立层级），
新来的年报走同一套规则——这是「普适性设计」的落点，不是逐文档配置。

**口径标注（ADR-056）**：`facts` 加 `period_basis`（`period` / `annualized`）。
招商银行半年报的加权 ROE 是**年化**披露（13.85）；粗猜按半年折算（÷2）会得到 6.9%，错得离谱，
故只在文本同时出现「报告期」字眼时标 `annualized`，保守阈值宁漏不误标。

**换库事故与修复**：v4 库切换到 `data/facts.duckdb`（1830 条事实）时踩两个坑——
坑-80 单表解析失败拖垮整份文档（601238 丢 68 条，已修为按表 try/except）；
坑-81 重灌后 `claims.fact_id` / `segment_id` 全部错指（代理键跨库不稳定），
已按天然键回映 1118 + 283 条，未解 0。两者均写入 DEVLOG §7。

### v2.2 相比 v2.1

本版为「参考实现吸收轮」，基于对 [OpenKB](https://github.com/VectifyAI/OpenKB) 与 [FIBO](https://github.com/edmcouncil/fibo) 的源码实地核对。

| 项 | v2.1 | v2.2 |
|---|---|---|
| 参考实现 | 仅网页级调研（PieKBS / Sage Wiki 等） | **新增 OpenKB（同范式）与 FIBO（金融本体）源码级核对**，结论见 2.4 |
| L2 目录 | 自定义 `companies/` + `comparisons/` | **P5：`summaries/` `concepts/` `entities/` `explorations/` `reports/` `_draft/`**（对齐 OKF） |
| 页码载体 | 行内 `<!-- META -->` 注释 | ~~P6：按页 JSON~~ → **ADR-029：直接读 `report.json`；锚点用 `pages` + `page_anchored.md` 校验（ADR-043）** |
| frontmatter | LLM 生成（含 YAML） | **P7：代码生成，LLM 只输出 JSON 正文** |
| lint | 「校验 frontmatter/断链」笼统描述 | **P8：7+2 条规则清单 + `--fix` + 原子写 + 文件锁** |
| 金额建模 | `unit_raw` 单字段混装元/千元/% | **P9：`unit_family` / `currency` / `unit_of_account` / `norm_unit` 正交拆分**（FIBO `AmountOfMoney`） |
| 重述排序 | 按 `source_doc` 字符串降序（**不可靠**） | **P9：按 `report_date DESC` 排序**（日期取自 `manifest.csv`） |
| 指标字典 | 假定可外购/对齐 | **明确结论：FIBO 的 `IND` 仅覆盖行情指标，无财务报表科目，`metric_dict` 必须自建** |
| 工时 | 26–30 天 | **29–33 天** |
| MVP | 约 10 天 | **约 11 天**（P6/P7 列入不可砍） |

### v2.1 相比 v2.0

| 项 | v2.0 | v2.1 |
|---|---|---|
| 语料年份 | 2023–2024 | **2025**（用已有数据，不补拉） |
| 语料清单 | 未明确 | 3 家 × 2025/2026 实际文件表 |
| MVP 文档 | 比亚迪 2024 年报 | **`002594_2025_年报`** |
| 待确认问题 | 4 个 | **改为 R1–R4 已锁定决议** |
| 补丁 | 无 | **新增第 6 章 P1–P4** |
| L1 表结构 | 无 `source_doc` 唯一键、无 `raw_text_matched` / `match_level` | **按 P1、P4 重构**；新增 `facts_dedup` / `unmatched_pool` |
| 宽表 | 未定义 | **按 P2 定义 `v_company_annual_wide` + 枚举注入** |
| 向量检索 | 「MVP 可砍，二期再做」 | **按 P3 保留在 MVP**；明确 numpy 而非 `vss` |
| 知识沉淀 | 分散在 L2 小节 | **新增第 8 章**：三类凭据、三视图、证据依赖图、Obsidian 超集策略 |
| 数据契约 | 分散 | **新增第 9 章**统一 |
| 工时 | 25–27 天 | **26–30 天** |

### v2.0 相比 v1（保留）

| 项 | v1 | v2 |
|---|---|---|
| 分层方式 | 按模块 | **按数据类型** |
| 数字来源 | LLM 生成表格 + 事后校验 | **SQL 计算** |
| 主检索 | Qdrant 双路向量 | **FTS5 为主** |
| 稀疏检索 | rank-bm25 + 自建词表 | 取消（FTS5 覆盖） |
| 知识沉淀 | 直接写 vault | **_draft → lint → 转正** |
| 图谱 | 无 | 链接图 + 关系边表 |
| 延迟承诺 | 1.2s（矛盾） | 分档承诺 |
| 评测 | 预估数字 | **全部实测** |
| 排期 | 14 天 | 25–27 天 |
