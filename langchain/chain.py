"""LangChain 实现：**控制流写成 Runnable 的组合**。

同一业务链路在三个框架下各实现一次（`adk/`、`langchain/`、`langgraph/`），
阶段语义统一来自 `chaincore`，这里只表达「下一步走哪」。

用 LCEL 的 `|` 串行、`RunnableBranch` 表达二选一的分支——
与 LangGraph 的区别是：这里没有显式的图结构，分支是"运行时的三选一"。

运行：
    pip install -r langchain/requirements.txt
    python langchain/chain.py --demo
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # 支持直接 `python langchain/chain.py --demo`
    sys.path.append(str(Path(__file__).resolve().parent.parent))

from langchain_core.runnables import Runnable, RunnableBranch, RunnableLambda

import chaincore as C


def _step(fn) -> Runnable:
    """把 chaincore 的阶段函数包成一个 Runnable：读状态、返回合并后的新状态。"""

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        return {**state, **fn(state)}

    return RunnableLambda(apply)


def _tail(query: Runnable) -> Runnable:
    """查询 → 合并 → 摘要，CK 与 ODPS 两条分支共用尾部。"""
    return query | _step(C.merge) | _step(C.summarize)


def build_chain() -> Runnable:
    """LCEL：顺序由 `|` 保证，分支由 `RunnableBranch` 表达。

    `RunnableBranch` 是"按顺序检查条件、命中即执行"，所以三选一的顺序很重要：
    先判断走哪条查询，再判断是挂起还是放行。
    """
    return (
        _step(C.parse_request)
        | _step(C.resolve_creatives)
        | _step(C.route_by_date)
        | RunnableBranch(
            (lambda state: state.get("route") == "ck", _tail(_step(C.query_ck))),
            (lambda state: not state.get("approved"), _step(C.pending)),
            _tail(_step(C.query_odps)),
        )
    )


def run(**kwargs: Any) -> dict[str, Any]:
    """跑一次完整链路，返回最终状态（含 trace）。"""
    return build_chain().invoke(C.initial_state(**kwargs))


# ------------------------------------------------------------------ demo


def _print(title: str, state: dict[str, Any]) -> None:
    print(f"\n=== {title} ===")
    for line in state.get("trace", []):
        print("  -", line)
    print("  status:", state.get("status"))
    print("  message:", state.get("message"))
    if state.get("merged"):
        print("  rows:", state["merged"][0])


def demo() -> None:
    for scenario in C.scenarios():
        _print(scenario["title"], run(**scenario["kwargs"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LangChain 实现的素材链路")
    parser.add_argument("--demo", action="store_true", help="跑内置的三个演示场景")
    args = parser.parse_args()
    if args.demo:
        demo()
    else:
        print(run(**C.scenarios()[0]["kwargs"]).get("message"))
