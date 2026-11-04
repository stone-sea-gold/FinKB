# FinKB v3.1：可行改进方案（本体知识库 + RAG 融合）

> 生成时间：2026-11-03 20:40（Asia/Shanghai）
> 依据：本机实测 + 仓库现状（HEAD `bd5734e`，406 项测试通过）+ `finkb_v3_0.md` 计划
> 目标：在现有资产上，用**本地资源可承载、离线可验证**的方式，把 FinKB 升级为「真正的本体论知识库 + 混合 RAG 问答」，而不是重写项目。

---

> **实施状态（2026-11-04 更新）**：本方案 S0–S4 已全部落地并通过验收——
> S0 提交 `9800014`；S1 本体推理 `480c2f4`（ADR-057）；S2 实体治理 `79f507b`（ADR-059）；
> S3/S4 混合检索与三路问答 `5531afc`（ADR-058）。当前 pytest **481 passed**、
> `eval/check.py` 15/15、`eval/check_qa.py` **50/50**、`scripts/check.sh` exit 0。
> 细节与实测数字见 `finkb/DEVLOG.md` §2/§5/§11。

---

## 0. 一页纸结论

### 0.1 推荐方案一句话

**保留现有 L0 解析 / L1 事实 / L2 claims 三层不动，把 L0.5 从「YAML + DuckDB 校验」升级为「OWL 2 RL 本体 + 带血缘的规则推理」，新增 L0 原文块索引与 L3 三路问答；不引入 Kùzu / Neo4j / Qdrant / Java / 大模型本地推理。**

### 0.2 对 v3.0 计划的裁决（摘要）

| v3.0 的关键主张 | 实测结论 | 本方案裁决 |
|---|---|---|
| Kùzu 嵌入式图库 | `pip` 在 Python 3.12 上**找不到 kuzu 发行版** | ❌ 不可用；改用 DuckDB 边表 + NetworkX/递归 CTE |
| Qdrant Local 向量库 | 16K 块 × 512 维约 32MB，numpy 暴力检索毫秒级 | ⏸ 不引入；用 numpy `.npy`（ADR-006 原方案），保留接口 |
| Neo4j / n10s / APOC | 621 条边、14 家公司，规模不匹配 | ⏸ 不引入 |
| Owlready2 + Pellet + SWRL | 本机**无 Java**；Pellet/HermiT 依赖 JVM | ❌ 不可用；改用 `rdflib 7.6 + owlrl 7.6.2`（纯 Python，已实测） |
| OWL 2 RL / Datalog 前向物化 | rdflib+owlrl 在真实数据上跑通：14,208→27,115 三元组、12,907 条推断、6.3 秒 | ✅ 可行，但要改成**批量预计算**，不是查询时推理 |
| 436 页空块需补 OCR | 436 页中 **426 页被跨页表的 page_start/page_end 覆盖**，是续页占位块（ADR-022） | ❌ 误判；不需要 OCR，切块用 page_anchored + 表页区间即可 |
| 36 个科目附注锚定率 ≥90% | 仅 23 个指标能在 section 里找到同名条目；13 个合计类指标（净利润/负债/权益/现金流等）附注不单列 | ⚠ 目标不现实；改为「36 项都有报表页锚，核心 13 项附注锚 ≥80% 文档」 |
| 关系覆盖 14 家公司 | 21 份文档里有 409 张关系类表、15+ 种表头族；现只解析 5 家 | ✅ 可行，但要补表头族 + 实体门禁 + 消歧 |
| 4 周完成 50 题 + 工作台 | 本方案估算 25–30 人日；4 周偏紧 | ⚠ 拆成 4 周 MVP + 6 周完整版 |
| jieba 分词 | trigram + 别名扩展已可用；2 字词靠别名 | ⏸ 可选，不是关键路径 |

### 0.3 本机资源结论

| 项 | 实测 | 对方案的含义 |
|---|---|---|
| 机器 | WSL2 / Intel i5-1035G1 / 4 核 8 线程 | 纯 CPU 可跑；推理与检索都能本地做 |
| 内存 | 7.6 GiB（可用 4.2 GiB） | 不能跑本地大模型；owlrl + numpy 索引峰值 <1 GiB，够 |
| 磁盘 | 927 GB 可用 | 模型/索引/三元组都放得下 |
| GPU | 无 | 稠密向量用云端 API（你之前选的方案 A）或小模型 CPU |
| Java | **无** | 排除 Owlready2/Pellet/HermiT 路线 |
| Python | 3.12.3；已有 duckdb 1.5.3、fastapi、numpy、httpx、pydantic、networkx | 新依赖尽量少；`rdflib/owlrl` 纯 Python 可离线 |
| 网络 | HF 直连被墙；`hf-mirror.com`、ModelScope、PyPI、清华镜像、SiliconFlow 可达 | 模型可下；但正式方案优先用 API embedding，避免 1GB torch 依赖 |

---

## 1. 现有资产盘点（哪些必须保留）

