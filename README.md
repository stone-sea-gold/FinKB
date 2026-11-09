# FinKB

本体驱动的 A 股财报知识库：把年报 PDF 解析成**可查询的结构化事实**、**知识页**与**原文块**，提供「指标 / 本体 / 原文」三档问答、混合检索、关系图与 MCP 接口。

> 版本 **0.1.0** · 代码与数据分离：仓库只含代码、测试与配置模板，**不附带任何年报 PDF、事实库或向量索引**。知识库由使用者用自己的年报在本地建立。

## 能力

- **解析**：MinerU 在线解析 / 离线（PyMuPDF）兜底；跨页表格缝合、版式归一。
- **事实层**：36 个财务指标、单位归一、守恒律校验、每个数字可回查原文页。
- **本体层**：62 概念 / 71 关系 / 10 约束 / 8 个数值定义类；OWL 2 RL 批量物化，推断带血缘（规则 + 证据事实）。
- **检索**：FTS5 + jieba 加权 + bge-m3 向量 + RRF 融合；块级引用可回查（doc + page）。
- **问答**：三档 fall-through（指标 SQL → 本体推理 → 原文检索）；数字只由 SQL/代码产生，不交给 LLM 编；证据不足明确拒答。
- **合成（可选，默认关）**：受约束生成答案——数字用账本占位符、句子带页码引用，V1–V7 机器校验不过就回退抽取式（`FINKB_LLM_SYNTH=1` 开启）。
- **接口**：统一 CLI（`finkb ask/search/compare/graph/eval/status`）+ MCP stdio（14 个工具：问答 5 + 本地库管理 6 + 合成 2 + 对比 1）。

## 环境要求

- Python **3.10+**（推荐 3.12）
- 不需要 GPU / Java / Docker，CPU 即可
- 建库时需要联网（调用 LLM；可选 MinerU、SiliconFlow）

## 安装

从 GitHub 安装（推荐）：

```bash
git clone https://github.com/stone-sea-gold/FinKB.git
cd FinKB/finkb
python3 -m venv ../.venv
../.venv/bin/pip install -e ".[search,ontology]"
```

或者直接 pip 安装（可选依赖按需）：

```bash
pip install "finkb[search,ontology] @ git+https://github.com/stone-sea-gold/FinKB.git#subdirectory=finkb"
```

> `search` 额外装 jieba（中文分词），`ontology` 额外装 rdflib + owlrl（OWL 2 RL 推理）。只做解析/检索也可以不装。

## 初始化本地库

所有用户数据都放在 `FINKB_HOME`（不设置时：源码安装在项目目录，pip 安装在 `~/.finkb`）。建议显式设置：

```bash
export FINKB_HOME="$HOME/.finkb"
python -m finkb.library init
```

初始化后的目录：

```text
$FINKB_HOME/
├── config/        # 指标字典、本体、检索配置（从包模板复制，可改）
├── raw/           # 你的年报 PDF + manifest.csv
├── data/          # parsed/、facts.duckdb、wiki.duckdb、_search/
└── wiki_vault/    # 编译出的知识页
```

## 配置（.env）

```bash
cp $FINKB_HOME/config/.env.example $FINKB_HOME/config/.env
```

编辑 `.env`：

| 变量 | 必填 | 作用 |
| --- | --- | --- |
| `LLM_API_KEY` | **建库必填** | 抽取事实、编译知识页（OpenAI 兼容接口） |
| `LLM_BASE_URL` | 否 | 默认 `https://opencode.ai/zen/go` |
| `LLM_MODEL` | 否 | 默认 `deepseek-v4.1-flash` |
| `MINERU_API_TOKEN` | 否 | 在线 PDF 解析；不填走离线兜底 |
| `MINERU_MODEL_VERSION` | 否 | 默认 `vlm` |
| `SILICONFLOW_API_KEY` | 否 | bge-m3 向量；不填只用 FTS/jieba 检索 |

说明：

- 环境变量优先于 `.env`；`.env` 不要提交到 git（已在 `.gitignore`）。
- 没有 LLM key 也能导入：加 `--no-facts --no-wiki`，只做解析 + 检索，问答只有「原文」档可用。

## 建库

### 方式 A：直接导入 PDF（自动解析）

```bash
python -m finkb.library add \
  --pdf ~/下载/600519_2025_年报.pdf \
  --company 600519 --period 2025 --type 年报 --name 贵州茅台
```

- 解析默认 `offline`（PyMuPDF 兜底）；有 MinerU token 时可用 `--parse-mode mineru` 获得更好版式效果。
- 解析后自动：抽取事实 → 编译知识页 → 重建切块与检索索引。
- 可选参数：`--no-facts`、`--no-wiki`、`--embed`（用 SiliconFlow 建向量，较慢）、`--name`（公司简称）。

### 方式 B：导入已有解析产物

如果你已有符合 FinKB 结构的解析目录（含 `report.json`、`page_anchored.md`、`content_list.json`）：

```bash
python -m finkb.library add \
  --parsed /path/to/002594_2025_年报 \
  --company 002594 --period 2025 --type 年报 --name 比亚迪
```

### 期间与文档命名规则

| 报告类型 | `--period` 示例 | 生成的 doc_id |
| --- | --- | --- |
| 年报 | `2025` | `600519_2025_年报` |
| 半年报 | `2025H1` | `600519_2025_半年报` |
| 一季报 | `2025Q1` | `600519_2025_一季报` |
| 三季报 | `2025Q3` | `600519_2025_三季报` |

公司代码必须是 6 位数字；只支持 A 股年报/半年报/一季报/三季报。

### 管理本地库

