"""chaincore —— 素材链路语义（跨框架共享，零框架依赖）。

同一条业务链路：

    起点解析 → 素材/创意解析 → 日期分流（CK / ODPS）→ 人工确认 → 查询 → 合并 → 摘要

这里只放**不随编排框架变化**的部分：每个阶段做什么、返回什么。
`adk/`、`langchain/`、`langgraph/` 三个目录只负责表达同一句话：
「下一步走哪，由谁决定」。

每个阶段函数接收当前状态，返回**增量 dict**，由调用方合并回状态。
"""
from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

#: 7 天以内的历史数据走 ClickHouse 快路径，其余（今天/未来/更早）走 ODPS
CK_WINDOW_DAYS = 7

#: demo 固定注入"今天"，保证三个实现输出可复现
DEFAULT_TODAY = "2026-11-20"

#: 跨框架一致性断言里比较的字段（trace 由各框架自己写，不参与比较）
RESULT_KEYS = (
    "start_type",
    "route",
    "needs_approval",
    "status",
    "creatives",
    "merged",
    "message",
)


# ------------------------------------------------------------------ 工具


def md5(text: str) -> str:
    """稳定哈希，替代生产里的素材/创意唯一标识。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _trace(state: dict[str, Any], line: str) -> list[str]:
    return [*state.get("trace", []), line]


def initial_state(
    start_value: str,
    *,
    today: str = DEFAULT_TODAY,
    start_type: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    approved: bool = False,
    question: str = "",
) -> dict[str, Any]:
    """构造初始状态。所有框架从同一份初始状态出发。"""
    return {
        "question": question,
        "start_value": start_value,
        "start_type": start_type or "",
        "today": today,
        "date_start": date_start or "",
        "date_end": date_end or "",
        "approved": approved,
        "trace": [],
    }


# ------------------------------------------------------------------ 阶段


def parse_request(state: dict[str, Any]) -> dict[str, Any]:
    """解析起点类型：URL 当素材，其他当创意 md5。纯代码，不经过模型。"""
    value = state.get("start_value", "")
    start_type = state.get("start_type") or (
        "asset_url" if value.startswith(("http://", "https://")) else "creative_md5"
    )
    return {"start_type": start_type, "trace": _trace(state, f"parse_request({start_type})")}


def resolve_creatives(state: dict[str, Any]) -> dict[str, Any]:
    """素材 URL → 素材 md5 → 创意 md5；创意起点直接拆分。"""
    if state["start_type"] == "asset_url":
        material = md5(state["start_value"])
        creative = md5(material + ":creative")
        return {
            "creatives": [creative],
            "trace": _trace(state, f"resolve_creatives(url→{material[:8]}→{creative[:8]})"),
        }
    creatives = [item.strip() for item in state["start_value"].split(",") if item.strip()]
    return {"creatives": creatives, "trace": _trace(state, f"resolve_creatives({len(creatives)} 个创意)")}


def route_by_date(state: dict[str, Any]) -> dict[str, Any]:
    """日期分流：7 天内历史 → ClickHouse；今天/未来/更早 → ODPS。"""
    today = date.fromisoformat(state["today"])
    start_text = state.get("date_start") or state["today"]
    end_text = state.get("date_end") or start_text
    start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)

    within_window = (today - start).days <= CK_WINDOW_DAYS and (today - end).days <= CK_WINDOW_DAYS
    route = "ck" if end < today and within_window else "odps"
    return {
        "route": route,
        "needs_approval": route == "odps",
        "trace": _trace(state, f"route_by_date({start_text}..{end_text}) → {route}"),
    }


def pending(state: dict[str, Any]) -> dict[str, Any]:
    """未获批准时的挂起结果：交回前端或下一轮恢复。"""
    return {
        "status": "pending_approval",
        "message": "ODPS 查询需要确认，已暂停；确认后重新进入链路并放行到查询节点。",
        "trace": _trace(state, "await_approval → pending"),
    }


def query_ck(state: dict[str, Any]) -> dict[str, Any]:
    """ClickHouse 快路径：只有落地页，没有深链、dsp_id 与具体时间。"""
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


def query_odps(state: dict[str, Any]) -> dict[str, Any]:
    """ODPS 明细：额外返回深链、dsp_id 与具体时间。"""
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


def merge(state: dict[str, Any]) -> dict[str, Any]:
    """合并去重：按（创意 / 落地页 / 日期）三元组。"""
    seen: set[tuple[Any, Any, Any]] = set()
    merged: list[dict[str, Any]] = []
    for row in state.get("rows", []):
        key = (row.get("creative_md5"), row.get("landing"), row.get("bns_date"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return {"merged": merged, "trace": _trace(state, f"merge({len(merged)} 行)")}


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    """给用户的摘要：只给行数与列名，不内联明细。"""
    merged = state.get("merged", [])
    if merged:
        columns = list(merged[0].keys())
        message = f"共 {len(merged)} 行；列：{'、'.join(columns)}"
    else:
        message = "未查询到数据"
    return {"status": "completed", "message": message, "trace": _trace(state, "summarize")}


def result_view(state: dict[str, Any]) -> dict[str, Any]:
    """把最终状态裁成可比较的结果视图。"""
    return {key: state.get(key) for key in RESULT_KEYS}


# ------------------------------------------------------------------ demo


def scenarios() -> list[dict[str, Any]]:
    """三个固定场景：CK 快路径 / ODPS 挂起 / 批准后完成。"""
    url = "https://cdn.example.com/asset/abc.jpg"
    return [
        {
            "title": "素材 URL + 7 天内历史 → ClickHouse 快路径",
            "kwargs": {"start_value": url, "date_start": "2026-11-17", "date_end": "2026-11-18"},
        },
        {
            "title": "素材 URL + 今天 → ODPS，需人工确认（挂起）",
            "kwargs": {"start_value": url, "date_start": "2026-11-20", "date_end": "2026-11-20"},
        },
        {
            "title": "人工确认后 → ODPS 完成",
            "kwargs": {
                "start_value": url,
                "date_start": "2026-11-20",
                "date_end": "2026-11-20",
                "approved": True,
            },
        },
    ]


__all__ = [
    "CK_WINDOW_DAYS",
    "DEFAULT_TODAY",
    "RESULT_KEYS",
    "md5",
    "initial_state",
    "parse_request",
    "resolve_creatives",
    "route_by_date",
    "pending",
    "query_ck",
    "query_odps",
    "merge",
    "summarize",
    "result_view",
    "scenarios",
]