| 资产 | 规模/状态 | 本方案处理 |
|---|---|---|
| L0 解析与切分（`parser/` 3,141 行） | 29 份 / 4,791 页 / 5,117 物理表→6,130 逻辑表 / 表头 93.5% / 守恒律 359/359 / 回查 99.98% | **原样保留**；只补「报表↔附注」锚点 |
| L1 事实层（`fact_engine/` 2,555 行） | facts 1,830 / 36 指标 / 0 未匹配 / 72 金标 | **保留**；新增实体主数据、附注锚点、推断表 |
| L2 知识页与 claims（`wiki/` 1,548 行 + `lint/` 481 行） | 153 页 active / claims 1,578（fact 1,118 / segment 283 / query 177）/ lint R1–R10 0 错 0 警 | **保留**；作为 L3 的「已编译知识」直读来源 |
| L0.5 本体层（`ontology/` 1,783 行） | 62 概念 / 71 关系 / 10 约束 / 3 维度 58 成员 / 六跳链条 / audit 0 断点 | **升级**：加 OWL 导出、定义类、属性公理、推理血缘 |
| 评测（`eval/`） | 15 题 15/15、证据自然键、离线复算 | **保留**；新增文字题/推理题/负例题 |
| 测试（406 项） | 全离线、exit 0 | **保持全绿**；新增测试 ≥80 项 |
| 文档体系 | DEVLOG（ADR 001–056）/ 任务日志 / AGENTS | **必须同步**：新 ADR、状态、未验证项 |

> 结论：**现有约 70–80% 的代码与数据资产可直接复用**；真正要新写的是「本体推理运行时 + 实体治理 + 附注锚点 + 原文块检索 + L3 问答」。

---

## 2. v3.0 计划逐条核验（证据）

### 2.1 图存储：Kùzu 不可用

- `pip index versions kuzu` → **`No matching distribution found for kuzu`**（Python 3.12 / Linux x86_64）。
- 结论：v3.0 的 `KuzuGraphStore` 无法落地；`GraphStoreProtocol` 可以保留，但默认实现必须是 DuckDB/NetworkX。
- 替代：DuckDB 边表 + 递归 CTE（现有 `ontology/chain.py` 已用递归 CTE 做祖先链）+ NetworkX 3.7（已装）做 2–3 跳 BFS。

### 2.2 向量库：Qdrant 不必要

- 按已测算：全语料约 **16,177 块**；512 维 float32 ≈ **32MB**，1024 维 ≈ **63MB**。
- numpy 暴力余弦：16K × 512 点积约 8M 次乘加，8 线程 CPU 毫秒级；ADR-006 早已定「numpy + `.npy`，不用 vss」。
- 结论：先不引入 Qdrant（也避免 `qdrant-client` + 常驻/本地模式带来的部署与测试复杂度）；保留 `VectorBackend` 接口，规模到 10 万块再换。

### 2.3 OWL 运行时：纯 Python 可行，已实测

- 依赖可用：`rdflib 7.6.0`、`owlrl 7.6.2`（纯 Python，无 Java）。
- 真实数据原型（`/tmp/owl_proto.py`，用本项目 62 概念 + 71 关系 + 1,830 facts + 621 edges + 255 segments）：
  - 建图 **14,208 三元组 / 0.2 秒**；
  - `DeductiveClosure(OWLRL_Semantics)` **6.3 秒**，推断后 **27,115 三元组（+12,907 推断）**；
  - 验证：收入 fact 被推断为 `fibo:Income` → `cmns:MonetaryAmount`。
- 结论：**OWL 2 RL 可行**，但 6.3 秒不适合「每次提问都推理」；应作为**批量预计算**（facts/ontology 变更时跑一次），查询读推断结果表。
- 数值阈值类定义（如「担保/净资产 > 0.5」）OWL 2 RL 不能直接算；用**Python Datalog 规则**实现，OWL 侧只做分类/属性公理/传递闭包。

### 2.4 436 页「空块」是续页，不是 OCR 盲区

- 29 份共 4,791 页；page_anchored 覆盖 4,355 页，缺 436 页。
- 436 页中 **426 页落在物理表的 `page_start..page_end` 区间内**（跨页表的续页占位块，ADR-022）；只有 10 页无覆盖（600030 年报 17/48/386、600276 年报 9/186、600900 年报 256、601088 年报 308、601238 年报 1、601318 半年报 1/171）。
- 结论：不需要 OCR；切块以 page_anchored 的块为准（跨页表内容在起始页整表块里），对 10 页无覆盖做人工记录即可。

### 2.5 附注锚点：目标要下调，但可行

- page_anchored 的 META `section` 是条目级标题（如「7、存货（续）」「45、营业收入和营业成本」「24、短期借款」），可做锚点。
- 按 metric 别名匹配 section 的覆盖（文档数/29）：cash 19、revenue 18、inventory 18、operating_cost 17、rd_expense 17、short_term_borrowings 17、lease_liabilities 17、selling_expense 17、admin_expense 17、long_term_borrowings 16、bonds_payable 16、accounts_receivable 16、finance_expense 15、minority_interest 7、eps_basic 5、total_assets 4、customer_deposits 4、loans_advances 3、operating_profit 2、net_interest_income 2、fee_commission_income 2、net_assets 1、insurance_revenue 1；**13 个指标为 0**（net_profit、net_profit_deducted、total_profit、ocf、total_liabilities、roe_weighted、total_equity、icf、fcf、net_cash_increase、fx_effect、current_assets、current_liabilities）。
- 结论：13 个合计类指标在附注里通常不单列，应锚到**报表页**（facts 已有 source_doc/page_num）。目标改为：
  - 36/36 指标都有「报表页锚」；
  - 核心 13 项（现金、收入、成本、存货、应收、研发、三费、借款、债券、租赁）附注锚 ≥80% 文档；
  - 其余按实际覆盖报数，不硬凑 90%。

