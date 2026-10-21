# FinKB 开发日志（DEVLOG）

> **给任何接手本项目的 agent 会话**：先读 §1、§2、§3，遇到具体实现问题再查 §5–§7。
> 这份文档的目的是让你**不必重新推演设计**就能接手。
> 维护规则见 §12，**任何改动都必须回写本文件**。

- 文档版本：v1
- 最后更新：2026-10-21
- 当前阶段：Phase 0（L0 凭证层）已完成，Phase 1 未开始

---

## 1. 项目是什么（30 秒版）

**FinKB** 是一个 A 股财报知识库系统，目标是把传统 RAG 的检索能力与 LLM Wiki 的
知识沉淀能力结合，构建「会积累、可审计、可维护」的财报知识库。

**核心不变量（不可违背）**：

> **数字只由 SQL 计算；LLM 只做「语义对齐」与「查询生成 / 结论叙述」。**

完整方案见 [`../FinKB-方案v2.md`](../FinKB-方案v2.md)（v2.1）。本日志只记录
**实现层**的决策与状态，方案层的论证不重复。

---

## 2. 当前状态快照

| 项 | 值 |
|---|---|
| 阶段 | **Phase 0 完成**；Phase 1（DuckDB + 两段式抽取）未开始 |
| 测试 | **98 项全部通过**，离线，约 0.5 秒 |
| 源码规模 | `src/` 1518 行（含 `__init__`），`tests/` 1331 行 |
| 真实数据验证 | 有（pypdf 路径，268 页年报） |
| MinerU 真实联调 | **未做**（无 token） |
| 版本控制 | ✅ **已建 git 仓库**（根目录 `/home/generalblack/code/works/reporting`，分支 `main`）。首次提交 `57f9fed` 覆盖 50 个文件 |

### 2.1 已完成

- MinerU 客户端骨架（预签名上传链路 + URL 直传链路 + pypdf 离线兜底）
- `content_list.json` → 本项目 IR 的适配层
- 页码锚定 Markdown 的解析与渲染
- 运行页眉/页脚与残留页码的统计式清洗
- 跨页表格缝合（含三页链式、重复表头、列数校验）
- 端到端流水线 `pipeline.build_tables()`

### 2.2 明确未做

- DuckDB 事实层、两段式抽取、Wiki 知识层、检索路由、前端
- 向量检索（补丁 P3）——设计已定，代码未写
- 中文 FTS 分词（依赖 `jieba`，未安装）

### 2.3 最大的未验证项

**真实年报的表格结构从未经过缝合器。** P43→P44 这张真实跨页表的结论来自
pypdf 的**文本层观察**，不是 `TableBlock` 级别的验证。详见 §8。

---

## 3. 架构与数据流

### 3.1 四层结构（方案层）

```
L3 交互层   三栏工作台 / 路由徽标 / 三视图
L2 知识层   Markdown Wiki + FTS5 + 链接图 + _draft 隔离 + lint
L1 事实层   DuckDB（数字只由 SQL 计算）
L0 凭证层   PDF + 物理页码锚 + 跨页缝合 + 向量兜底
```

**当前代码只实现了 L0。**

### 3.2 L0 内部数据流（实现层，含格式边界）

```
PDF
 │
 ├─[生产路径] MinerU 在线 API
 │     content_list.json  ──┐
 │                          │
 └─[兜底路径] pypdf 逐页    │
                  │         │
                  ▼         ▼
        ┌──────────────────────────────┐
        │  content_list_to_pages()      │  ← 唯一的 MinerU 格式适配层
        │  OfflinePdfParser.parse()     │  ← 唯一的 pypdf 格式适配层
        └──────────────┬───────────────┘
                       ▼
        ┌──────────────────────────────┐
        │  Page / Block / TableBlock    │  ← 本项目 IR（层内唯一真相）
        └──────────────┬───────────────┘
                       │
                       ├─ strip_running_noise()   打标噪音块、删噪音行
                       ▼
                   stitch_tables()               按「内容块」缝合
                       ▼
                   StitchedTable                 交给 L1
```

**关键约定**：`noise_filter` 与 `table_stitcher` **完全不知道数据来自哪个解析器**，
它们只操作 IR。这是有意的解耦（见 ADR-008）。

### 3.3 落盘格式

页码锚定 Markdown —— L0 的持久化形态，也是人工核对与调试的入口：

```markdown
<!-- META: {"doc":"002594_2025_年报","page":42,"section":"第三节 管理层讨论与分析"} -->
| 项目 | 2025年 | 2024年 |
| --- | --- | --- |
| 营业收入 | 77,201,000 | 60,231,500 |
```

一条 META 开启一个块，块内容持续到下一个 META。

---

## 4. 模块清单与职责边界

| 文件 | 行数 | 职责 | 不该做什么 |
|---|---|---|---|
| `common/models.py` | 185 | IR 定义：`DocMeta` / `Block` / `Page` / `TableBlock` / `StitchedTable` | 不含任何解析或业务逻辑 |
| `parser/markdown_blocks.py` | 294 | 页码锚定 MD ↔ IR 的双向转换；HTML 表格 → 行/网格 | 不做噪音判定 |
| `parser/noise_filter.py` | 257 | 统计签名识别运行页眉/页脚；页码行识别 | 不删除块（只打标，见 ADR-010） |
| `parser/table_stitcher.py` | 220 | 跨页缝合四条件判定 + 链式续接 | 不改写单元格内容 |
| `parser/mineru_client.py` | 483 | MinerU API + `content_list` 适配 + pypdf 兜底 | **唯一**允许接触外部格式的文件 |
| `parser/pipeline.py` | 77 | 串「解析 → 清洗 → 缝合」 | 不承载业务规则 |
| `cli.py` | 236 | 命令行入口；`.env` 解析与注入；产物落盘 | 不做解析算法 |

