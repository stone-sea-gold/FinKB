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
"$PYTHON" tools/audit.py corpus --baseline config/corpus-baseline.json --pdf-root ../raw

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
echo "== 金标事实集 =="
if [ -f data/facts.duckdb ]; then
  "$PYTHON" tools/gold_check.py --db data/facts.duckdb --gold config/gold_facts.yaml
else
  echo "跳过：data/facts.duckdb 不存在"
fi