### 2.6 关系抽取：可扩，但要补表头族

- 21 份文档里有 **409 张**关系类表；表头族 15+，例如：
  - 「关联方|关联交易内容|本期发生额|上期发生额」(15)
  - 「股东名称(全称)|报告期内增减|期末持股数量|比例(%)…」(8)
  - 「项目名称|关联方|期末账面余额|期初账面余额」(8)
  - 「子公司名称|主要经营地|注册资本|注册地|业务性质|持股比例…」(5)
  - 「关联方|关联关系|每日最高存款限额…」(5)
- 现 `relations.py` 只认 4 种精确表头；扩到 14 家需要「表头模式表 + 行标签门禁 + 实体消歧」，不是改一行。
- 结论：v3.0 的「三道防线」方向正确，本方案吸收。

### 2.7 本地 embedding：可行但成本高；API 更现实

- `huggingface.co` 直连 **超时/000**；`hf-mirror.com` 的 `BAAI/bge-small-zh-v1.5` 权重文件 HEAD **200**；ModelScope 同样 200；但 `onnx/model.onnx` 在 BAAI/Qdrant 仓库均 **404**，`fastembed` 元数据里只见英文模型。
- 本地跑 bge-small-zh 需要 `torch`（约 1GB 依赖）；本次安装因 PyPI 文件超时未完成，**速度未实测**。
- 现有 LLM 网关 `opencode.ai/zen/go` 的 `/v1/embeddings` 返回 **404**（只提供 chat）。
- SiliconFlow embeddings 端点可达（无 key 时 401），即**你之前选的方案 A 仍是最现实的稠密向量来源**；需要 `SILICONFLOW_API_KEY`。
- 结论：默认 `SiliconFlowEmbedder`（bge-m3，1024 维，一次性建库）；`LocalTorchEmbedder` 作为可选 extras（离线兜底）；测试用 `FakeEmbedder`（确定性哈希），保证 406 项测试仍离线。

---

## 3. 目标架构 v3.1

```
┌──────────────────────────────────────────────────────────────────────┐
│ L3 问答层（新建 src/finkb/qa/）                                      │
│  Router：事实 / 本体+关系 / 原文混合 三路                              │
│  Answer：claims 账本 + [Ref: doc#P] + 结构化数据                       │
│  Verifier：数字绑定、锚点存在、claim 重算（复用 R7/R8/R9）             │
│  CLI：python -m finkb.qa "问题"；可选 FastAPI /ask                    │
├──────────────────────────────────────────────────────────────────────┤
│ L2 检索层（现有 search/ 扩展）                                        │
│  L2 Wiki FTS5（153 页，保持）                                          │
│  L0 Chunk 索引：16K 块 + FTS5 + 稠密向量（numpy）+ RRF                 │
│  Chunk payload：doc/company/period/page/section/type/linked_concepts  │
├──────────────────────────────────────────────────────────────────────┤
│ L0.5 本体运行时（现有 ontology/ 升级）                                │
│  YAML（单一真相源）→ TBox Turtle + ABox Turtle                         │
│  OWL 2 RL 闭包（rdflib+owlrl，批量预计算）                             │
│  Datalog 数值规则（定义类/风险标签，带 rule_id + evidence_fact_ids）   │
│  属性公理：transitive / inverse / property_chain → 推断边              │
│  推断结果表：inferred_concepts / inferred_edges / inference_runs       │
├──────────────────────────────────────────────────────────────────────┤
│ L1 事实与主数据（现有 fact_engine/ 扩展）                              │
│  facts 1,830 / segment_facts 255 / conflicts 1（不动）                 │
│  新增：entities_master、entity_aliases、note_anchors                   │
│  关系：relations 扩到 21 份文档、补表头族、实体消歧、时态字段           │
├──────────────────────────────────────────────────────────────────────┤
│ L0 凭证（现有 parser/ 不动）                                          │
│  MinerU vlm + 清洗 + 跨页缝合 + 逻辑表切分 + 守恒律门禁                │
└──────────────────────────────────────────────────────────────────────┘
```

### 3.1 与 v3.0 五层的对应

| v3.0 | v3.1 | 变化 |
|---|---|---|
| L1 解析（Docling/MinerU） | 现有 MinerU 管线 | **不重建** |
| L2 本体与推理（Owlready2/Pellet/SWRL） | YAML + rdflib/owlrl + Python 规则 | 去 Java、可离线 |
| L3 混合存储（Neo4j/Qdrant） | DuckDB + numpy | 去服务 |
| L4 联合计算（LlamaIndex Agent） | 自写三路 Router + 工具函数 | 去框架、可测试 |
| L5 生成 | 现有 claims/页锚 + 新合成器 | 复用 |