**依赖方向**（单向，禁止反向）：

```
pipeline → noise_filter, table_stitcher, markdown_blocks
         → (间接) models
mineru_client → markdown_blocks, models
noise_filter, table_stitcher → models
```

`noise_filter` 与 `table_stitcher` **不得** import `mineru_client`——那会破坏格式解耦。

---

## 5. 关键设计决策（ADR）

每条含：**决策 / 理由 / 影响 / 失效条件**。「失效条件」很重要——不要把决策当教条。

### ADR-001 按数据类型分层，而不是按模块分层

- **决策**：L0 凭证 / L1 事实 / L2 知识 / L3 交互。数字归 L1，定性叙述归 L2。
- **理由**：v1 计划把「数值」和「定性知识」混在同一层，让 LLM 生成 Markdown 表格
  再事后校验数字。但 LLM 不擅长算术，校验是补救而非解决。
- **影响**：v1 的 Write-Gate「数字存在性校验」被整体取消。
- **失效条件**：若某天必须让 LLM 直接产出数值（例如纯叙述型报告），分层要调整。

### ADR-002 检索以 FTS5 为主，向量为辅

- **决策**：L2 主检索用 SQLite FTS5 + BM25 + jieba；向量只服务定性长尾。
- **理由**：用户抱怨「检索概率性不可解释」。BM25 命中是**词**，可以展示给用户；
  向量命中是**数**，无法解释。且会计科目/股票代码这类精确匹配 BM25 天然更强。
- **影响**：v1 的 Qdrant 双路向量 + rank-bm25 稀疏向量方案被废弃。
- **失效条件**：语料扩到单主题下 10 万+ 文档、大量同义改写查询时，需要重新权衡。

### ADR-003 不做 GraphRAG，用「链接图 + 关系边表」

- **决策**：Markdown 显式双向链接当图用；需要多跳时查 L1 的 `relations` 表。
- **理由**：语料只有 3 家公司，实体图谱投入产出比极差；GraphRAG 索引成本高。
- **失效条件**：文档量到千级、需要跨文档实体消歧时再考虑。

### ADR-004 L1 采用两段式抽取（补丁 P1）

- **决策**：
  1. **LLM** 只做语义对齐与原值摘录（`raw_text_matched` / `value_raw` / `unit_raw`），
     **严禁换算与四舍五入**；
  2. **Python** 反查原文命中后确定性归一化入库。
- **理由**：A 股年报表格行列名高度非标（「汽车及相关产品」/「新能源汽车」/「汽车整车」
  都可能指同一指标），纯正则无法映射；但让 LLM 做数字运算会引入幻觉。
- **影响**：反查采用三级匹配 `exact` → `normalized` → `numeric`，全失败进
  `unmatched_pool`，**禁止静默丢弃**。`exact` 匹配率是解析质量指标（验收 ≥80%）。
- **失效条件**：若表格格式高度规整（如监管模板统一），可退化为纯代码解析。

### ADR-005 预建宽表视图 + 枚举注入（补丁 P2）

- **决策**：`facts` 是 EAV 纵表，但给 Text-to-SQL 提供 `v_company_annual_wide` 宽表；
  System Prompt 强制附带完整 `metric_dict` 枚举。
- **理由**：LLM 面对纵表写多指标跨期对比需要大量 Self-JOIN / `CASE WHEN`，易出错；
  且会臆造指标名导致空集。
- **失效条件**：指标数极大（>100）时宽表列会爆炸，需改回纵表 + 模板化 SQL。

### ADR-006 MVP 保留向量检索，用 numpy 不用 DuckDB `vss`

- **决策**：L0 兜底链路用 `bge-m3` 稠密向量 + `bge-reranker-v2-m3`，
  存储用 **numpy 暴力检索 + `.npy`**。
- **理由**：预计 3k–6k 个子块 × 1024 维 float32 ≈ 12–24MB，点积毫秒级；
  `vss` 扩展需联网 `INSTALL`，破坏离线可复现性。
- **失效条件**：语料扩到 10 万块以上，或需要元数据过滤的 ANN 时。

### ADR-007 唯一键含 `source_doc`，重述视为知识库特性（补丁 P4）

- **决策**：`UNIQUE (company, period, metric_id, caliber, source_doc)`；
  `facts_dedup` 视图按 `source_doc` 降序取最新口径；跨年报同期间数值冲突时
  自动生成 L2 的 `contradicts` 记录。
- **理由**：2025 年报内含 2024 同期数（可能重述），与原口径可能不一致，
  用旧约束会主键冲突丢信息。
- **影响**：冲突不是 bug，是 Wiki 的价值——把矛盾显式暴露供人裁决。

### ADR-008 IR 与外部解析格式解耦

- **决策**：`Page`/`Block`/`TableBlock` 是层内唯一真相；外部格式只在两个适配函数里出现。
- **理由**：MinerU 字段随版本变化（实测规范里 `middle.json` 已改名 `layout.json`）。
  把格式细节限制在一个文件里，换解析器成本最低。
- **影响**：新增解析器 = 新增一个 `xxx_to_pages()`，其余层零改动。
- **代价**：**清洗是否充分取决于 IR 的块粒度**，同一页眉在 pypdf 里是「块内的行」、
  在 MinerU 里是「独立的块」，两条路径必须分别验证（见 §7 坑-05）。

### ADR-009 噪音识别用「统计签名」，不用固定文案

- **决策**：把候选行的数字替换为 `#` 得到签名，签名在超过阈值比例的页面重复出现
  即判定为运行页眉/页脚。
