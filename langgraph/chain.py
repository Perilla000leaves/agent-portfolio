"""LangGraph 实现：**控制流写在图的边里**。

同一业务链路在三个框架下各实现一次（`adk/`、`langchain/`、`langgraph/`），
阶段语义统一来自 `chaincore`，这里只表达「下一步走哪」。

运行（必须用脚本方式，不能用 `-m`）：
    pip install -r langgraph/requirements.txt
    python langgraph/chain.py --demo

命名坑：安装的 `langgraph` 是**命名空间包**（没有 `__init__.py`），所以本目录会被
并进它的 `__path__`。只要本目录里不出现与库同名的子模块（比如 `graph.py` 会撞上
库的 `langgraph/graph/`），`from langgraph.graph import ...` 仍能解析到真实库。
这也是本文件叫 `chain.py` 而不是 `graph.py` 的原因。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, TypedDict

if __package__ in (None, ""):  # 支持直接 `python langgraph/chain.py --demo`
    sys.path.append(str(Path(__file__).resolve().parent.parent))

from langgraph.graph import END, START, StateGraph

import chaincore as C


class AgentState(TypedDict, total=False):
    """图状态：节点只返回自己要更新的键。"""

    question: str
    start_value: str
    start_type: str
    today: str
    date_start: str
    date_end: str
    approved: bool
    creatives: list[str]
    route: str
    needs_approval: bool
    status: str
    rows: list[dict[str, Any]]
    merged: list[dict[str, Any]]
    message: str
    trace: list[str]


# ------------------------------------------------------------------ 节点


def _node(fn):
    """把 chaincore 的阶段函数包成 LangGraph 节点（状态进、增量出）。"""

    def run(state: AgentState) -> dict[str, Any]:
        return fn(dict(state))

    run.__name__ = fn.__name__
    return run


def need_confirm(state: AgentState) -> dict[str, Any]:
    """确认检查点：对应生产里的成本确认，只决定下面走审批还是查询。"""
    needs = state.get("route") == "odps"
    return {
        "needs_approval": needs,
        "trace": [*state.get("trace", []), f"need_confirm({needs})"],
    }


# ------------------------------------------------------------------ 装配


def _route_key(state: AgentState) -> str:
    return state.get("route", "odps")


def _approval_key(state: AgentState) -> str:
    return "query" if state.get("approved") else "approval"


def build_graph() -> Any:
    """构建并编译状态图。"""
    graph = StateGraph(AgentState)
    for name, fn in (
        ("parse_request", C.parse_request),
        ("resolve_creatives", C.resolve_creatives),
        ("route_by_date", C.route_by_date),
        ("await_approval", C.pending),
        ("query_ck", C.query_ck),
        ("query_odps", C.query_odps),
        ("merge", C.merge),
        ("summarize", C.summarize),
    ):
        graph.add_node(name, _node(fn))
    graph.add_node("need_confirm", need_confirm)

    graph.add_edge(START, "parse_request")
    graph.add_edge("parse_request", "resolve_creatives")
    graph.add_edge("resolve_creatives", "route_by_date")
    # 分支一：日期分流
    graph.add_conditional_edges(
        "route_by_date", _route_key, {"ck": "query_ck", "odps": "need_confirm"}
    )
    # 分支二：是否需要人工确认
    graph.add_conditional_edges(
        "need_confirm", _approval_key, {"approval": "await_approval", "query": "query_odps"}
    )
    graph.add_edge("query_ck", "merge")
    graph.add_edge("query_odps", "merge")
    graph.add_edge("merge", "summarize")
    graph.add_edge("await_approval", END)
    graph.add_edge("summarize", END)
    return graph.compile()


def run(**kwargs: Any) -> dict[str, Any]:
    """跑一次完整链路，返回最终状态（含 trace）。"""
    return build_graph().invoke(C.initial_state(**kwargs))


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
    parser = argparse.ArgumentParser(description="LangGraph 实现的素材链路")
    parser.add_argument("--demo", action="store_true", help="跑内置的三个演示场景")
    args = parser.parse_args()
    if args.demo:
        demo()
    else:
        print(run(**C.scenarios()[0]["kwargs"]).get("message"))
