#!/usr/bin/env bash
# 一条命令跑完「代码对不对」和「22 份产物对不对」。
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
"$PYTHON" tools/audit.py corpus --baseline config/corpus-baseline.json
