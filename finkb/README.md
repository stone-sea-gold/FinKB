# FinKB — L0/L1/L2 + L0.5 本体 + 块级混合检索 + 三路问答

A 股财报知识库，架构见 [`../FinKB-方案v2.md`](../FinKB-方案v2.md)（v2.2）。

> 📓 **接手项目请先读 [`DEVLOG.md`](./DEVLOG.md)** —— 那里记录当前状态、设计决策、
> 踩过的坑与未验证项。本 README 只讲「怎么用」，DEVLOG 讲「为什么这样」。
> 工作约定见 [`AGENTS.md`](./AGENTS.md)。

当前实现范围：**L0 凭证层**（解析 → 页码锚定 → 清洗 → 跨页缝合 → 逻辑表 → 审计）；
**L1 事实层**（29 份唯一库 **1,830 条**、0 未匹配，金标 72/72）；**L2 知识层**（**153 页**
Markdown 知识页 + claims 账本 + FTS5）；**L0.5 本体与推理**（62 概念 / 71 关系 / 10 约束 +
OWL 2 RL 物化，ADR-057）；**S2 实体治理与附注锚点**（4,437 条关系边 / 1,474 实体 / 2,689 附注锚，
ADR-059）；**S3 块级混合检索**（15,764 块，jieba FTS5 + bge-m3 向量 + RRF，ADR-058）；
**S4 三路问答**（fact / ontology / text，50 题评测 50/50）。

## 数据格式分层（重要）

清洗与缝合**不直接操作 MinerU 原生格式**，而是操作本项目自有的中间表示（IR）。
三层职责必须区分清楚：

```
MinerU content_list.json          外部格式，字段随版本变化
        │  content_list_to_pages()      ← 唯一的格式适配层
        ▼
Page / Block / TableBlock         本项目 IR（层内唯一真相）
        │  strip_running_noise()        ← 清洗只看 IR 的块顺序与行
        ▼
StitchedTable                     缝合产物，交给 L1
```

| 环节 | 输入格式 | 说明 |
|---|---|---|
| `content_list_to_pages` | MinerU `content_list.json` | **唯一**依赖 MinerU 字段的函数 |
| `html_table_to_rows` | MinerU `table_body`（HTML） | 用 `<th>` 存在性判断有无表头 |
| `strip_running_noise` | 本项目 IR | 与数据来源无关；pypdf 与 MinerU 路径共用 |
| `stitch_tables` | 本项目 IR | 同上 |

这样做的代价与收益：换解析器只需重写适配层；但**清洗逻辑的充分性依赖 IR 的块粒度**。
同一个页眉，在 pypdf 输出里是「块内的行」，在 MinerU 输出里是「独立的块」，
两者的清洗路径不同，必须分别验证（见 `tests/test_mineru_noise_path.py`）。

> mineru pipeline 后端本身会把页眉页脚丢进 `layout.json` 的 `discarded_blocks`，
> 走 MinerU 主路径时页眉大概率不进 `content_list`。本模块的清洗因此是**第二道保险**，
> 在 pypdf 兜底路径上是**唯一防线**。

## 目录