- **理由**：硬编码「比亚迪股份有限公司」换一家公司就废；统计特征天然可迁移。
- **参数**：`min_pages=3`、`min_ratio=0.30`、阈值硬下限 2、
  `scan_head_lines=2`、`scan_tail_lines=2`、签名有效字符 ≥4、长度 ≤80。

### ADR-010 噪音「打标不删除」

- **决策**：删噪音**行**；若块因此变空则置 `is_noise=True` 并保留占位，
  需要物理移除时调 `drop_noise_blocks()`。
- **理由**：① 保留审计信息；② 让缝合能跳过噪音块，即使清洗不彻底也不会误判
  （见 ADR-011）。
- **影响**：`drop_noise_blocks` 后 `block_index` 与页内位置不再一致，
  因此**缝合一律用页内位置索引，不用 `block_index`**。

### ADR-011 缝合判据用「内容块」而非「块」

- **决策**：定位页首/页尾时用 `first_content_index()` / `last_content_index()`，
  跳过 `is_noise=True` 的块。
- **理由**：年报每页页首都有运行页眉。若它被解析成独立块，「第 N+1 页第一个块是表格」
  这一判据直接失效。跳过噪音块使缝合**不依赖清洗是否彻底**。
- **影响**：清洗与缝合是互补的两道防线，不是替代关系。

### ADR-012 表格表头按 `<th>` 判定，不走 Markdown 往返

- **决策**：从 MinerU 的 `table_body`（HTML）直接构造 `TableBlock`，
  首行有 `<th>` **或首行单元格全部 `rowspan≥2`** 才是表头，否则 `header=None`。
  （2026-10-21 实测修订：MinerU 用 `<td rowspan="2">` 做表头，没有 `<th>`，
  原判据在真实数据上失效，见坑-12。）
- **理由**：Markdown 管道表**语法上必须有表头**。若走「HTML → Markdown → 再解析」，
  无表头的跨页续接表会被强行套上表头，缝合时被判为「自带表头的新表」而拒绝拼接。
- **影响**：`html_table_to_markdown` 仍存在（用于落盘与人工核对），
  但**不得**用于构造 `TableBlock`。

### ADR-013 页码行识别要求数字等于物理页码

- **决策**：`is_page_number_line(line, page_number)` 只在数字**等于该页物理页码**时
  返回 True，且仅在文本块头/尾极小范围或「整块只有一个非空行」时启用。
- **理由**：页眉形如 `公司名 年份 报告全文  44`，签名只能删掉文字部分，
  页码 `44` 会作为独立行或独立块残留，进而让缝合误判「页首第一个块是文本」。
  要求数字等于页码是为了避免误删正文里的真实指标值。
- **失效条件**：若页码与物理页码不一致（如正文页码从封面起算），此规则会失效，
  需要改为「数字落在 [page-2, page+2] 容差区间」。

### ADR-014 语料锁定 2025 年，不补拉历史

- **决策**：使用工作区 `raw/` 已有数据；不补拉 2023/2024 年报。
- **理由**：用户明确要求用已有数据。
- **影响**：跨期对比只能靠 ① 2025 年报内的「上年同期」列；② 2025 vs 2026 半年报。
  **重述场景因此变成必然而非边缘**，ADR-007 从防御性设计升级为必需项。

### ADR-015 MinerU 的类型标签优先于统计推断（实测新增）

- **决策**：解析 `content_list.json` 时，`type ∈ {header, footer, page_number,
  aside_text, page_footnote}` 的块**直接标记 `is_noise=True`**；
  `noise_filter` 的统计式清洗降级为**兜底路径**（服务 pypdf）。
- **背景**：此前假设「MinerU 会把页眉页脚丢进 `discarded_blocks`、不进 `content_list`」。
  **实测发现恰恰相反**——pipeline 后端不过滤，而是输出独立块并打上类型标签
  （`type="header"`、`type="page_number"`）。VLM 后端类型更全。
- **理由**：类型标签是**确定性信号**，统计签名是**推断**。有确定信号时不应使用推断。
- **影响**：
  - MinerU 路径上 `noise_filter` 报「剥离 0 行」是**正常现象**，不代表清洗失效
  - pypdf 路径仍必须走统计清洗（没有类型标签）
  - 两条路径的清洗机制不同，测试须分别覆盖
- **失效条件**：若换成不输出类型标签的解析器（或 VLM 后端类型发生变化），
  必须退回统计清洗并重新验证。

### ADR-016 默认使用 `vlm` 后端而非 `pipeline`（2026-10-21 实测）

- **决策**：`MINERU_MODEL_VERSION` 默认取 **`vlm`**；`pipeline` 仅作为成本/兼容备选。
- **实测对比**（同一份 PDF，同样的 P43–P44，`pipeline 2.6.4` vs `vlm`）：

| 维度 | pipeline | vlm |
|---|---|---|
| P43 表列数 | 14 | 14 |
| P44 续接表列数 | **12（丢 2 列）** | **14（完整）** |
| 跨页缝合 | ❌ 拒绝（列数不符） | ✅ 成功（P43-P44，14 列 14 行） |
| 表头标记 | `<td rowspan="2">` | 纯 `<td>`，无任何标记 |
| 合并单元格 | 直接把两格并成一格（信息丢失） | 用 `colspan` 表达（可还原） |
| 数字精度 | 有 `86,3880,000.00`、`144, 778,000.00` 两处千分位错误 | 正确 |
| 横向结构 | 被压扁 | 展开成规整网格 |

- **理由**：vlm 后端把「合并」信息编码成 `colspan`（可还原），pipeline 则是**丢掉列**。
  前者可以修复，后者不可逆。