---

## 4. 本体层设计（「真正的本体论」如何验收）

### 4.1 三层产物

| 产物 | 内容 | 生成方式 |
|---|---|---|
| `config/ontology.yaml` | 单一真相源：概念/关系/约束/维度 + 新增 `object_properties` / `defined_classes` / `property_chains` | 手工/生成脚本维护 |
| `data/ontology/finkb.ttl` | TBox：62+ 类、对象属性（domain/range/transitive/inverse/property chain）、定义类、disjoint、注释、IRI | `ontology/export.py` 单向导出 |
| `data/ontology/abox.ttl` | ABox：14 公司、15 期间、1,830 facts（reified）、621 关系边、255 分部、附注锚点 | 从 DuckDB 导出 |
| `data/ontology/inferred.ttl` + DuckDB 推断表 | OWL RL 闭包（按规则类型差分记录）+ Datalog 规则结果（逐条带 `rule_id` / `premises` / `evidence_fact_ids`） | `ontology/infer.py` 批量运行 |

> **单一真相源仍是 YAML**；OWL 是「导出 + 校验 + 互操作」层，不是第二套手工维护的真相。这与 ADR-052 的初衷一致，只把「只校验」扩成「可推理」。

### 4.2 推理规则（两类，分开实现）

**A. OWL 2 RL 结构推理**（`owlrl`，已验证）：
- 子类传递：`metric:revenue rdfs:subClassOf fibo:Income rdfs:subClassOf cmns:MonetaryAmount`；
- 子属性/逆属性/传递属性/属性链（OWL RL 支持）；
- domain/range 类型推断；
- 产出：`inferred.ttl` + 推断三元组差异报告。

**B. 数值 Datalog 规则**（Python，OWL RL 做不了算术）：
- 定义类示例：
  - `risk_cashflow_divergence`：`net_profit > 0 and ocf < 0 and ocf/net_profit < -0.2`
  - `risk_high_guarantee_exposure`：`guarantee_total / total_equity > 0.5`
  - `risk_high_leverage`：`total_liabilities / total_assets > 0.7`
- 每条命中写 `inferred_concepts(company, period, class_id, rule_id, evidence_fact_ids, severity)`；
- 规则表达式复用并扩展 `ontology/expr.py`（现只支持算术+比较，**需加 `and/or/not`**）；
- 属性链/传递闭包：在 DuckDB 边表上迭代到不动点，写 `inferred_edges(src, rel, dst, rule_id, premises_json)`。

### 4.3 「真正的本体论」验收标准

1. `finkb.ttl` 可被 rdflib 解析、`owl:Class` ≥62、对象属性 ≥10、定义类 ≥8、property chain ≥1、disjoint ≥1；
2. `abox.ttl` 中每个 fact 有类型、值、期间、公司、来源页；
3. `inferred.ttl`/推断表可回答至少 3 类问题：风险标签、间接担保/控制链、概念祖先/派生关系；
4. Datalog 推断逐条带 `rule_id` + 前提/证据；OWL RL 闭包按规则类型汇总（差分记录），不假装逐条前提；`ontology audit` 升级为「七跳」后断点 0；
5. 全量推理 **<10 秒**（实测 6.3 秒，留余量）；
6. `python -m finkb.ontology reason|audit|query` 可离线复跑；
7. 与现有 62 概念 / 71 关系 / 10 约束 / CAS 对齐测试**全部保持通过**；
8. 提供 ≥3 条 SPARQL 示例（风险标签 / 间接担保 / 概念祖先），结果可复现；`finkb.ttl` 可被 Protégé 等标准工具打开（仅用于人工审阅，不引入运行时）；
9. 推断结果可被 L3 路由直接查询（至少 3 类问题），不是只躺在 TTL 文件里。

### 4.4 与现有 ADR 的关系

- **ADR-052 需要修订**（不是悄悄推翻）：原决策「只校验不推导」的失效条件已出现——目标是「本体知识库 + 推理」，且纯 Python OWL RL 已实测可行。建议新增 **ADR-057「YAML 单一真相源 + OWL 2 RL 单向导出 + 带血缘的 Datalog 推理」**，写清代价（6.3 秒批量、推断表需重建）与失效条件（概念 >2,000 或需要完整 OWL DL 时再评估图库/推理机）。
- ADR-003（不做 GraphRAG）保留：仍不做 LLM 抽三元组的 GraphRAG；图只来自结构化表 + 本体公理。
- ADR-002/006 需微调：事实通道仍 FTS/SQL；文本通道新增稠密向量，但**仍用 numpy，不引 Qdrant**。

---

## 5. 实体主数据与关系治理

### 5.1 目标

- 把 621 条边（5 家公司）扩到 **≥10 家公司、≥1,500 条去重边**；
- 行标签噪音 **0**（当前 15 条）；
- 每个实体有稳定 URI，跨文档同名合并；
- 边带 `source_doc/page_num/period/confidence`，推断边带 `is_inferred/rule_id`。

### 5.2 三道防线

