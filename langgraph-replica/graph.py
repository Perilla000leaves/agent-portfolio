"""LangGraph 复刻：素材起点 → 创意 → 落地页/深链 → 合并。

对照 `ad_agent_sample3` 里的同一条链路：
- ADK 版：模型自主选工具，hook 负责补参/守卫/确认，靠提示词约束顺序；
- 这里：把「起点解析、日期分流、ODPS 需审批、结果合并」写成显式节点与条件边。

两种写法语义等价，差别在控制流归属：一个在提示词，一个在代码。

运行：
    pip install -r langgraph-replica/requirements.txt
    python langgraph-replica/graph.py --demo
"""
from __future__ import annotations

import argparse
import hashlib
from datetime import date
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph


class AgentState(TypedDict, total=False):
    """图状态：所有节点只返回自己要更新的键。"""

    question: str
    start_type: str          # asset_url | creative_md5
    start_value: str
    today: str               # 注入固定日期，保证 demo 可复现
    date_start: str
    date_end: str
    approved: bool           # ODPS 段是否已获人工批准

    creatives: list[str]
    route: str               # ck | odps
    needs_approval: bool
    status: str              # completed | pending_approval
    rows: list[dict[str, str]]
    merged: list[dict[str, str]]
    message: str
    trace: list[str]


def _trace(state: AgentState, line: str) -> list[str]:
    return [*state.get("trace", []), line]


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- 节点


def parse_request(state: AgentState) -> dict[str, Any]:
    """解析起点类型（纯代码，不经过模型）。"""
    value = state.get("start_value", "")
    start_type = state.get("start_type") or (
        "asset_url" if value.startswith(("http://", "https://")) else "creative_md5"
    )
    return {"start_type": start_type, "trace": _trace(state, f"parse_request({start_type})")}


def resolve_creatives(state: AgentState) -> dict[str, Any]:
    """素材 URL → material_md5 → creative_md5；创意起点直接拆分。"""
    if state["start_type"] == "asset_url":
        material = _md5(state["start_value"])
        creative = _md5(material + ":creative")
        return {
            "creatives": [creative],
            "trace": _trace(state, f"resolve_creatives(url→{material[:8]}→{creative[:8]})"),
        }
    creatives = [item.strip() for item in state["start_value"].split(",") if item.strip()]
    return {"creatives": creatives, "trace": _trace(state, f"resolve_creatives({len(creatives)} 个创意)")}


def route_by_date(state: AgentState) -> dict[str, Any]:
    """日期分流：今天/未来/超过 7 天 → ODPS；7 天内历史 → ClickHouse。"""
    today = date.fromisoformat(state["today"])
    start_text = state.get("date_start") or state["today"]
    end_text = state.get("date_end") or start_text
    start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)

    within_7 = (today - start).days <= 7 and (today - end).days <= 7
    route = "ck" if end < today and within_7 else "odps"
    return {
        "route": route,
        "needs_approval": route == "odps",
        "trace": _trace(state, f"route_by_date({start_text}..{end_text}) → {route}"),
    }


def need_confirm(state: AgentState) -> dict[str, Any]:
    """确认检查点：ODPS 段需要人工批准（对应生产里的成本确认）。"""
    return {
        "needs_approval": state.get("route") == "odps",
        "trace": _trace(state, f"need_confirm(needs_approval={state.get('route') == 'odps'})"),
    }


def await_approval(state: AgentState) -> dict[str, Any]:
    """未批准：挂起，交给前端/下一轮恢复。"""
    return {
        "status": "pending_approval",
        "message": "ODPS 查询需要确认，已暂停；确认后从 query_odps 继续。",
        "trace": _trace(state, "await_approval → pending"),
    }


def query_ck(state: AgentState) -> dict[str, Any]:
    """ClickHouse 快速路径：只有 landing，没有 dsp_id / ts。"""
    rows = [
        {
            "creative_md5": creative,
            "landing": f"https://landing.example/{creative[:8]}",
            "dplink": "",
            "bns_date": state.get("date_start", ""),
        }
        for creative in state.get("creatives", [])
    ]
    return {"rows": rows, "trace": _trace(state, f"query_ck({len(rows)} 行)")}


