# FinKB

本体驱动的 A 股财报知识库：从年报 PDF 解析结构化事实与知识页，提供「指标 / 本体 / 原文」三档问答、混合检索、关系图与 MCP 接口。

> 版本 **0.1.0** · 代码与数据分离：仓库只含代码与配置模板，用户用自己的年报在本地建库。

## 能力

- **解析**：MinerU 在线解析 / 离线兜底；跨页表格缝合、10 类版式。
- **事实层**：36 个财务指标、单位归一、守恒律校验、每个数字可回查原文页。
- **本体层**：62 概念 / 71 关系 / 10 约束 / 8 个数值定义类；OWL 2 RL 批量物化，推断带血缘。
- **检索**：FTS5 + jieba + bge-m3 向量 + RRF 融合；块级引用可回查。
- **问答**：三档 fall-through（指标 SQL → 本体推理 → 原文检索）；数字只由 SQL/代码产生，不交给 LLM 编。
- **接口**：统一 CLI（`finkb ask/search/graph/eval/status`）+ MCP stdio（11 个工具）。

## 安装

要求 Python **3.10+**；不需要 GPU / Java / Docker。

```bash
git clone https://github.com/stone-sea-gold/FinKB.git
cd FinKB/finkb
python3 -m venv ../.venv
../.venv/bin/pip install -e ".[search,ontology]"
```

## 快速开始（用自己的年报建库）

```bash
export FINKB_HOME=~/finkb-data
python -m finkb.library init
cp $FINKB_HOME/config/.env.example $FINKB_HOME/config/.env   # 至少填 LLM_API_KEY

python -m finkb.library add --pdf ~/下载/600519_2025_年报.pdf \
  --company 600519 --period 2025 --type 年报 --name 贵州茅台

python -m finkb.qa "贵州茅台 2025 年营业收入是多少亿元？"
python -m finkb.library status
```

MCP 客户端（Claude Desktop / Cursor / DSH 等）：

```json
{
  "mcpServers": {
    "finkb": {
      "command": "/绝对路径/.venv/bin/finkb-mcp",
      "args": ["--home", "/home/你的用户名/finkb-data"]
    }
  }
}
```

## 目录结构

```
finkb/                 Python 包、CLI 与 MCP（详见 finkb/README.md）
├── src/finkb/         代码
├── config/            配置模板（随包安装）
├── tests/             离线单测
└── scripts/check.sh   质量门禁
raw/manifest.csv      开发语料清单（PDF 不入库）
eval/                 评测题库与脚本（针对开发语料）
FinKB-*.md            方案、盘点与问题记录
```

## 数据与分发

- 仓库**不包含** PDF、facts、向量、wiki 页；`finkb/data/`、`raw/*.pdf`、`finkb/wiki_vault/` 已 gitignore。
- 用户数据全部在 `FINKB_HOME`（默认源码树；pip 安装后 `~/.finkb`），删除目录即可清空。
- `finkb eval` 的 50 题评测针对项目自带开发语料，不适用于用户库。

## 测试

```bash
cd finkb
../.venv/bin/python -m pytest -q
```

离线可跑；部分回归测试需要开发语料（`data/facts.duckdb` 等），干净克隆下会失败或跳过。

## License

未定（如需开源协议请告知）。