1. **表头族**：`config/relation_headers.yaml` 维护「关系类型 → 表头模式（含同义词/列位置）」；支持 15+ 表头族，不再写死 4 个字符串。
2. **实体门禁**（`entity_gate.py`）：拦截含 `合计|小计|总额|占净资产|比例|其中|余额|上限|期末|期初|本期|上期|其他|不适用` 的行标签；名称长度/字符集/纯数字/纯符号规则；金标 100 条人工抽检，precision ≥95%。
3. **实体消歧**（`entity_registry.py`）：`entities_master(entity_uri, canonical_name, entity_type, aliases, credit_code, first_seen)`；归一化（去空白/括号/全半角/后缀）；精确 + 归一化 + 编辑距离阈值合并；URI 用 `finkb:entity/{slug}`，冲突加序号。

### 5.3 覆盖目标与风险

- 关系表分布：21 份文档 / 409 张表；银行、保险、券商的表结构差异大，先做「子公司/股东/担保/关联方」四类，客户/供应商名单语料本身没有（ADR-046 已记录）。
- 风险：关联方表常是「交易金额」而非「关系边」；本方案只抽「主体↔主体」关系，交易金额表单独进 `related_party_transactions`（可选）。
- 验收：金标 100 条边 precision ≥95%；14 家公司中 ≥10 家有边；重复实体合并率 ≥90%（人工抽检 50 组）。

---

## 6. 附注锚点（note_anchors）

### 6.1 表结构

```sql
CREATE TABLE note_anchors (
  metric_id   VARCHAR,
  doc_id      VARCHAR,
  page_num    INTEGER,
  section     VARCHAR,        -- page_anchored META 的 section（条目级标题）
  method      VARCHAR,        -- 'note' | 'statement' | 'manual'
  confidence  DOUBLE,
  PRIMARY KEY (metric_id, doc_id, page_num, method)
);
```

### 6.2 生成规则

1. **报表页锚**：`facts.source_doc/page_num` 直接作为 `method='statement'`（36 项全覆盖）；
2. **附注页锚**：用 metric 别名匹配 page_anchored 的 META section（如「存货」「应收账款」「短期借款」），限定在「财务报表附注/项目注释」章节内；`method='note'`；
3. **人工覆盖**：`config/note_anchors_overrides.yaml` 处理别名歧义（如「应收账款」与「应收票据」）；
4. 质量：抽 30 条人工核对（metric ↔ 页 ↔ 该页确实讲该科目），准确率 ≥90%。

### 6.3 用途

- 问答「存货为什么涨」时，`note_drill_down` 算子从 facts 异动 → note_anchors → 附注原文块；
- L2 页面脚注可增加「附注 Pxx」锚点（可选）；
- 评测：10 道「附注定位」题。

---

## 7. 文本通道与混合检索（RAG）

### 7.1 切块

- 输入：`data/parsed/<doc>/page_anchored.md`（块级 META 已带 doc/page/section）+ `report.json` 的物理表页区间；
- 文本块：页内相邻文本合并到 ~300 字，不跨页；表格块：保留 Markdown 表，按 ~1,200 字切分并**重复表头**；
- 规模：约 **16,177 块**（9,726 文本 + 6,451 表格）；
- Payload：`chunk_id, doc_id, company, period, page_num, section, block_type(text|table), linked_concept_ids`；
- 落盘：`data/_search/chunks.jsonl`（元数据 + 正文）、`chunks.npy`（向量）、`chunk_manifest.json`（模型/维度/哈希）。

### 7.2 双路检索

| 路 | 实现 | 作用 |
|---|---|---|
| 词面 | SQLite FTS5（沿用 trigram；可选 jieba 预处理） | 精确科目/代码/短语 |
| 稠密 | `SiliconFlowEmbedder`（bge-m3 1024 维）→ numpy 余弦 Top-K | 同义改写/长尾定性 |
| 融合 | RRF（k=60），按 company/period/doc/type 过滤 | 互补 |

- 检索返回：Top-K chunk + 父页/章节 + 可回查锚点；
- 延迟目标：FTS <10ms、稠密 <20ms（16K×1024 点积）、RRF <5ms；
- 失败隔离：embedding API 不可用时自动降级为 FTS-only，并在结果中标注。

### 7.3 评测

- 20 道文字题（管理层讨论/风险/研发/分红/诉讼/会计政策），人工标 gold 页；
- 指标：Recall@5（页命中）≥80%、引用有效率 100%、拒答正确率 100%（5 道无答案题）；
- 消融：FTS-only / dense-only / RRF，记录真实数字（对齐方案 v2 §10 的 A/B/C 思路）。

---

## 8. L3 问答层

### 8.1 三路路由（规则优先，LLM 兜底）

| 路由 | 触发 | 数据来源 | 输出 |
|---|---|---|---|
| **事实** | 命中指标别名 + 公司 + 期间 + 数词/运算 | facts / facts_dedup / 命名 SQL | 数字 + 计算式 + 证据页 |
| **本体/关系** | 命中概念/风险标签/子公司/股东/担保/关联方 | 本体推断表、relations、note_anchors | 实体/边/标签 + 证据 |
| **文本** | 定性词（为什么/如何/风险/规划/说明…）或无槽位 | chunk 检索 + L2 Wiki | 原文片段 + `[Ref]` + 可选 LLM 摘述 |