def query_odps(state: AgentState) -> dict[str, Any]:
    """ODPS 明细：额外返回 dsp_id 与具体时间。"""
    rows = [
        {
            "creative_md5": creative,
            "landing": f"https://landing.example/{creative[:8]}",
            "dplink": f"https://dp.example/{creative[:8]}",
            "bns_date": state.get("date_start", ""),
            "dsp_id": "1001",
            "ts": f"{state.get('date_start', '')} 10:05:00",
        }
        for creative in state.get("creatives", [])
    ]
    return {"rows": rows, "trace": _trace(state, f"query_odps({len(rows)} 行)")}


def merge(state: AgentState) -> dict[str, Any]:
    """合并 + 去重（按 creative/landing/日期）。"""
    seen = set()
    merged: list[dict[str, str]] = []
    for row in state.get("rows", []):
        key = (row.get("creative_md5"), row.get("landing"), row.get("bns_date"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return {"merged": merged, "trace": _trace(state, f"merge({len(merged)} 行)")}


def summarize(state: AgentState) -> dict[str, Any]:
    """产出给用户的摘要（模型侧可读，不内联明细）。"""
    merged = state.get("merged", [])
    columns = list(merged[0].keys()) if merged else []
    message = f"共 {len(merged)} 行；列：{'、'.join(columns)}" if merged else "未查询到数据"
    return {
        "status": "completed",
        "message": message,
        "trace": _trace(state, "summarize"),
    }


# --------------------------------------------------------------------- 装配


def _route_key(state: AgentState) -> str:
    return state.get("route", "odps")


def _approval_key(state: AgentState) -> str:
    return "query" if state.get("approved") else "approval"


def build_graph() -> Any:
    """构建并编译图。"""
    graph = StateGraph(AgentState)
    for name, node in (
        ("parse_request", parse_request),
        ("resolve_creatives", resolve_creatives),
        ("route_by_date", route_by_date),
        ("need_confirm", need_confirm),
        ("await_approval", await_approval),
        ("query_ck", query_ck),
        ("query_odps", query_odps),
        ("merge", merge),
        ("summarize", summarize),
    ):
        graph.add_node(name, node)

    graph.add_edge(START, "parse_request")
    graph.add_edge("parse_request", "resolve_creatives")
    graph.add_edge("resolve_creatives", "route_by_date")
    graph.add_conditional_edges(
        "route_by_date", _route_key, {"ck": "query_ck", "odps": "need_confirm"}
    )
    graph.add_conditional_edges(
        "need_confirm", _approval_key, {"approval": "await_approval", "query": "query_odps"}
    )
    graph.add_edge("query_ck", "merge")
    graph.add_edge("query_odps", "merge")
    graph.add_edge("merge", "summarize")
    graph.add_edge("await_approval", END)
    graph.add_edge("summarize", END)
    return graph.compile()


def run(
    start_value: str,
    *,
    today: str = "2026-11-20",
    start_type: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    approved: bool = False,
    question: str = "",
) -> AgentState:
    """跑一次完整链路，返回最终状态（含 trace）。"""
    initial: AgentState = {
        "question": question,
        "start_value": start_value,
        "start_type": start_type or "",
        "today": today,
        "date_start": date_start or "",
        "date_end": date_end or "",
        "approved": approved,
        "trace": [],
    }
    return build_graph().invoke(initial)


# --------------------------------------------------------------------- demo


def _print(title: str, state: AgentState) -> None:
    print(f"\n=== {title} ===")
    for line in state.get("trace", []):
        print("  -", line)
    print("  status:", state.get("status"))
    print("  message:", state.get("message"))
    if state.get("merged"):
        print("  rows:", state["merged"][0])


def demo() -> None:
    """两个场景：CK 快速路径；ODPS 先挂起、批准后完成。"""
    ck = run(
        "https://cdn.example.com/asset/abc.jpg",
        date_start="2026-11-17",
        date_end="2026-11-18",
    )
    _print("素材URL + 7 天内历史 → ClickHouse", ck)

    pending = run(
        "https://cdn.example.com/asset/abc.jpg",
        date_start="2026-11-20",
        date_end="2026-11-20",
        approved=False,
    )
    _print("素材URL + 今天 → ODPS（等待确认）", pending)

    resumed = run(
        "https://cdn.example.com/asset/abc.jpg",
        date_start="2026-11-20",
        date_end="2026-11-20",
        approved=True,
    )
    _print("人工确认后 → ODPS 完成", resumed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LangGraph 复刻链路")
    parser.add_argument("--demo", action="store_true", help="跑内置演示")
    args = parser.parse_args()
    if args.demo:
        demo()
    else:
        final = run("https://cdn.example.com/asset/abc.jpg", date_start="2026-11-19", approved=True)
        print(final.get("message"))