- **代价**：vlm 通常更慢、更贵（具体倍率未测）。
- **失效条件**：若额度紧张或 vlm 不可用，可退回 pipeline，但必须接受
  「复杂跨页表会丢列，缝合会失败」，且应在报告中显式标注。

---

## 6. 被推翻 / 废弃的方案（不要重提）

| 曾提出 | 为什么废弃 | 替代 |
|---|---|---|
| Qdrant 双路向量（dense + sparse） | 主路径不需要向量；稀疏可用 FTS5 免费得到 | ADR-002 |
| `rank-bm25` + 自建 Qdrant 稀疏词表 | 不产出可持久化稀疏向量，需 100–150 行词表映射 | FTS5 |
| Write-Gate「数字存在性校验」 | 归一化后字面量在原文中不存在，必然误杀 + 重试死锁 | ADR-001 / ADR-004 |
| 「Wiki 命中 1.2 秒」延迟承诺 | 只要过 LLM 生成就不可能是 1.2 秒 | 分档承诺：直读 <1s / 摘述 3–8s |
| 补拉 2023/2024 年报 | 用户明确要用已有 2025 数据 | ADR-014 |
| DuckDB `vss` 扩展 | 需联网 INSTALL，破坏离线可复现 | ADR-006 |
| HTML → Markdown → 回表结构 | Markdown 无法表达「无表头」 | ADR-012 |
| 先缝合再清洗噪音 | 判据依赖页首/页尾块，未清洗必然失效 | 先清洗后缝合 |
| 固定文案匹配页眉 | 换公司即失效 | ADR-009 |
| 用 `block_index` 作缝合游标 | `drop_noise_blocks` 后索引会错位 | 用页内位置 |

---

## 7. 踩过的坑与护栏（防止回归）

每条都是**真实踩过**的，修复后有对应测试。改动相关代码前请先读这一节。

### 坑-01 跨页表头会被误判成页眉清掉
- **症状**：缝合后表格没有列名。
- **根因**：跨页续接表在每页都重复表头，签名频次很高，被统计判定为「运行页眉」。
- **修复/护栏**：`line_signature()` 对以 `|` 开头的行直接返回 `None`；
  `_candidate_lines()` 只考察非表格块。
- **测试**：`test_signature_rejects_table_row`、`test_repeating_table_header_is_not_stripped`

### 坑-02 小文档上比例阈值退化为 1
- **症状**：只出现一次的正文行被删。
- **根因**：`int(3 * 0.3) = 0`、`int(5 * 0.3) = 1`，阈值失效。
- **修复**：`max(2, ceil(total_pages * min_ratio))`。
- **测试**：`test_single_occurrence_signature_is_not_noise`

### 坑-03 单文本块页面重复计数
- **症状**：幂等性测试失败——第二次清洗又删了 3 行。
- **根因**：整页只有一个文本块时，头部行与尾部行取自同一块，同一行被计两次，
  阈值被虚高。
- **修复**：`_candidate_lines()` 对候选行按内容去重。
- **测试**：`test_strip_is_idempotent`

### 坑-04 页眉清掉后页码残留
- **症状**：`剥离 268 行`看着正常，但抽查 P44 发现首块仍是 `"44 \n债券 019742…"`。
- **根因**：页眉是「文字 + 页码」两部分，签名只匹配文字部分。
- **修复**：新增页码行识别（ADR-013）。
- **测试**：`test_standalone_page_number_line_is_stripped`、
  `test_page_number_on_separate_line_is_stripped`

### 坑-05 页码作为独立块时清不掉
- **症状**：MinerU 路径测试失败，P41 页码块仍在。
- **根因**：独立页码块既不是页首块也不是页尾块，落在边缘区外。
- **修复**：增加「整块只有一个非空行」的放行条件。
- **测试**：`test_page_number_as_separate_block_is_stripped`

### 坑-06 「文本 + 表格」同块时表格消失
- **症状**：表格凭空不见。
- **根因**：整块被判为 `text`，表格行被吞。
- **修复**：`_segment_block()` 按行切分成 text 段与 table 段。
- **测试**：`test_text_before_next_table_blocks_stitching`

### 坑-07 无 META 的 Markdown 整篇丢失
- **症状**：解析出 0 页。
- **根因**：`flush()` 在 `current_meta is None` 时直接返回。
- **修复**：回退到 `default_doc` 第 1 页。

### 坑-08 MinerU 标题检测字段用错
- **症状**：`section` 继承链失效，所有标题被当正文。
- **根因**：content_list 里标题是 `type="text"` + `text_level` 字段，
  **正文块完全没有 `text_level` 这个字段**（不是 null/0）；`type="title"` 是
  layout.json 的写法。
- **修复**：`_is_heading()` 判断 `text_level is not None`。

### 坑-09 MinerU 跨页续接表被当成有表头
- **症状**：缝合拒绝拼接。
- **根因**：HTML → Markdown 往返强行给无表头表套表头（ADR-012）。
- **修复**：`html_table_to_rows()` 按 `<th>` 存在性返回 `(rows, first_row_is_header)`。

### 坑-10 上传预签名 URL 时带了 header
- **症状**：HTTP 403 `SignatureDoesNotMatch`。
- **根因**：OSS 预签名校验要求 **PUT 不带任何请求头**（含 `Content-Type`）。
- **修复**：`upload_file()` 显式不传 headers。

### 坑-11 分页解析时物理页码全错
- **症状**：`--pages 43-44` 解析出的表，`parent_id` 是 `#P1_T02` / `#P2_T03`。
- **根因**：MinerU 在只解析部分页时，返回的 `page_idx` **从 0 重新编号**；
  我们按 `page_idx + 1` 计算物理页码，得到的是「第 1 页」而不是第 43 页。