- 槽位抽取复用已验证原型：15/15 题（公司/期间/指标/运算）；新增派生比率走 `wiki_queries.yaml`；
- 路由输出 `route` + `confidence` + `signals`，便于评测和用户看到「为什么走这条路」；
- 不确定时**双路都跑、并列展示**，不强行融合。

### 8.2 合成与校验

- **数字闸门**：答案中的数字只能来自 `{{cN}}` 账本（SQL/segment/query claim）或原文逐字引用；LLM 不得算数（ADR-001）；
- **引用闸门**：每个 `[Ref: doc#P]` 必须在该页 page_anchored 里找到对应文字（复用 lint R8/R9）；
- **裸数字拦截**：正则扫描未绑定数字，命中则拒绝/重生成；
- **离线模式**：无 LLM 时返回抽取式答案（片段 + 页码），保证 Demo 不依赖网络；
- **LLM 模式**：复用现有 `deepseek-v4.1-flash`（opencode.ai，需 `x-opencode-session` 头），只做叙述，不做计算。

### 8.3 最小 UI

- 优先 CLI：`python -m finkb.qa "比亚迪 2025 毛利率为什么下降"`；
- 可选 FastAPI（已装）提供 `/ask` + 单页 HTML（三栏：问题/答案+账本/证据），不引入 Streamlit。

---

## 9. 评测体系（50 题设计）

| 类型 | 数量 | 判分方式 | 说明 |
|---|---|---|---|
| 数值单点/跨期/跨司 | 15（现有） | 确定性 SQL 复算 | 保持 15/15 |
| 文字检索 | 20 | Recall@5 + 引用有效 | gold 页 + 关键片段 |
| 本体/关系推理 | 10 | 推断结果/关系边 gold | 风险标签、间接担保、概念祖先、DuPont 分解 |
| 负例 | 5 | 必须拒答/标不确定 | 不存在的数据、越界期间、错误公司 |
| 路由 | 30（可复用上述） | 路由正确率 ≥90% | 事实/本体/文本三分类 |

- 评测脚本：`eval/check_qa.py`（离线结构校验 + 可选真实 embedding 检索复算）；
- 所有数字以实测为准，写进 DEVLOG；不用 LLM judge 判数值。

---

## 10. 实施路线（S0–S4）

> 假设：1 人，每天 6 小时有效开发；按「先本体、后 RAG」排序（RAG 的路由与锚点依赖本体/附注）。

### S0 地基与决策（1 天）

- 提交当前工作树 3 个 eval 文件；清理 git 状态；
- 新增 ADR-057/058/059 草稿；更新 AGENTS/DEVLOG 的「新依赖/离线约束」；
- `pyproject.toml` 增加 optional extras：`ontology = [rdflib, owlrl]`、`embed = [httpx]`（已有）、`dev`；
- 验收：pytest 406 绿；`check.sh` 绿；文档记录新依赖与未验证项。

### S1 本体内核（6–7 天）

| 任务 | 产出 |
|---|---|
| 扩展 `ontology.yaml` schema：object_properties / defined_classes / property_chains / disjoint | YAML 通过 `test_ontology` 校验 |
| 扩展 `expr.py`：`and/or/not`、括号、比较链 | 表达式单测 |
| `ontology/export.py`：YAML→TBox/ABox Turtle | `finkb.ttl` / `abox.ttl`，rdflib 解析通过 |
| `ontology/infer.py`：OWL RL 闭包 + Datalog 规则 + 属性链闭包 | `inferred.ttl` / DuckDB 推断表 / 报告 |
| CLI：`ontology reason/export/query`；audit 升级七跳 | 命令可跑 |
| 测试：schema/表达式/导出/推理金标/血缘/断点 | +30 项，全绿 |

**验收**：TBox 类 ≥62、对象属性 ≥10、定义类 ≥8；真实数据推断 <10 秒；推断表带 rule_id/evidence；audit 七跳 0 断点；facts 表零改动。

### S2 实体治理 + 附注锚点（5–6 天）

| 任务 | 产出 |
|---|---|
| `entity_gate.py` + `entity_registry.py` + `entities_master/entity_aliases` | 噪音 0（金标 100 条） |
| `relation_headers.yaml` + `relations.py` 扩展表头族/时态字段 | ≥10 家公司、≥1,500 边 |
| `note_anchors` 生成器 + `note_drill_down` 算子 | 36/36 报表页锚；核心 13 项 ≥80% 附注锚 |
| 测试：门禁/消歧/表头变体/锚点覆盖 | +25 项 |

**验收**：金标 precision ≥95%；关系边可回查；附注抽检 30 条 ≥90%。

### S3 文本通道（6–7 天）

| 任务 | 产出 |
|---|---|
| `search/chunker.py` | ~16K chunks + manifest |
| `search/embed.py`：`Embedder` 协议 + SiliconFlow/Fake | 真实索引 + 测试用 Fake |
| `search/hybrid.py`：FTS5 + numpy + RRF + 过滤 | 检索 API/CLI |
| 20 道文字题 + Recall@5 评测 | 评测报告 |

**验收**：块锚 100% 可回查；FTS <10ms、稠密 <20ms；Recall@5 ≥80%（真实 embedding）；无 key 时自动降级 FTS-only；pytest 不触网。

