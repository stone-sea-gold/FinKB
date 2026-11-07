#!/usr/bin/env bash
# 一条命令跑完「代码对不对」和「29 份产物对不对」。
# 用法: scripts/check.sh   （PYTHON=... 可覆盖解释器路径）
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-../.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  echo "找不到解释器 $PYTHON，用 PYTHON=... 指定" >&2
  exit 2
fi

echo "== 单元测试 =="
"$PYTHON" -m pytest -q

echo
echo "== 全语料守恒律 + 基线对照 =="
if [ -d ../raw ]; then
  "$PYTHON" tools/audit.py corpus --baseline config/corpus-baseline.json --pdf-root ../raw
else
  echo "跳过：../raw 不存在（仓库不含开发语料）"
fi

echo
echo "== 本体链条完整性 =="
if [ -f data/facts.duckdb ]; then
  "$PYTHON" -m finkb.ontology --db data/facts.duckdb --wiki data/wiki.duckdb audit --strict
else
  echo "跳过：data/facts.duckdb 不存在"
fi

echo
echo "== 每份文档事实数（坑-80）=="
if [ -f data/facts.duckdb ]; then
  "$PYTHON" tools/facts_baseline.py --db data/facts.duckdb --check
else
  echo "跳过：data/facts.duckdb 不存在"
fi

echo
echo "== 指标治理（Tier 1 定义/SQL 安全/连库）=="
if [ -f data/facts.duckdb ]; then
  "$PYTHON" tools/metric_check.py --db data/facts.duckdb
else
  echo "跳过：data/facts.duckdb 不存在"
fi

echo
echo "== 本体质量门禁（OntoQA/OoPS 式结构检查）=="
if [ -f data/facts.duckdb ]; then
  "$PYTHON" -m finkb.ontology.quality --db data/facts.duckdb
else
  echo "跳过：data/facts.duckdb 不存在"
fi

echo
echo "== 闭世界数据校验（约束求值/实体回填/附注覆盖）=="
if [ -f data/facts.duckdb ]; then
  "$PYTHON" -m finkb.fact_engine.data_check --db data/facts.duckdb
else
  echo "跳过：data/facts.duckdb 不存在"
fi

echo
echo "== 文字题召回（离线 FTS，Recall@5 下限 0.9）=="
if [ -f data/_search/chunks.db ]; then
  "$PYTHON" ../eval/recall.py --no-dense --min-recall 0.9
else
  echo "跳过：data/_search/chunks.db 不存在"
fi

echo
echo "== 多轮上下文（12 组宿主模拟）=="
if [ -f data/facts.duckdb ] && [ -f data/_search/chunks.db ]; then
  "$PYTHON" ../eval/check_context.py
else
  echo "跳过：data/facts.duckdb 或 data/_search/chunks.db 不存在"
fi

echo
echo "== 跨期对比（6 道离线验收）=="
if [ -f data/facts.duckdb ]; then
  "$PYTHON" ../eval/check_compare.py
else
  echo "跳过：data/facts.duckdb 不存在"
fi

echo
echo "== 金标事实集 =="
if [ -f data/facts.duckdb ]; then
  "$PYTHON" tools/gold_check.py --db data/facts.duckdb --gold config/gold_facts.yaml
else
  echo "跳过：data/facts.duckdb 不存在"
fi