- **后果**：若不修正，**所有 `[Ref: doc#P1]` 锚点都会指错页**——对
  「物理页码强溯源」是致命错误。
- **修复**：MinerU 会输出 `type="page_number"` 的块，其 `text` 就是真实物理页码。
  `_build_page_number_map()` 建立 `page_idx → 物理页码` 映射，优先使用它；
  缺该块时才退回 `page_idx + 1`。
- **测试**：`test_page_number_type_overrides_renumbered_page_idx`、
  `test_page_idx_used_when_page_number_missing`

### 坑-12 MinerU 用 `<td rowspan>` 做表头，不用 `<th>`
- **症状**：P43 证券投资表明明有表头，却报 `header=无`。
- **根因**：ADR-012 当初假设「首行有 `<th>` 才是表头」。**实测 MinerU 的表头是
  `<td rowspan="2">`**，全表没有一个 `<th>` —— 该判据在真实数据上完全失效。
- **修复**：增加兼容判据——首行所有单元格 `rowspan≥2` 时也认定为表头。
- **测试**：`test_rowspan_merged_header_is_detected`
- **教训**：ADR-012 的「不走 Markdown 往返」结论仍然正确，但「按 `<th>` 判定」
  这一半是**错的**。设计决策要标注验证来源，未实测的假设必须显式标记。

### 坑-13 `rowspan` 未展开时留下全空垃圾行
- **症状**：P43 表多出一条全空行，`rows_count` 比实际多 1。
- **根因**：`<td rowspan="2">` 占两行，但 MinerU 在第二行仍输出 1 个空 `<td>`。
  解析器不展开 rowspan，于是这条空行被当成数据行。
- **修复**：`html_table_to_rows()` 剔除全空行，并同步维护 `row_has_th` /
  `row_spans` 的下标映射。
- **测试**：`test_rowspan_artifact_empty_row_is_dropped`

### 坑-14 `colspan` 未展开导致合并表列错位
- **症状**：缝合后表格看着「行数对、列数对」，但 P44 的数值整体左移 2 列。
- **根因**：vlm 用 `colspan="3"` 表示「这一格横跨三列」。解析器不展开时，
  一个 cell 被当成一列，其后的所有数值就整体前移。
- **危害等级**：**比缝不上更高**。缝不上会报错，错位则是**静默产出错误数据**，
  一路流进 L1 事实层。
- **修复**：`_HTMLTableExtractor` 展开 `colspan`，被跨越的列补空字符串占位。
- **测试**：`test_colspan_is_expanded_to_keep_column_positions`、
  `test_stitched_rows_have_uniform_width`

### 坑-15 vlm 的表头不做任何标记
- **症状**：`header=无`，表头行被当成数据行，`rows` 多 1。
- **根因**：vlm 的表头是普通 `<td>`，既无 `<th>` 也无 `rowspan`——
  ADR-012 的两条判据同时失效。
- **修复**：增加保守统计判据——**首行没有任何「纯数值」单元格、且第二行至少有一个**
  时判为表头。实测校验：P43 首行 14 个中文列名（0 纯数值）→ 表头；
  P44 续接表首行含多个金额 → 不判表头。`2025年` 这类带数字但非纯数值的单元格
  不会干扰判定。
- **测试**：`test_vlm_style_header_inferred_without_th_or_rowspan`、
  `test_data_row_is_not_inferred_as_header`、`test_header_with_year_label_is_still_header`

### 坑-16 分批解析合并时 `page_idx` 冲突
- **症状**：268 页合并后只解析出 **200 页**，且前 68 页的锚点被错映射到 201-268。
- **根因**：**每一批**的 `page_idx` 都从 0 重新编号。直接拼接 content_list 会让
  批次2 的 `{0:201, 1:202...}` 覆盖批次1 的 `{0:1, 1:2...}`，页码映射互相踩踏。
- **修复**：合并时把后续批次的 `page_idx` **整体加上前序批次的最大值 + 1**，
  使 `page_idx` 全局唯一；`page_number` 块的值保持不变（它本来就是真实物理页）。
- **待办**：应把「按 200 页自动分批 + 偏移合并」做进 CLI，目前是手工合并。

---

## 8. 未验证项与风险

**接手前必须知道：哪些结论是验证过的，哪些是推断的。**

| 项 | 状态 | 影响 |
|---|---|---|
| **MinerU 真实联调** | ✅ **已完成**（2026-10-21，P43-44，pipeline 后端） | 端点、上传链路、字段全部跑通；发现三处与文档不符的实情（见坑-11/12/13） |
| **真实年报表格结构缝合** | ✅ **已验证成功**（vlm 后端） | P43-P44 缝合为 14 列 × 14 行，列位、行宽、表头全部正确；`pipeline` 后端则因丢列而失败（见 ADR-016） |
| **MinerU 全量 268 页** | ✅ **已跑**（vlm，分 1-200 / 201-268 两批） | 278 张逻辑表，3 张跨页缝合（含 P89-P91 三页链式，85 行）；6 处因列数不符被拒 |
| vlm 后端单文件页数上限 | ✅ **实测 200 页** | 268 页需分批；pipeline 约 600 页 |
| vlm 后端的性能/成本 | ⚠️ 未测 | 只知「通常更慢更贵」，没有量化倍率 |
| 页眉清洗（pypdf 路径） | ✅ 已验证 | 268 页，剥离 534 行，签名命中 268/268 |
| 缝合算法（构造数据） | ✅ 已验证 | 15 项测试覆盖两页/三页/列数/重复表头/页首非表格 |
| 清洗（MinerU 路径） | ✅ 已验证 | 实测 MinerU 对页眉/页码**打类型标签**，直接标记 `is_noise`，无需统计推断 |
| 硅基流动 API | ❌ 未接入 | 向量兜底（P3）无代码 |
| 中文 FTS 分词 | ❌ 未开始 | `jieba` 未安装 |
| `rowspan`/`colspan` | ⚠️ 部分支持 | 表头 `rowspan` 可识别；`colspan` 仍不展开，会压平列（P44 列数丢失的直接原因） |