### S4 L3 问答 + 评测 + 文档（7–9 天）

| 任务 | 产出 |
|---|---|
| `qa/router.py` / `qa/answer.py` / `qa/verifier.py` / `qa/__main__.py` | 三路问答 + 校验 |
| `eval/gold_text.json`（20）/`gold_reasoning.json`（10）/`gold_negative.json`（5）/`check_qa.py` | 50 题评测 |
| 可选 FastAPI `/ask` + 单页 UI | Demo |
| ADR/DEVLOG/README/任务日志同步 | 文档一致 |

**验收**：数值 15/15；路由 ≥90%；引用有效 100%；负例 5/5；pytest ≥480；`check.sh` 全绿；端到端延迟（fact <2s / text 抽取 <3s / LLM <8s）。

### 10.1 总工期与 MVP 砍法

- **完整版**：S0 1 + S1 7 + S2 6 + S3 7 + S4 8 ≈ **29 人日（约 5–6 周）**。
- **4 周 MVP**：S0 + S1（核心）+ S2（门禁/消歧/锚点）+ S3（切块/FTS，不做 dense）+ S4（CLI，不做 UI/50 题，先 30 题）≈ 20 人日。
- **不建议**：为赶 4 周把 50 题、工作台、全量实体消歧同时塞进去（会重演 v3.0 的工期低估）。

---

## 11. 验收总表

| 类别 | 指标 | 目标 |
|---|---|---|
| 回归 | pytest / eval / check.sh | 406 → ≥480 绿；15/15；全绿 |
| 本体 | TBox 类/属性/定义类 | ≥62 / ≥10 / ≥8 |
| 本体 | 全量推理耗时 | <10 秒（实测 6.3 秒） |
| 本体 | 推断血缘 | 每条有 rule_id + evidence |
| 本体 | audit 七跳断点 | 0 |
| 实体 | 关系边覆盖 | ≥10 家公司、≥1,500 边 |
| 实体 | 金标 precision | ≥95%（100 条） |
| 附注 | 报表页锚 | 36/36 |
| 附注 | 核心 13 项附注锚 | ≥80% 文档 |
| 检索 | chunk 数 | 16K ±10%，锚点 100% |
| 检索 | Recall@5 | ≥80%（20 题，真实 embedding） |
| 问答 | 路由准确率 | ≥90%（30 题） |
| 问答 | 引用有效 | 100% |
| 问答 | 负例拒答 | 5/5 |
| 性能 | 延迟 | fact <2s；text 抽取 <3s；LLM <8s |

---

## 12. 风险与回退

| 风险 | 概率 | 影响 | 回退 |
|---|---|---|---|
| SiliconFlow key 未提供 / API 限流 | 中 | 稠密检索不可用 | 自动降级 FTS-only；QA 仍可用；本地 torch 作为可选 extras |
| 本地 torch 安装/速度不可控 | 中 | 离线 dense 不可用 | 不作为主路径；只保留接口 |
| OWL RL 全量推理 >10 秒 | 低（实测 6.3s） | 重建慢 | 只对 TBox+关系图跑闭包，facts 类型推断用 DuckDB 批量 |
| 实体消歧误合并 | 中 | 图查询错 | 高置信度才自动合并，其余进 `pending_review`；金标抽检 |
| 关系表变体太多 | 中 | 覆盖率低 | 先做四类高价值表；交易金额表另存 |
| 附注条目别名歧义 | 中 | 锚错页 | overrides + 抽检 30 条；不确定的标 `low_confidence` |
| 评测题人工成本 | 中 | 延期 | 先 30 题（15 数值 + 10 文字 + 5 推理），后续扩 50 |
| 新依赖破坏离线测试 | 低 | CI 红 | 依赖全部 optional；测试用 FakeEmbedder；不联网 |
| 文档再次漂移 | 高 | 接手者误判 | S0/S4 强制回写 DEVLOG/README/任务日志 |

---

## 13. 与 v3.0 的差异总表

| 主题 | v3.0 | v3.1（本方案） | 原因 |
|---|---|---|---|
| 图存储 | Kùzu（默认）+ Neo4j（预留） | DuckDB 边表 + NetworkX；GraphStoreProtocol 预留 | Kùzu 不可安装；规模小 |
| 向量库 | Qdrant Local/Server | numpy `.npy`；VectorBackend 预留 | 16K×1024 仅 63MB；ADR-006 |
| 本体推理 | Owlready2 + Pellet + SWRL | rdflib + owlrl（OWL 2 RL）+ Python Datalog | 无 Java；已实测 |
| 数值定义类 | SWRL/OWL | Datalog 规则 + evidence | OWL RL 不能算算术 |
| 解析 | Docling 重建 | 保留 MinerU 管线 | 已有 3,141 行 + 29 份产物 + 80 坑 |
| 空块 | 436 页补 OCR | 不补；用表页区间 | 426/436 已被覆盖 |
| 附注锚点 | 36 科目 ≥90% | 36 报表页 + 核心 13 ≥80% 附注 | 13 个合计类无附注 |
| 关系覆盖 | 14 家 + 实体注册 | 同方向；先四类表 + 金标 | 409 表/15+ 表头族 |
| 编排 | LlamaIndex Agent | 自写规则 Router + 工具函数 | 可测试、无框架依赖 |
| UI | Streamlit/FastAPI 三栏 | CLI 优先 + 可选 FastAPI | FastAPI 已装，少依赖 |
| 工期 | 4 周 | 5–6 周（MVP 4 周） | 任务量更真实 |
| 测试 | 500+ | ≥480 | 406 + 新增 80 |