```
finkb/
├── src/finkb/
│   ├── common/models.py            # Page / Block / TableBlock / StitchedTable
│   ├── fact_engine/                 # L1：选表 / 抽取 / 逐字反查 / DuckDB 仓储
│   │   ├── metric_dict.py          # YAML → Metric；单位族由 unit_default 推导
│   │   ├── segment.py              # 分产品/分行业/分地区结构表 → segment_facts
│   │   ├── relations.py            # 子公司/关联方/股东/担保 → relations
│   │   ├── conflicts.py            # 同科目跨文档数值冲突 → conflicts
│   │   └── store.py                # metric_dict / facts / segment_facts / relations / conflicts
│   ├── wiki/                        # L2：frontmatter / 原子写 / claim 账本 / 编译器 / 实体 / 概念页
│   ├── lint/                        # L2：R1–R10 规则 + 单页转正前检查
│   ├── search/                      # L2：页面 FTS5 索引（trigram + 别名扩展 + bm25）
│   └── parser/
│       ├── markdown_blocks.py      # 页码锚定 Markdown 解析；HTML 表格 → 行/网格
│       ├── noise_filter.py         # 运行页眉/页脚与残留页码清洗
│       ├── table_stitcher.py       # ★ 跨页表格缝合
│       ├── section_splitter.py     # 物理表 → 逻辑表切分 + 表头继承
│       ├── pipeline.py             # 解析 → 清洗 → 缝合 → 切分 端到端入口
│       └── mineru_client.py        # MinerU 客户端 + content_list 适配 + pypdf 兜底
├── wiki_vault/                      # L2 知识页（index / summaries / explorations / entities / concepts / _draft）
├── eval/                           # 评测集：数字 15 题（check.py）+ 50 题问答（check_qa.py）
├── tests/                          # 560 项测试，全部离线可跑
├── scripts/check.sh                # 一条命令验收（测试 + 语料基线 + 金标）
├── config/corpus-baseline.json     # 29 文档 × 5 个数字的退步基线
├── config/metric_dict.yaml        # L1 指标字典 v3（36 个指标 / 80 个别名）
├── config/wiki_scope.yaml         # L2 编译范围（29 份全登记）
├── config/wiki_queries.yaml       # L2 命名 SQL（同比 / 资产负债率 / 三项利润率）
└── data/parsed/                    # 解析产物（不进 git）
```

## 一条命令验收

```bash
scripts/check.sh            # 单元测试 + 29 份产物的守恒律
```

想单独看某一项：

```bash
python tools/audit.py corpus                       # 29 份的体检表（有表头率/裸表/守恒律/数字异常）
python tools/audit.py invariants --doc data/parsed/002594_2025_年报   # 单份 16 条守恒律
python tools/audit.py verify-cells --doc ... --pdf ...               # 数字是否都能在 PDF 原文找到
python tools/audit.py recall --doc ... --pdf ...                     # 原文的数字是否都进了表
python tools/audit.py rowspan --doc ...                              # rowspan 展开前后对照
python tools/audit.py headers --doc ...                              # 多层表头逐张对照
python tools/audit.py diff --before a.json --after b.json            # 两次快照的字段级差异
```

L2 知识页：