### 8.1 一个容易误判的点

P43→P44 那张证券投资表（14 列）在 pypdf 文本层能清楚看到首尾衔接，但
**pypdf 不输出表格结构**。所以「这张表能被缝合器正确处理」至今仍是**推断**，
不是实测。任何基于此的结论都要标注为未验证。

---

## 9. 数据资产与环境

### 9.1 语料

位置：`../raw/`（相对 `finkb/`），清单见 `../raw/manifest.csv`。

| 公司 | 代码 | 可用报告 |
|---|---|---|
| 比亚迪 | 002594 | 2025 年报 / 半年报 / 一季报 / 三季报；2026 半年报 / 一季报 |
| 赛力斯 | 601127 | 同上 |
| 长安汽车 | 000625 | 同上 |

- **MVP 主文档**：`002594_2025_年报.pdf`（7.18 MB，268 页）
- 腾讯/新浪等外部行情数据不涉及

### 9.2 运行环境

| 项 | 值 |
|---|---|
| Python | 3.12.3（系统 Python，PEP 668 externally-managed） |
| 虚拟环境 | `../.venv`（`--system-site-packages` 创建） |
| 安装项目 | `cd finkb && ../.venv/bin/pip install --no-build-isolation -e .`（装后可用 `finkb` 命令） |
| 跑测试 | `cd finkb && ../.venv/bin/python -m pytest` |
| 继承的系统包 | duckdb 1.5.3、pytest 9.0.3、pydantic 2.13.4、numpy、pandas、requests |
| 额外安装 | **pypdf 6.21.1**（唯一需要手动装的） |
| 未安装 | `jieba`（Phase 3 需要）、`PyMuPDF`（扫描件兜底需要） |

> 为什么用 `--system-site-packages`：系统 Python 受 PEP 668 保护，直接 `pip install`
> 会被拒绝；从零装全套媒体包又太慢。继承系统包 + 只补 pypdf 是最省时的组合。

### 9.2.1 配置（`config/.env`）

真实解析需要 MinerU token，通过 `config/.env` 注入。

| 项 | 值 |
|---|---|
| 配置文件 | `finkb/config/.env`（**已被 .gitignore 忽略**） |
| 模板 | `finkb/config/.env.example`（**会提交**） |
| 注入方式 | `finkb/cli.py` 的 `apply_env_file()` 写入 `os.environ`，再由 `MineruConfig.from_env()` 读取 |
| 优先级 | 已存在的环境变量**优先**，`.env` 不覆盖它们 → 支持 `MINERU_API_TOKEN=xxx <cmd>` 临时覆盖 |

支持的变量：`MINERU_API_TOKEN`（必填）、`MINERU_BASE_URL`、`MINERU_MODEL_VERSION`、
`MINERU_LANGUAGE`、`MINERU_POLL_INTERVAL`、`MINERU_POLL_TIMEOUT`。

**刻意不引入 python-dotenv**：格式足够简单，自带 20 行解析器（支持 `#` 注释、
`export` 前缀、引号），少一个依赖就少一处离线风险。若将来 `.env` 格式变复杂
（多行值、变量插值）再考虑换。

> 缺 token 时 `MineruConfig.from_env()` **立即抛错**，而不是带着空 token 发请求——
> 那样只会得到难以理解的 401。

### 9.3 版本控制

**已建 git 仓库**，仓库根在 `/home/generalblack/code/works/reporting`（**不是** `finkb/`）。

```
reporting/               ← git 仓库根
├── .gitignore
├── FinKB-方案v2.md      ← 方案文档
├── CLAUDE.md            ← 早期工作约定
├── raw/                 ← 原始 PDF（PDF 被 ignore，manifest.csv 入库）
├── scrape-cninfo*.mjs   ← 年报抓取脚本（数据获取链路，入库）
├── _ref/                ← 参考资料（见 9.4）
└── finkb/               ← 项目代码
```

选根目录而非 `finkb/` 的理由：年报抓取脚本、语料清单、方案文档都属于本项目，
分仓会导致引用断裂（DEVLOG 里写的是 `../FinKB-方案v2.md`）。

**约定**：

- 分支 `main`；本地身份 `FinKB Dev <dev@finkb.local>`（占位，可改 `git config user.name`）
- 忽略项：`.venv/`、`raw/*.pdf`（约 40MB）、`*.duckdb`、`*.npy`、`__pycache__`、
  `.pytest_cache`、`.env`、`*:Zone.Identifier`
- **入库项**：源码、测试、文档、`raw/manifest.csv`、抓取脚本、`_ref/`
- 提交后仓库体积约 744K

L2 知识层的审计机制（`git diff` 追溯知识变更）依赖此仓库，所以这件事必须在
Phase 2 之前完成——现已完成。

### 9.4 参考资料（已移除，保留恢复方式）

**当前工作区没有第三方参考材料。** 此前 `_ref/` 下曾有一份快照，已按项目所有者
要求于 2026-10-21 移除。

**恢复方式**（只读参考，不是本项目代码）：

```bash
cd /home/generalblack/code/works/reporting
git show 57f9fed --stat -- _ref            # 查看清单
git checkout 57f9fed -- _ref               # 恢复到工作区
```

曾收录的两份材料，供判断是否值得恢复：