---

## 14. 需要你拍板的 5 件事

1. **Embedding 路线**：用你之前选的 SiliconFlow `BAAI/bge-m3`（需要你提供 `SILICONFLOW_API_KEY`），还是让我先做 FTS-only 的 MVP、dense 后置？（推荐前者，否则「语义检索」名不副实）
2. **OWL 范围**：接受「OWL 2 RL + Datalog 数值规则」（已实测），还是坚持 v3.0 的 Owlready2/Pellet 全量 DL？（后者需要装 JDK，且与离线测试冲突）
3. **工期目标**：4 周 MVP（30 题、CLI、无 UI）还是 5–6 周完整版（50 题、UI、全量实体）？
4. **评测口径**：50 题是否作为硬验收？如果时间紧，先 30 题、把「负例 + 引用有效」作为硬指标。
5. **是否先提交当前 eval 改动**（3 个文件）作为 S0 第一步？（推荐是，避免后续分支混杂）

---

## 附录 A：实测证据（2026-11-03）

```text
# 1) 测试与评测
cd finkb && ../.venv/bin/python -m pytest -q        # 406 passed（收集数；exit 0）
cd .. && .venv/bin/python eval/check.py             # 15/15 PASS

# 2) OWL RL 原型（真实数据）
/tmp/finkb_feas/bin/python /tmp/owl_proto.py
# build 0.202s  reason 6.324s  triples 14208 -> 27115  inferred 12907
# sample fact types: revenue, Income, MonetaryAmount, owl#Thing

# 3) 依赖
pip index versions rdflib        # 7.6.0
pip index versions owlrl         # 7.6.2
pip index versions pyoxigraph    # 0.5.13
pip index versions qdrant-client # 1.20.0
pip index versions jieba         # 0.42.1
pip index versions kuzu          # No matching distribution found

# 4) 网络
curl -s -o /dev/null -w '%{http_code}' https://huggingface.co        # 000（被墙）
curl -s -o /dev/null -w '%{http_code}' https://hf-mirror.com         # 200
curl -s -o /dev/null -w '%{http_code}' https://www.modelscope.cn     # 302
curl -s -o /dev/null -w '%{http_code}' https://api.siliconflow.cn    # 404（可达）
# opencode.ai/zen/go/v1/embeddings -> 404（无 embedding 接口）

# 5) 模型文件
curl -s -L -o /dev/null -w '%{http_code}' \
  https://hf-mirror.com/BAAI/bge-small-zh-v1.5/resolve/main/model.safetensors  # 200
# BAAI/Qdrant 仓库的 onnx/model.onnx -> 404

# 6) 缺页覆盖
# 436 missing；426 covered by physical table page spans；10 uncovered
```

## 附录 B：关键文件改动清单（预估）

```text
新增：
  src/finkb/ontology/export.py        YAML -> Turtle
  src/finkb/ontology/infer.py         OWL RL + Datalog + 属性链闭包
  src/finkb/ontology/operators.py     DuPont/分部毛利/附注穿透算子
  src/finkb/fact_engine/entity_gate.py
  src/finkb/fact_engine/entity_registry.py
  src/finkb/search/chunker.py
  src/finkb/search/embed.py
  src/finkb/search/hybrid.py
  src/finkb/qa/{__init__,router,answer,verifier,__main__}.py
  config/relation_headers.yaml
  eval/gold_text.json / gold_reasoning.json / gold_negative.json / check_qa.py
  tests/test_ontology_infer.py / test_entity_registry.py / test_note_anchors.py
  tests/test_chunker.py / test_hybrid_search.py / test_qa.py
  docs: ADR-057/058/059（DEVLOG §5）

修改：
  config/ontology.yaml（object_properties/defined_classes/property_chains）
  src/finkb/ontology/{model,schema,expr,audit,cli}.py
  src/finkb/fact_engine/relations.py（表头族/时态）
  src/finkb/search/index.py（保持兼容，新增 chunk 索引入口）
  pyproject.toml（optional extras）
  finkb/scripts/check.sh（新增 ontology infer/audit、chunk 锚点检查）
  finkb/DEVLOG.md / README.md / 任务日志.md（状态与依赖）
  eval/gold_qa.json（如需扩充数值题）

不动：
  src/finkb/parser/*（29 份产物不重跑）
  src/finkb/fact_engine/{selector,extractor,verifier,ingest,store}.py（facts 链路不动）
  src/finkb/wiki/* / lint/*（claims/页锚/门禁复用）
```

---

> 本方案的所有「实测」结论均可由附录 A 命令复现；未实测项已标注（本地 torch 速度、SiliconFlow 真实 embedding、实体消歧精度、附注抽检准确率）。方案落地前请先确认 §14 的 5 个决策。