```bash
python -m finkb.wiki init                    # 建目录骨架（幂等）
python -m finkb.wiki compile --doc 002594_2025_年报   # 编译单份：1 摘要 + 5 探针（无数据页跳过）
python -m finkb.lint                         # 十条规则；报告写 wiki_vault/reports/，有 error 时 exit 1
python -m finkb.wiki index                   # 重算 index.md（编译后自动做）
python -m finkb.search "比亚迪 毛利率"        # 旧 153 页知识索引（trigram + 别名扩展）
python -m finkb.search --rebuild             # 全量重建旧索引
cd .. && .venv/bin/python eval/check.py      # 15 题离线复算（不联网、不调 LLM）

块级混合检索与三路问答（S3/S4）：

```bash
python -m finkb.search.chunker --verify      # 切块 + 回查校验（15,764 块，0 失配）
python -m finkb.search.hybrid build          # 建 FTS5 + 向量索引（需 SILICONFLOW_API_KEY，约 11 分钟）
python -m finkb.search.hybrid query "海外 布局" --company 002594 --period 2025
python -m finkb.qa "万科A 2025 年营业收入是多少亿元？"
cd .. && .venv/bin/python eval/check_qa.py   # 50 题：数字/文字/推理/负例
```

三档问答（ADR-060；`auto` 逐级回落，`--explain` 看剪枝/残差门）：

```bash
python -m finkb.qa "万科A 2025 年营业收入是多少亿元？" --explain        # Tier 1 指标
python -m finkb.qa "哪些公司 2025 年资产负债率超过 70%？" --tier ontology  # Tier 2 本体
python -m finkb.qa "比亚迪 2025 年报里如何描述海外布局？" --tier document # Tier 3 文档
```


问答理解层（ADR-062；先理解、再分类）：

- 规则优先：期间写法全表（一季度 / 第1季度 / Q1 / 上半年 / 前三季度 / 三季报 / 全年）、
  裸期间继承上下文年份、公司名精确匹配 + difflib 近似（“塞力斯”→“赛力斯”）。
- 覆盖守卫：问题里的显式时间词解析不了时**直接拒答并提示换说法**，不再默默按年报回答。
- 事实通道回原文：金额类同时给出归一值与原值（如“8039.65 亿元（原文 803,964,958,000.00 元）”），
  多实体 / 多期间逐项列出。
- 可选 LLM 读题（默认关闭，保证离线）：

```bash
FINKB_LLM_UNDERSTAND=1 finkb ask "赛力斯 2025 年第一季度营业收入是多少亿元？"
```

Agent 接入与审计（ADR-061 / ADR-063；MCP stdio，13 个工具）：

```bash
finkb-mcp                                        # 默认知识库（源码树或 ~/.finkb）
finkb-mcp --home ~/finkb-data                    # 指定用户本地库（也可用 FINKB_HOME）
# 问答/检索/图谱 5 个工具：ask / query_metric / search_documents / describe_ontology / traverse_relations
# 合成 2 个工具：evidence（取证据包）/ verify（校验宿主答案）
# 本地库 6 个工具：init_library / library_status / list_documents / add_document / remove_document / rebuild_index
python -m finkb.qa "比亚迪 2025 年营业收入是多少亿元？" --audit-log
# 审计日志默认 data/qa_audit.jsonl（已 gitignore），可用 FINKB_QA_AUDIT 改路径
```


多轮追问（P5；会话归宿主，语义归项目）：

- `ask` 可选 `context`，只传结构化槽位，不传整段历史：
  `{"previous_question": "上一轮问题", "previous_slots": <上一轮 resolved_slots>}`。
- 项目返回 `resolved_question`（补全后的完整问题）、`resolved_slots`、`inherited` /
  `overridden`（哪些槽位来自上一轮、哪些被本轮显式覆盖）、`missing` / `clarify`
  （补不全时给缺什么和候选，不静默猜）。
- 继承规则：公司/期间/指标/限定词缺则继承；`source_doc` 不继承；当前显式值优先。
- 不带 `context` 时行为与原来完全一致（只增字段，不改旧字段）。

```bash
# 第一轮
finkb ask "比亚迪 2025 年营业收入是多少？" --json
# 第二轮：把上一轮的 resolved_slots 放进 --context（宿主负责保存历史）
finkb ask "那 2024 年呢？" --context '{"previous_question":"比亚迪 2025 年营业收入是多少？","previous_slots":{"companies":["002594"],"periods":["2025"],"metric":"revenue"}}'
```

多轮验收（离线，12 组宿主模拟）：`python ../eval/check_context.py`。

响应契约（P6；字段只增不改）：

- 每个回答都带 `schema_version`（当前 `"1.0.0"`）与 `disclaimer`（研究参考、不构成投资建议）。
  宿主可先看版本号，再决定是否按老字段解析。
- 模式 A（默认，项目写答案）：`synthesis.used=true` 且 `synthesis.mode="A"`；无后端/校验不过时
  回退抽取式（`synthesis.used=false`、`mode="extractive"`），不影响主字段。
- 模式 B（宿主写答案）：`evidence`/`verify` 两个工具返回 `mode="B"` 与 `responsibility`
  责任声明——**答案文本由宿主生成，FinKB 只提供证据与校验，不对宿主表述负责**。

在 DSH（或其它支持 MCP 的宿主）中接入的最小配置：

```json
{
  "mcpServers": {
    "finkb": {"command": "finkb-mcp", "args": []}
  }
}
```

宿主侧流程：① 保存每轮问题与 `resolved_slots`；② 追问时把上一轮槽位作为 `context` 传回
`ask`；③ 收到 `clarify` 就向用户补问；④ 需要宿主自己写答案时用 `evidence` 取包、写完调
`verify` 校验；⑤ 会话历史/压缩/流式/展示都留在宿主，项目不保存。

## 本地知识库（用户自建，v1）

项目自带的 29 份年报及其 facts / 向量 / wiki **不随分发提供**；用户用自己的年报建库。
所有用户数据放在 `FINKB_HOME`（默认：源码树 `finkb/`；pip 安装后 `~/.finkb`）：

```bash
export FINKB_HOME=~/finkb-data