| 目录 | 内容 | 状态 |
|---|---|---|
| `_ref/openkb_src/` | **OpenKB**（PageIndex / Vectify AI 团队），Apache-2.0，13 个 Python 文件约 5521 行 | ⚠️ **不完整、不可运行**的部分快照 |
| `_ref/fibo_src/` | FIBO 金融业务本体 RDF | 仅 4 个抽样文件，非完整本体 |

**关于 OpenKB 的准确认知（重要，避免误解）**：

- 它是被**拍平**的包：源码 `openkb/*.py` 被复制成 `openkb_src/openkb_*.py`，
  但文件内仍是 `from openkb.config import ...` 的包内绝对导入 → **import 必然失败**。
- **缺 `locks.py`（被 6 个文件引用）、`README.md`、`log.py`、`__init__.py`、
  `tests/`、`LICENSE`**，以及 `api_helpers` / `api_models` / `agent/tools` 等被引用模块。
- 因此它**只能当代码阅读**，不能运行、不能直接复用。
- 若将来真要复用 OpenKB 能力（例如它的 lint 或 indexer），应重新获取**完整仓库**，
  而不是依赖这份快照。

**若后续恢复了快照，使用时的三条约束**：

1. 做 L2（lint / frontmatter / draft 流转 / 索引 / 可视化）时可以参考其**组织方式**，
   但不要照搬代码。
2. OpenKB 的检索底座是 **PageIndex**（无向量结构树推理），与本项目
   「FTS5 为主 + 向量兜底」不同——**参考知识层，不参考检索层**。
3. 第三方 Apache-2.0 代码**不得直接 copy 进 `src/`**；复用逻辑须重写并在此记录来源。

---

## 10. 术语表

| 术语 | 含义 |
|---|---|
| **IR** | 中间表示。本项目的 `Page` / `Block` / `TableBlock` / `StitchedTable` |
| **物理页码** | PDF 阅读器里的页序，**1-based**。不是财报正文印刷的页码 |
| **锚点** | `[Ref: {doc_id}#P{N}]`，贯穿四层的溯源标识 |
| **doc_id** | `{股票代码}_{年度}_{报告类型}`，如 `002594_2025_年报` |
| **缝合** | 把被分页切开的同一张表拼回逻辑表 |
| **逻辑表** | `StitchedTable`，缝合产物，L1 抽取的最小单元 |
| **parent_id** | 逻辑表主键，跨页时形如 `002594_2025_年报#P43-P44_T01` |
| **签名** | 一行文本把数字替换成 `#` 后的骨架，用于识别运行页眉 |
| **噪音块** | `is_noise=True` 的块，被打标但不删除 |
| **内容块** | 非噪音块。缝合定位页首/页尾时只看内容块 |
| **续接表** | 跨页表在第 N+1 页的部分，通常**无表头** |
| **EAV** | Entity-Attribute-Value 纵表，`facts` 表的结构 |
| **重述** | 新报告对旧期间数据的会计口径修正 |
| **探针** | 编译期主动发起的财务审计提问（如「核心财务指标」） |
| **`_draft`** | 来源不足 2 个的 Wiki 页面的隔离目录，不进索引 |

---

## 11. 变更记录

> 时间线：2026-10-21 之后的条目来自 git 提交历史，之前的是**重建**的。

### 2026-10-21（当日第七段）—— 全量 268 页
- vlm 后端**单文件上限 200 页**（实测报错 `number of pages exceeds limit (200 pages)`），
  268 页拆为 1-200 / 201-268 两批
- 合并时遇到坑-16（`page_idx` 冲突），按偏移修正后得到完整 268 页
- **全量结果**：278 张逻辑表，3 张跨页缝合（P43-P44 / P58-P59 / P89-P91 三页链式），
  6 处因列数不符被拒并留审计；150/278 张表识别出表头
- 噪音清洗剥离 0 行——因为 MinerU 的类型标签（ADR-015）已直接标记，符合预期

### 2026-10-21（当日第六段）—— vlm 后端验证成功
- `MINERU_MODEL_VERSION=vlm` 跑 P43-44，**跨页缝合成功**：14 列 × 14 行，行宽一致
- 修复两个只在 vlm 上暴露的缺陷（坑-14 / 坑-15）：
  - `colspan` 未展开 → 合并表数值整体左移（**静默错数据，比缝不上更危险**）
  - vlm 表头无任何标记 → 增加「纯数值分布」推断判据
- 新增 ADR-016：默认用 vlm 后端，pipeline 降为备选（vlm 把合并编码成 colspan
  可还原，pipeline 直接丢列不可逆）
- CLI 增加 `--reuse-content-list`：复用已保存的 content_list 重跑后处理，
  改清洗/缝合逻辑时不必重复消耗额度
- 测试数 93 → **98**

### 2026-10-21（当日第五段）—— MinerU 首次真实联调
- **跑通 MinerU 在线解析**（pipeline 后端，`--pages 43-44`，2 页）
- 实测推翻两处假设，修复三个缺陷（详见坑-11/12/13、ADR-012 修订、ADR-015）：
  - 物理页码错位：分页解析时 `page_idx` 从 0 重编号 → 用 `page_number` 块修正
  - MinerU 用 `<td rowspan>` 做表头而非 `<th>` → 增加合并表头判据
  - `rowspan` 未展开产生全空垃圾行 → 剔除
- 新增 `StitchResult.rejections`：被拒绝的疑似续接必须可见（CLI 与 report.json 均输出）
- **P43→P44 仍未缝合**，原因已定位：MinerU 把 P44 的 2 对单元格合并
  （`债券`+`019742`、`24 特国 01`+`113,357,000.00`），14 列变 12 列。
  判据正确拒绝并留下审计记录，**不是本项目 bug**
