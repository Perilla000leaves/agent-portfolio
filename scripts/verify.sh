#!/usr/bin/env bash
# 一条命令验证：
#   1) 单测（含跨框架一致性断言，缺依赖的框架会自动跳过）
#   2) 三套编排实现的演示（装了对应框架才跑，否则跳过）
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

demo() {
  local label="$1" file="$2" module="$3"
  if "$PY" -c "import $module" >/dev/null 2>&1; then
    echo
    echo "== $label =="
    "$PY" "$file" --demo
  else
    echo
    echo "== skip $label =="
    echo "   pip install -r $(dirname "$file")/requirements.txt"
  fi
}

demo "langgraph demo" langgraph/chain.py langgraph.graph
demo "langchain demo" langchain/chain.py langchain_core.runnables
demo "adk demo" adk/chain.py google.adk.agents