python -m finkb.library init                     # 建目录 + 复制配置模板
cp $FINKB_HOME/config/.env.example $FINKB_HOME/config/.env
# 编辑 .env：至少填 LLM_API_KEY（建库必需）；可选 MINERU_API_TOKEN / SILICONFLOW_API_KEY

python -m finkb.library add --pdf ~/下载/600519_2025_年报.pdf \
  --company 600519 --period 2025 --type 年报 --name 贵州茅台
python -m finkb.library add --parsed ./已解析目录 \
  --company 600519 --period 2025 --type 年报 --name 贵州茅台
python -m finkb.library list
python -m finkb.library status
python -m finkb.library rebuild --embed          # 重建切块 + 索引（--embed 需 SILICONFLOW_API_KEY）
python -m finkb.library remove 600519_2025_年报
```

- `add` 默认「解析 → LLM 事实抽取 → wiki 编译 → 切块/索引」；缺 `LLM_API_KEY` 时只做解析+切块并给出 warning。
- `--parse-mode auto|offline|mineru`：`auto` 用离线兜底（不消耗 MinerU 额度）；`mineru` 需要 token。
- `--no-facts` / `--no-wiki` 可只建原文检索库；同一份文档重复导入会覆盖该文档的数据。
- 支持的期间：`2025`（年报）、`2025H1`（半年报）、`2025Q1`（一季报）、`2025Q3`（三季报）。
- 通过 MCP 使用同一套能力（`init_library` / `add_document` / `remove_document` / `rebuild_index` / `list_documents` / `library_status`）。
- **分发边界**：仓库里的 `data/`、`raw/*.pdf`、`wiki_vault/` 是开发期产物；打包/发布时不要带上，
  代码只需要 `src/` 与 `config/` 模板。用户数据始终在 `FINKB_HOME`。

质量门禁（`scripts/check.sh` 已包含）：

```bash
python tools/metric_check.py --db data/facts.duckdb          # 指标定义 + SQL 安全 + 连库
python -m finkb.ontology.quality --db data/facts.duckdb      # 本体结构质量（9 项）
python -m finkb.fact_engine.data_check --db data/facts.duckdb # 约束求值/实体回填/附注覆盖
cd .. && .venv/bin/python eval/recall.py --no-dense --min-recall 0.9  # 离线召回 + MRR
cd .. && .venv/bin/python eval/check_context.py                       # 多轮上下文 12 组
```

批量编译器在 `finkb/.tmp/batch_compile.py`（读 `config/wiki_scope.yaml`，逐份编译并写
`.tmp/batch_compile.log`；29 份约 22 分钟）。`wiki init/compile` 需要 `LLM_API_KEY`；
`lint` 与 `search` 全离线。

`verify-cells` / `recall` 需要 PDF 与 `PyMuPDF`（已装）；其余全离线。

## 运行测试

```bash
cd finkb
../.venv/bin/python -m pytest -q
```

无需网络、无需任何 API key。

## 安装

要求 Python **3.10+**（开发环境 3.12）；不需要 GPU / Java / Docker。

```bash
cd finkb
python3 -m venv ../.venv
../.venv/bin/pip install -e ".[search,ontology]"
```

- `search` 带 jieba（中文分词；缺失时退化为字二元组，召回下降）
- `ontology` 带 rdflib/owlrl（只有本体导出/推理需要，日常问答可不装）
- 从 GitHub 安装：`pip install "finkb[search,ontology] @ git+https://github.com/stone-sea-gold/FinKB.git#subdirectory=finkb"`

安装后可用 `finkb` 与 `finkb-mcp` 命令（也可用 `python -m finkb.cli`，需 `PYTHONPATH=src`）。
配置模板随包安装，`library init` 会自动复制到 `$FINKB_HOME/config/`。

## 配置密钥（config/.env）

源码开发用 `finkb/config/.env`；用户本地库用 `$FINKB_HOME/config/.env`（`library init` 会把模板复制过去）：

```bash
# 源码开发
cp finkb/config/.env.example finkb/config/.env
# 用户本地库
python -m finkb.library init
cp $FINKB_HOME/config/.env.example $FINKB_HOME/config/.env
```

`config/.env` 已被 `.gitignore` 覆盖，不会提交；`.env.example` 会提交。

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `LLM_API_KEY` | 建库必需 | — | OpenAI 兼容接口的 key（事实抽取 + wiki 编译） |
| `LLM_BASE_URL` | | `https://opencode.ai/zen/go` | OpenAI 兼容服务地址 |
| `LLM_MODEL` | | `deepseek-v4.1-flash` | 抽取/编译使用的模型 |
| `SILICONFLOW_API_KEY` | | — | 向量索引与稠密检索（bge-m3）；缺了自动纯关键词 |
| `MINERU_API_TOKEN` | | — | 仅 `--parse-mode mineru` 在线解析需要 |

MinerU Token 从 <https://mineru.net/apiManage/token> 获取；SiliconFlow key 从 <https://siliconflow.cn> 获取。

支持的 MinerU 变量：

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `MINERU_API_TOKEN` | | — | Bearer token |
| `MINERU_BASE_URL` | | `https://mineru.net` | API 根地址 |
| `MINERU_MODEL_VERSION` | | `pipeline` | `pipeline` / `vlm` / `MinerU-HTML` |
| `MINERU_LANGUAGE` | | `ch` | PaddleOCR 语言码，中文是 `ch` 不是 `zh` |
| `MINERU_POLL_INTERVAL` | | `3` | 轮询间隔（秒） |
| `MINERU_POLL_TIMEOUT` | | `900` | 轮询总超时（秒） |

> 本项目 `config/.env` 固定 `vlm`（ADR-016）：`pipeline` 后端会丢列，只作为成本备选。

临时覆盖（不写文件）：

```bash
MINERU_API_TOKEN=xxx ../.venv/bin/python -m finkb.cli parse ...
```

## 统一 CLI（问答 / 检索 / 图谱 / 评测）

`pip install -e .` 后会生成 `finkb` 命令，日常使用只需这一个入口：

```bash
finkb ask "比亚迪 2025 年营业收入是多少亿元？"          # 三档问答（Tier 1/2/3 自动回落）
finkb ask "哪些公司 2025 年资产负债率超过 70%？" --explain
finkb ask "比亚迪 2025 年报里如何描述海外布局？" --tier document
finkb ask                                              # 不给问题：进入交互模式（:q 退出）

finkb search "海外 布局" --company 002594 --period 2025 --limit 5
finkb search "研发人员" --type table --no-dense          # 只用关键词

finkb graph 比亚迪 --limit 10                           # 公司/实体关系边
finkb graph --concept self:gross_margin                 # 概念、公式与上下位关系
finkb graph --overview                                  # 本体与关系图概览

finkb status                                           # 数据资产/索引健康快照
finkb eval                                             # 50 题问答评测
finkb eval --recall                                    # 文字题召回 + MRR（离线）
finkb eval --full                                      # scripts/check.sh 全套验收
```

说明：`ask`/`search` 默认离线读本地库；`search` 检测到 `SILICONFLOW_API_KEY` 时自动加向量路，
否则退回纯关键词。`finkb-mcp` 是给外部 agent 的 MCP 入口，日常问答不需要它。

## 命令行用法（PDF 解析）

```bash
# ① 先做小范围联调（强烈建议）：只解析 P43-P44，验证真实跨页表
finkb parse "../raw/002594_2025_年报.pdf" --pages 43-44

# ② 全量解析（268 页，消耗较多额度）
#    MinerU 单文件上限 200 页；超过时加 --auto-split 自动分批并合并成一份结果
finkb parse "../raw/002594_2025_年报.pdf" --auto-split

# ③ 离线模式：不调用 MinerU，用 pypdf 验证链路是否通
finkb parse "../raw/002594_2025_年报.pdf" --offline
```

产物落在 `data/parsed/<doc_id>/`：

| 文件 | 内容 |
|---|---|
| `page_anchored.md` | 页码锚定 Markdown（L0 落盘格式） |
| `content_list.json` | MinerU 原始输出（在线模式才有） |
| `tables/T001.md` | 每张逻辑表一个文件，便于人工核对 |
| `report.json` | 统计、噪音签名、缝合审计记录 |

退出码：`0` 成功 / `2` PDF 不存在 / `3` 未配置 token / `4` MinerU 调用失败。

## 流水线顺序

```python
from finkb.parser.pipeline import build_tables

result = build_tables(markdown, doc_id="002594_2025_年报")
result.summary()        # 页数=268 逻辑表=N 跨页缝合=M 剥离噪音行=534
result.tables           # 缝合后的逻辑表
```

顺序**强制**为「先清洗、后缝合」：缝合判据依赖「页首/页尾是什么块」，
未清洗的页眉会让判据失效。

## 关键设计

### 页码约定

`DocMeta.page` 一律为 **1-based 物理页码**，与 PDF 阅读器 `#page=N` 及方案 §9.3 的
`[Ref: {doc_id}#P{N}]` 锚点一致。MinerU 的 `page_idx` 是 0-based，在
`content_list_to_pages()` 里统一 +1。

### 跨页缝合判据

四个条件同时成立才缝合：

1. 第 N 页的表格是**该页最后一个内容块**；
2. 第 N+1 页的表格是**该页第一个内容块**；
3. 两表**列数一致**；
4. 第 N+1 页表格**无表头**，或其表头与第 N 页**完全重复**（重复表头会被丢弃）。

条件 4 是防误缝的关键：正常起始的表格几乎总带表头，无表头的页首表格绝大多数是
上一页的延续。三页及以上链式拆分由循环续接处理。

**「内容块」指非噪音块** —— 缝合会跳过 `Block.is_noise=True` 的块。这让缝合
不依赖清洗是否彻底，是比「先洗干净再缝」更稳的组合。

```python
result.tables[0].parent_id      # 002594_2025_年报#P43-P44_T01
result.tables[0].to_markdown()  # 还原成完整表，供 L1 抽取
result.stitch.records           # 审计记录（页码序列 + 缝合理由）
```

### 页眉页脚清洗

不靠固定文案，靠**跨页重复**这一统计特征：把候选行的数字替换成 `#` 得到签名，
签名在超过阈值比例的页面上出现即判定为运行页眉/页脚。

三条护栏（都是踩坑后加的）：

| 护栏 | 原因 |
|---|---|
| **表格行永不参与判定** | 跨页表头每页都会重复，一旦被当成页眉清掉，缝合后就没有列名了 |
| **阈值硬下限 = 2** | 比例阈值在小文档上会退化为 1，导致只出现一次的正文行也被删 |
| **单块页去重候选行** | 整页只有一个文本块时，头尾取自同一块，会重复计数把阈值虚高 |

另处理一类残留：页眉是 `公司名 年份 报告全文  44`，签名只能删文字部分，
页码 `44` 会作为独立行留下，进而让缝合误判「页首第一个块是文本」。
因此做**页码行识别**（数字须等于物理页码才删），覆盖两种形态：

- 页码是长块里的一行 → 只在文本块头/尾 `scan_*_lines` 范围内生效；
- 页码是**独立块**（MinerU 常见）→ 整块只有一个非空行时单独立判定。

## 已验证（29 份 / 14 家公司）

| 项 | 结果 |
|---|---|
| 测试 | **547 项通过**，离线 |
| 语料 | 29 份 / 14 家公司 / 4791 页 / 10 类版式 |
| 逻辑表 | 6130 张；有表头 **5729（93.5%）** |
| 守恒律 | **359/359**（年报 16 条 / 半年报 13 条 / 季报 10 条） |
| 数值回查 | 36026 个抽样中 6 个查不到（99.98%，均为 MinerU 粘格） |
| 跨页缝合 | 28 张；列数不一致拒绝 21 条，逐条带原因 |
| 原文召回 | 18 份实测 100%：PDF 里的数字没有整表丢失 |
| L1 事实层 | **29 份唯一库 1,830 条**（0 未匹配、0 列缺口，42 组资产负债权益全平）；金标事实集 72/72 |
| L2 知识层 | **153 页 active**（29 摘要 + 107 探针 + 14 实体 + 3 概念）、lint 0 错 0 警；claims 1,578 条；`segment_facts` 255 条、`relations` 4,437 条、`conflicts` 1 组 |
| L0.5 本体 | 62 概念 / 71 关系 / 10 约束；定义类 96 命中、关系边推出 378、OWL RL 2,999 三元组；audit 七跳 0 断点 |
| 检索与问答 | 块级索引 15,764 块；20 题文字 Recall@5 1.00（带过滤）；`eval/check.py` 15/15；`eval/check_qa.py` 50/50 |

以上数字由 `python tools/audit.py corpus` 复现；退步由 `config/corpus-baseline.json` 判定。

## 按 MinerU 实测规范做的三处修正

端点与字段对照第三方实测规范（pipeline 2.6.4，
[mineru_specification-v1.0](https://raw.githubusercontent.com/fufankeji/VibeCodingCourse_v1/refs/heads/main/Week3_Claude_Code_Engineering/Lesson06_Multimodal_RAG_System/GraphRAGAgent/docs/mineru_specification-v1.0.md)）：

| 问题 | 原实现 | 修正后 |
|---|---|---|
| 标题检测 | 用 `type in {title, heading}` | `type=text` 且 **`text_level` 字段存在** → 标题；正文无该字段 |
| 本地文件上传 | 把本地路径当公网 URL 提交 | `file-urls/batch` 取预签名 URL → **裸 PUT（无任何 header）** → `extract-results/batch` 轮询 |
| 表格表头 | HTML 一律把首行当表头 | 按 `<th>` 是否存在判断，续接表保持 `header=None` |

## 已知限制

- **纯文字表头**：首行与数据行 HTML 同构，无法自动判定；裸表 299 张里约一半是键值表，
  真实缺口需人工标注。
- **宽表逐页丢空列**：如股东权益变动表某页 16 列、下一页 15 列，列数判据会拒绝缝合并告警；
  数据不丢，但分片没有列名。**不猜对齐**（坑-40）。
- **4 个数字无法回查**：都是 MinerU 把两格粘成一格，上游 OCR 问题，不是本项目解析错误。
- `OfflinePdfParser` 只做逐页纯文本，**不含表格结构，不可用于 L1 抽取**，仅用于链路验证与测试。
- P6 已定：L1 直接以 `LogicalTable`（含页码/表头/行）为输入，不再产出按页 JSON（ADR-029）。
- **盲测只做了一轮**：四行业 4 份（茅台/美的/恒瑞/长电）用冻结代码跑过，暴露了 CF 选表、
  行标签单位、费用负号三处缺陷，已修（ADR-034）；券商/能源的跨页拼表已补（ADR-035），
  但盲测样本仍只有一轮。
- **跨行业只到「能跑」**：中信证券/中国神华的三表已能完整选中（ADR-035），但行业科目
  只补了 6 个，字典外的行会缺；同一页并列合并表与母公司表时，仍可能选错。
- **L2 做了 5 个探针**：01 核心财务 / 02 分产品结构 / 03 研发投入 / 04 海外收入 /
  05 现金流与资本结构；方案里的 06/07/08 只有编号没有定义（v3 §4 只定义 01–05），按「二期」处理。
- **L2 结构覆盖 13/29 份**：分产品/分行业/分地区表只存在于 13 份文档；银行、保险、季报
  本身不披露三列表（已逐份核对，不是漏抽）。
- **两套检索并存**：旧 153 页知识索引仍用 trigram；块级通道用 jieba + bge-m3 向量 + RRF（ADR-058），
  暂未合并；稠密检索依赖 SiliconFlow API，无 key 时自动降级 FTS-only。
- **本地库 v1 的边界**：只支持 A 股年报/半年报/一季报/三季报四类；`auto` 解析走离线兜底
  （表格结构弱于 MinerU）；向量索引需要 `SILICONFLOW_API_KEY`，wiki 编译需要 `LLM_API_KEY`；
  `finkb eval` 的 50 题只针对项目自带的 29 份语料，不适用于用户库。
- **本地库数据位置**：`FINKB_HOME` 下的 `data/`、`raw/`、`wiki_vault/`、`config/` 都是用户数据，
  不进 git、不随代码分发；`finkb-mcp --home` 与 `FINKB_HOME` 等价，必须在进程启动前确定。
- **L2 编译不幂等**：重编会重写 claims、LLM 正文每次不同；token 成本未量化。
- **L2 关系与冲突只做结构化部分**：关系仅四类表（子公司/关联方/股东/担保），
  矛盾仅 facts 级数值不一致；文本语义矛盾、客户/供应商名单未做（ADR-046）。

## 下一步（Phase 2 及以后）

0. ✅ 已完成：`LLM_API_KEY` 已配置并联调（chat 模型，用于语义对齐；**不是** embedding key）；
   `metric_dict` 15 项已建（先覆盖 3 家整车，再按行业扩到银行/保险）
1. ✅ `fact_engine/{metric_dict,store}.py`：`metric_dict` / `facts` / `facts_dedup` /
   `relations` / `unmatched_pool` 均已落库
2. ✅ 两段式抽取（补丁 P1）：`selector.py` 选表 → LLM 只摘录 Observation →
   `verifier.py` 回单元格逐字核对 + 单位归一 → `ingest.py` 写库
3. ✅ 三家 18 份（含季报）跑通：617 条事实，0 条未匹配（1 条由 `repair` 修正），
   2025 毛利率一条 SQL 出结果；单份抽取+入库 12–60 秒
4. ✅ 跨行业验证：招商银行、中国平安 2 份 → 73 条事实、0 未匹配（ADR-033）
5. ✅ 盲测第一轮：白酒/家电/医药/电力 4 份，冻结代码跑完，暴露 3 处缺陷并修复（ADR-034）
6. ✅ 券商/能源跨页拼表：中信证券、中国神华三表完整选中，67 条事实、0 未匹配；28 份既有选表不变（ADR-035/036）
7. ✅ L2 单文档编译：`python -m finkb.wiki compile --doc 002594_2025_年报` 产出 4 页并全部转正；
   `python -m finkb.lint` 0 错 0 警
8. ✅ L2 完整跑通：29 份全部编译（153 页 active）、5 个探针、实体页 14 张、跨公司概念页 3 张、
   FTS5 检索、15 题评测 15/15、lint 十条规则 0 错 0 警；`scripts/check.sh` 与 `eval/check.py` 全绿
9. ✅ S3/S4：块级混合检索 + 三路问答（50 题 50/50）；ADR-058/059
10. ⏳ 后续：06/07/08 探针语义定义、跨期对比页、Web 三栏工作台、LLM 合成层（当前问答为抽取式）
