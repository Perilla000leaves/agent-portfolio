#!/usr/bin/env bash
# 一条命令验证：
#   1) 单测（优先用项目内 .venv，没有就退回系统 python）
#   2) LangGraph 复刻演示（装了 langgraph 才跑，否则跳过）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  PY="python"
fi

echo "== unit tests =="
"$PY" -m pytest -q

if "$PY" -c "import langgraph" >/dev/null 2>&1; then
  echo
  echo "== langgraph replica demo =="
  "$PY" langgraph-replica/graph.py --demo
else
  echo
  echo "== skip langgraph demo =="
  echo "   pip install -r langgraph-replica/requirements.txt"
fi
