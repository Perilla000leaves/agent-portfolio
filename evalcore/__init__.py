"""evalcore —— 轨迹判定的归一化与匹配（纯函数，不依赖任何评测框架）。

评测 Agent 时最容易踩的坑不是「模型调错工具」，而是**判定器把对的判成错的**：

- 期望里只写了工具名，实际调用带了 `bns_date_start="20261110"` 这类会漂的参数，
  严格相等直接判 0；
- 只有工具名匹配还不够，`query_adx_*` 这类工具必须真的把 SQL 发到执行层，
  否则只是「看起来调了」；
- 同一工具被重复调用（长任务/ODPS 的典型翻车）不会被工具名匹配发现。

这里把「怎么比对两条轨迹」抽成纯函数：先做成对归一化，再按三种顺序语义匹配，
最后把自定义指标（SQL 是否上报、是否重复调用）作为**信息项**附在结果上。

生产里这一步交给评测框架的官方打分器；本模块是它的**等价实现**，好处是
不装框架也能对判定逻辑写单测。
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

#: 归一化时把参数值替换成这个占位符，比较退化成「key 集合比较」
ANY_VALUE = "<any>"

#: 支持的三条匹配语义
MATCH_MODES = ("exact", "in_order", "any_order")

#: 这些工具必须真的上报 SQL，才算跑到执行层
SQL_TOOL_PREFIX = "query_adx_"


def _as_args(raw: Any) -> dict[str, Any] | None:
    """事件里的 args 常是 JSON 字符串；解析不出 dict 就当「没写参数」。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def normalize_call(call: dict[str, Any]) -> dict[str, Any]:
    """统一成 `{name, args}`；args 缺失记 None。"""
    return {"name": str(call.get("name") or ""), "args": _as_args(call.get("args"))}


def normalize_pair(
    actual: Sequence[dict[str, Any]],
    expected: Sequence[dict[str, Any]],
    args_mode: str = "keys",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """成对归一化实际与期望的调用，返回两份「可以直接相等比较」的列表。

    规则（两侧同一套，保证比较时 `==` 仍然有意义）：

    - 期望侧 `args` 为 None → 不关心参数，实际侧也置空；
    - `args_mode="full"` → 原样比较；
    - 其余（默认 keys 模式）→ 只比较期望提到的那些 key，值统一替换成占位符。

    期望没提到的实际调用不参与参数比较（匹配阶段只看 name 是否出现）。
    """
    act = [normalize_call(call) for call in actual]
    exp = [normalize_call(call) for call in expected]
    paired = [False] * len(act)

    for want in exp:
        for index, have in enumerate(act):
            if paired[index] or have["name"] != want["name"]:
                continue
            paired[index] = True
            if want["args"] is None:
                have["args"] = None
            elif args_mode == "full":
                pass
            else:
                wanted = set(want["args"])
                given = set(have["args"] or {})
                want["args"] = {key: ANY_VALUE for key in sorted(wanted)}
                # 期望要的参数没传 → 实际侧少 key，比较时自然不相等
                have["args"] = {key: ANY_VALUE for key in sorted(wanted & given)}
            break

    for index, have in enumerate(act):
        if not paired[index]:
            have["args"] = None
    return act, exp


def _is_subsequence(needle: Sequence[str], haystack: Sequence[str]) -> bool:
    iterator = iter(haystack)
    return all(item in iterator for item in needle)


def match_names(actual_names: Sequence[str], expected_names: Sequence[str], mode: str = "any_order") -> bool:
    """按三种语义判断工具名是否匹配。

    - `exact`：数量、顺序完全一致；
    - `in_order`：期望是实际调用链路的**子序列**（允许多调别的工具）；
    - `any_order`：只要期望的工具都出现过，顺序不限。
    """
    mode = str(mode or "any_order").strip().lower().replace("-", "_")
    if mode not in MATCH_MODES:
        mode = "any_order"
    actual = [name for name in actual_names if name]
    expected = [name for name in expected_names if name]

    if not expected:
        return False
    if mode == "exact":
        return actual == expected
    if mode == "in_order":
        return _is_subsequence(expected, actual)
    # any_order：多重集合包含（重复调用也算命中，重复本身由 duplicates 单独报）
    return not (Counter(expected) - Counter(actual))


def duplicates(names: Iterable[str]) -> list[str]:
    """找出被重复调用的工具名（保序）。"""
    seen: set[str] = set()
    repeated: list[str] = []
    for name in names:
        if not name:
            continue
        if name in seen and name not in repeated:
            repeated.append(name)
        seen.add(name)
    return repeated


def score_case(
    *,
    expected_tools: Sequence[str] | None = None,
    actual_calls: Sequence[dict[str, Any]],
    expected_calls: Sequence[dict[str, Any]] | None = None,
    error: str = "",
    match_type: str = "any_order",
    args_mode: str = "keys",
) -> dict[str, Any]:
    """给一条用例打分，返回 `{score, pass, reason, metrics}`。

    `pass` 为 None 表示**未判定**（没给期望工具、或本轮无有效调用），
    与 False 严格区分——把「没测」混进通过率是评测里最常见的自欺。
    """
    expected = list(expected_calls or [{"name": name} for name in (expected_tools or [])])
    actual = list(actual_calls)
    metrics: list[dict[str, Any]] = []

    if error:
        return {"score": 0.0, "pass": False, "reason": f"本轮报错：{error[:120]}", "metrics": metrics}
    if not expected:
        return {"score": None, "pass": None, "reason": "未提供期望工具，跳过判定", "metrics": metrics}

    actual_names = [str(call.get("name") or "") for call in actual if call.get("name")]
    expected_names = [str(call.get("name") or "") for call in expected if call.get("name")]
    matched = match_names(actual_names, expected_names, match_type)
    repeated = duplicates(actual_names)

    metrics.append(
        {
            "name": "tool_trajectory",
            "score": 1.0 if matched else 0.0,
            "pass": matched,
            "reason": "" if matched else _miss_reason(expected_names, actual_names),
        }
    )
    # SQL 类工具必须真的上报 SQL：框架的参数比较看不出这一点，只能自建指标
    sql_calls = [call for call in actual if str(call.get("name") or "").startswith(SQL_TOOL_PREFIX)]
    metrics.append(
        {
            "name": "sql_reported",
            "score": None,
            "pass": None,
            "reason": (
                f"调用了 {', '.join(str(c.get('name')) for c in sql_calls)}，需人工核对 SQL"
                if sql_calls
                else ""
            ),
        }
    )
    metrics.append(
        {
            "name": "no_duplicate_calls",
            "score": 0.0 if repeated else 1.0,
            "pass": not repeated,
            "reason": f"重复调用：{', '.join(repeated)}" if repeated else "",
        }
    )

    passed = matched and not repeated
    if passed:
        reason = f"期望工具全部命中（{match_type}）"
    elif repeated and matched:
        reason = f"重复调用：{', '.join(repeated)}"
    else:
        reason = _miss_reason(expected_names, actual_names)
    return {"score": 1.0 if matched else 0.0, "pass": passed, "reason": reason, "metrics": metrics}


def _miss_reason(expected: Sequence[str], actual: Sequence[str]) -> str:
    missing = [name for name in expected if name not in actual]
    if missing:
        return f"未调用期望工具：{', '.join(missing)}"
    return "工具顺序与期望不一致"


__all__ = [
    "ANY_VALUE",
    "MATCH_MODES",
    "SQL_TOOL_PREFIX",
    "normalize_call",
    "normalize_pair",
    "match_names",
    "duplicates",
    "score_case",
]