- 测试数 87 → **93**

### 2026-10-21（当日第四段）
- **新增配置通路**，为 MinerU 真实联调做准备：
  - `config/.env.example` 模板 + `config/.env`（gitignore）
  - `MineruConfig.from_env()`：读环境变量，缺 token 立即失败（不再发空 token 请求）
  - `cli.py` 内置 `.env` 解析器（**不引入 python-dotenv**）
- **新增 `cli.py`**（236 行）命令行入口：
  - `parse` 子命令，产物落 `data/parsed/<doc_id>/`（page_anchored.md / content_list.json /
    tables/*.md / report.json）
  - `--pages 43-44` 分页参数透传到 MinerU `page_ranges`，用于**低成本验证**跨页表
  - `--offline` 用 pypdf 验证链路；退出码 0/2/3/4 区分失败原因
- `request_upload_urls` / `parse_pdf` 增加 `page_ranges` 参数
- 新增 `tests/test_cli_and_config.py`（13 项），测试数 74 → **87**
- `pyproject.toml` 增加 build-system 与 `[project.scripts] finkb`，`pip install -e .` 后
  可直接用 `finkb parse ...`；此前 `python -m finkb.cli` 因 `src/` 不在路径上而失败
- 离线全流程实测通过：真实年报 268 页、剥离 534 行噪音、产物落 `data/parsed/`

### 2026-10-21（当日第三段）
- **移除 `_ref/`**（22 个文件）：项目所有者确认不需要本地保留。
  已从工作区与索引删除，可从 `57f9fed` 用 `git checkout 57f9fed -- _ref` 恢复
- 核查发现该快照**不完整且不可运行**（缺 `locks.py` / `README.md` / `__init__.py` /
  `tests/` / `LICENSE`，且包被拍平但内部仍是包内绝对导入），
  据此重写 §9.4，避免后续 agent 误以为它是可用的完整实现

### 2026-10-21（当日第二段）
- **建立 git 仓库**（根目录 `reporting/`，`main` 分支），首次提交 `57f9fed`：
  50 文件 / 12422 行；`.venv` 与 40MB PDF 已忽略，仓库体积 744K
- 建立 `.gitignore`：忽略 `raw/*.pdf` 但保留 `manifest.csv` 与抓取脚本，
  保证任何环境都能凭清单重建语料
- 发现 `_ref/` 参考资料并入库（**已于当日第三段移除**，见上）

### 2026-10-21（当日第一段）
- 建立本日志体系（`DEVLOG.md` + `AGENTS.md`）
- 按 MinerU 实测规范修正三处：标题检测（`text_level`）、本地文件上传链路
  （预签名 URL + 裸 PUT）、表头判定（按 `<th>`）
- 新增 `test_mineru_noise_path.py`（7 项），覆盖 MinerU 块粒度下的清洗与缝合
- 修复：页码作为独立块时清不掉（坑-05）
- 测试数 59 → **74**

### 2026-10-20
- 新增 `noise_filter.py` + `pipeline.py`
- 缝合改为按「内容块」定位（ADR-011）
- 新增 `Block.is_noise`
- 修复：坑-01 / 坑-02 / 坑-03 / 坑-04
- 测试数 39 → 59
- 真实年报验证：268 页，剥离 534 行噪音
- **确认 P43→P44 是真实跨页表**（用户人工核对）

### 2026-10-19
- Phase 0 起步：`models.py` / `markdown_blocks.py` / `table_stitcher.py` /
  `mineru_client.py`
- 缝合四条件判据落地；39 项测试
- 修复：坑-06 / 坑-07
- 真实 PDF 首次跑通（pypdf 路径，268 页，页码 1..268）

### 更早（不可追溯）
- 方案层工作：v1 计划 → 调研 → v2 → v2.1（含 4 项决议 R1–R4、4 项补丁 P1–P4）
- 细节见 [`../FinKB-方案v2.md`](../FinKB-方案v2.md)，本日志不重复

---

## 12. 日志维护约定（所有 agent 必须遵守）

### 12.1 何时必须更新

| 触发 | 必须做的 |
|---|---|
| 改代码 | §11 变更记录追加一条；若改了设计意图，同步 §5 对应 ADR |
| 新增/删除模块 | §4 模块清单同步；§3 数据流图若变化也要改 |
| 修 bug | §7 追加「坑-NN」：症状 / 根因 / 修复 / 对应测试 |
| 放弃某个方案 | §6 追加一行，写清为什么废弃、替代是什么 |
| 验证了一个「未验证项」 | §8 状态从 ❌/⚠️ 改为 ✅，并写出验证方式与数据 |
| 引入新依赖 | §9.2 更新；若是外部 API，§8 增加一行「未联调」 |

### 12.2 写作要求

1. **写「为什么」，不只写「什么」。** 代码能说明做什么，日志要说明为什么这么做。
2. **决策要带失效条件。** 没有失效条件的决策会被后人当成教条。
3. **区分「已验证」与「推断」。** §8 的表格必须诚实，不要粉饰。
4. **数字要实测。** 不要写「约 70 个测试」，写 `74 项通过` 并注明获取方式。
5. **编号不复用。** ADR / 坑 / 不重排、不复用编号，删掉的留空号。

### 12.3 快速上手清单（新会话）

```bash
cd finkb
../.venv/bin/python -m pytest -q      # 应答：98 passed
```

1. 读 §1、§2 —— 知道边界
2. 读 §5 里与你任务相关的 ADR —— 知道为什么
3. 读 §7 —— 别踩已踩过的坑
4. 读 §8 —— 别把推断当事实
5. 动手；**结束后回写本文件**
