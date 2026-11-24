"""ADK 实现：**控制流写成顺序 Agent + 节点内守卫**。

同一业务链路在三个框架下各实现一次（`adk/`、`langchain/`、`langgraph/`），
阶段语义统一来自 `chaincore`，这里只表达「下一步走哪」。

ADK 的 `SequentialAgent` 只提供"依次执行"，**没有条件边**：
分支要么收敛到节点内部判断（本实现），要么交给模型用 `transfer` 决定。
这是三个实现里最值得对照的一点。

运行：
    pip install -r adk/requirements.txt
    python adk/chain.py --demo
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # 支持直接 `python adk/chain.py --demo`
    sys.path.append(str(Path(__file__).resolve().parent.parent))

from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.events import Event, EventActions
from google.adk.runners import InMemoryRunner
from google.genai import types

import chaincore as C

APP_NAME = "material_chain"
USER_ID = "demo"
SESSION_ID = "s1"


# ------------------------------------------------------------------ 阶段


def _stage(name: str, fn: Callable[[dict[str, Any]], dict[str, Any]]) -> BaseAgent:
    """把一个阶段函数包成 ADK 自定义 Agent。

    自定义 Agent 只需实现 `_run_async_impl`：往事件流里 yield `Event`，
    状态变更通过 `EventActions(state_delta=...)` 交给 SessionService 落盘。
    """

    class Stage(BaseAgent):
        async def _run_async_impl(self, ctx) -> AsyncGenerator[Event, None]:
            delta = fn(dict(ctx.session.state))
            payload: dict[str, Any] = {
                "author": self.name,
                "content": types.Content(role="model", parts=[types.Part(text=self.name)]),
            }
            # 只有产生增量时才写 state_delta（传 None 会被 pydantic 拒绝）
            if delta:
                payload["actions"] = EventActions(state_delta=delta)
            yield Event(**payload)

    return Stage(name=name, description=name)


def _is_pending(state: dict[str, Any]) -> bool:
    return state.get("status") == "pending_approval"


def confirm(state: dict[str, Any]) -> dict[str, Any]:
    """ODPS 长查询前的人工确认：未批准就挂起。"""
    if state.get("route") == "odps" and not state.get("approved"):
        return C.pending(state)
    return {}


def query(state: dict[str, Any]) -> dict[str, Any]:
    """分支收敛点：在节点内部决定查 CK 还是 ODPS。"""
    if _is_pending(state):
        return {}
    if state.get("route") == "ck":
        return C.query_ck(state)
    return C.query_odps(state)


def merge(state: dict[str, Any]) -> dict[str, Any]:
    return {} if _is_pending(state) else C.merge(state)


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    return {} if _is_pending(state) else C.summarize(state)


def build_pipeline() -> BaseAgent:
    """顺序 Agent：7 个阶段依次执行，分支靠节点内守卫跳过。"""
    return SequentialAgent(
        name="material_chain",
        sub_agents=[
            _stage("parse_request", C.parse_request),
            _stage("resolve_creatives", C.resolve_creatives),
            _stage("route_by_date", C.route_by_date),
            _stage("confirm", confirm),
            _stage("query", query),
            _stage("merge", merge),
            _stage("summarize", summarize),
        ],
    )


# ------------------------------------------------------------------ 运行


async def _run_async(kwargs: dict[str, Any]) -> dict[str, Any]:
    state = C.initial_state(**kwargs)
    runner = InMemoryRunner(agent=build_pipeline(), app_name=APP_NAME)
    await runner.session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID
    )
    message = types.Content(role="user", parts=[types.Part(text="run")])
    async for _ in runner.run_async(
        user_id=USER_ID,
        session_id=SESSION_ID,
        new_message=message,
        state_delta=state,
    ):
        pass
    session = await runner.session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID
    )
    return dict(session.state)


def run(**kwargs: Any) -> dict[str, Any]:
    """跑一次完整链路，返回最终状态（含 trace）。"""
    return asyncio.run(_run_async(kwargs))


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
    parser = argparse.ArgumentParser(description="ADK 实现的素材链路")
    parser.add_argument("--demo", action="store_true", help="跑内置的三个演示场景")
    args = parser.parse_args()
    if args.demo:
        demo()
    else:
        print(run(**C.scenarios()[0]["kwargs"]).get("message"))