```bash
python -m finkb.library list                  # 已导入文档
python -m finkb.library status                # 目录/计数/依赖状态
python -m finkb.library remove 600519_2025_年报
python -m finkb.library rebuild --embed       # 重建切块/索引（可选向量）
```

## 使用（CLI）

```bash
# 问答：自动选档（指标 → 本体 → 原文）
finkb ask "贵州茅台 2025 年营业收入是多少亿元？"
finkb ask "比亚迪 2025 年海外布局怎么样？"
finkb ask "招商银行 2025 年半年报净利息收益率的变化情况？"

# 指定档位 / 查看路由与校验细节
finkb ask --tier metric --explain "赛力斯 2025 年第一季度营业收入是多少？"

# 交互模式（:q 退出）
finkb ask

# 跨期对比：同一指标多期间并排（数字仍来自事实库，带页码引用）
finkb compare "美的集团 2023-2025 年营业收入"
finkb compare "比亚迪 2024H1 和 2024 年营业收入" --periods 2024H1,2024

# 原文检索（块级）
finkb search "研发投入" --company 002594 --period 2025
finkb search "海外" --company 002594 --no-dense

# 关系图 / 本体
finkb graph 比亚迪
finkb graph --concept self:gross_margin
finkb graph --overview

# 库状态
finkb status
```

问答输出包含：档位（Tier 1/2/3）、置信度、账本（`{{cN}}` 数值来源）、引用（`doc#P页码`）、校验结果。数字类问题会同时给出原文金额（如「原文 803,964,958,000.00 元」）。

## 使用（MCP）

`finkb-mcp` 是 stdio MCP server，可接入 Claude Desktop、Cursor、DSH 等支持 MCP 的客户端。

```json
{
  "mcpServers": {
    "finkb": {
      "command": "/绝对路径/.venv/bin/finkb-mcp",
      "args": ["--home", "/home/你的用户名/.finkb"]
    }
  }
}
```

也可以不传 `--home`，用环境变量 `FINKB_HOME`。工具列表：

| 类别 | 工具 |
| --- | --- |
| 问答 | `ask`、`compare`、`query_metric`、`search_documents`、`describe_ontology`、`traverse_relations` |
| 合成/审查 | `evidence`、`verify` |
| 本地库 | `init_library`、`library_status`、`list_documents`、`add_document`、`remove_document`、`rebuild_index` |

> MCP 客户端通常有超时限制；`add_document` 首次解析/抽取可能较慢，建议先用 CLI 建库，再让 Agent 用问答工具。

## 使用（DSH 插件版）

`dsh-finkb/` 是本仓库的 **DSH 插件发布版**（npm bundle，版本 `finkb-dsh@0.2.0`）：把同一套 MCP 服务、14 个工具、「财报助手」预设和设置页密钥卡片一次性接入 DSH，不复制任何业务逻辑、不带数据。

```bash
# 先装 Python 端（提供 finkb-mcp），再装 DSH 插件
pip install finkb
dsh plugin --profile web add file:/绝对路径/FinKB/dsh-finkb
export FINKB_MCP_BIN=/绝对路径/.venv/bin/finkb-mcp   # 开发态需要；pip 安装后在 PATH 上可省略
```

装完后：插件页出现 `finkb-dsh`、会话里多 14 个 `mcp__finkb__*` 工具、预设选择器多「财报助手」、设置页左侧多「FinKB」（填密钥、保存即热加载）。

详见 [`dsh-finkb/README.md`](dsh-finkb/README.md)。

## 数据目录、备份与迁移

- 全部数据都在 `FINKB_HOME`，复制该目录即可迁移（`.env` 不复制则需重新配置）。
- 可重建：`data/_search/`（`library rebuild`）、`data/parsed/`（重新解析）。
- 建议备份：`raw/`（原始 PDF）、`config/wiki_scope.yaml`、`data/library.json`。
- 删除 `FINKB_HOME` 即清空本地库。

## 常见问题

- **没有 LLM key**：用 `--no-facts --no-wiki` 建库；问答只有原文检索档，指标/本体档不可用。
- **没有 MinerU token**：离线解析可用，但复杂版式（多栏、扫描页）效果会差。
- **没有 SiliconFlow key**：检索自动只用 FTS/jieba，不走向量。
- **建库很慢**：LLM 抽取是主要耗时；一季报约 1 分钟，年报可能 10–30 分钟。
- **支持哪些报告**：A 股年报/半年报/一季报/三季报；不支持港股、美股、招股书。
- **数据边界**：只覆盖你导入的年报，不是全市场；跨公司问题需要先导入对应公司。

## 开发与测试

```bash
cd finkb
../.venv/bin/python -m pytest -q     # 离线单测
bash scripts/check.sh                 # 完整门禁（需要开发语料）
```

仓库**不包含开发语料**（PDF、facts、chunks、vectors、wiki_vault）。依赖开发数据的少数测试（如 `test_cli.py`、`test_mcp_server.py`）在干净克隆下会自动 skip；`check.sh` 中依赖语料的步骤也会跳过。

## 目录结构

```text
FinKB/
├── finkb/                # Python 包
│   ├── src/finkb/        代码（parser / fact_engine / ontology / search / wiki / qa / library）
│   ├── config/           配置模板（随包安装到 share/finkb/config）
│   ├── tests/            离线单测
│   └── scripts/check.sh  质量门禁
├── eval/                 评测题库与脚本（开发用，针对开发语料）
└── README.md
```

## 分发边界与 License

代码以 **Apache License 2.0** 发布（见 [LICENSE](LICENSE)）。仓库不附带任何年报 PDF 或由年报衍生的数据；使用者需自行确保年报的获取与使用符合相关法律法规和披露平台条款。
