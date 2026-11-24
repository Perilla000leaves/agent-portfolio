"""contextcore —— 长输入进模型前的「占位符 + 结果投递」策略（与框架无关）。

两件事都属于上下文工程，而且都有同一个目标：**别让模型的上下文被原始数据撑爆，
也别让模型有机会自己拼装 ID。**

## 1. 占位符：整组标记，不是序号

长 URL 直接进上下文有两个问题：占窗口，而且模型会截断或重写。做法是先把它们替换成标记，
工具执行前再还原。

早期实现给每个 URL 分配一个序号（`[__URL__0]`、`[__URL__1]`），代价是一整套序号状态机：
跨轮续接、同值复用、超限裁剪、按序号反查、还要处理模型的"第 N 个"指代。现在改成**整组一个标记**
（URL 用 `[__URL_LIST__]`，创意 ID 用 `[__CREATIVE_LIST__]`），映射里只有一条
「标记 → 逗号拼接的一组原值」。省掉整套状态机，代价是模型不能再指代"上一轮第 N 个"，
且新一轮出现新对象时整组被覆盖——这个取舍在 `index_urls` 的文档里写明。

还原侧必须**宽容**：模型会把标记写成变体（去方括号、改大小写、多写空格），
变体一旦穿进工具参数会静默查空（SQL/ES 都当普通字符串匹配），比报错更难排查。

## 2. 结果投递：一律走对象存储，小结果才附表格

查询结果不再按大小二选一：**所有结果都写 OSS 返链接**，行数 ≤ 20 时额外附一份 HTML
表格给前端渲染。模型上下文里永远只有「行数 + 列名 + 链接」——表格行不进模型，
既不撑爆窗口，模型也没法复述行数据。
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

#: 整组标记（同一轮所有 URL / 创意指向同一组值）
URL_MARKER = "[__URL_LIST__]"
CREATIVE_MARKER = "[__CREATIVE_LIST__]"

#: 还原侧宽容匹配：方括号可有可无、大小写不敏感、允许中间空格与多余下划线
URL_MARKER_RE = re.compile(r"\[?\s*__URL_?LIST_*\s*\]?", re.IGNORECASE)
CREATIVE_MARKER_RE = re.compile(r"\[?\s*__CREATIVE_?LIST_*\s*\]?", re.IGNORECASE)

#: 残留检测只看前缀：老会话里的 `[__URL__3]`、模型臆造的 `[__URL__9]` 都要拦住
URL_LEFTOVER_RE = re.compile(r"__URL", re.IGNORECASE)
CREATIVE_LEFTOVER_RE = re.compile(r"__CREATIVE", re.IGNORECASE)

#: URL 里的 query string 可能自带逗号（图片处理参数），不能当分隔符
URL_PATTERN = (
    r'[a-zA-Z][a-zA-Z0-9+\-.]*://[^\s,，\u4e00-\u9fff\)\]}\'"?]+'
    r'(?:\?(?:[^\s,，\u4e00-\u9fff\)\]}\'"]+|,(?![a-zA-Z][a-zA-Z0-9+\-.]*://))*)?'
)

#: 创意 ID：`image-21-xxx` / `native-255-xxx`，且紧贴中文时也要能匹配
#: （不能用 `\b`：Python 的 `\b` 是 Unicode 语义，中文属 `\w`，紧贴中文时词边界不成立）
CREATIVE_PATTERN = (
    r"(?:(?<![A-Za-z0-9_])(?:image|native|video)-\d+|(?<![A-Za-z0-9_-])-\d+)"
    r"-[A-Za-z0-9][A-Za-z0-9_-]{8,}(?![A-Za-z0-9_])"
)

#: 需要还原的参数表：不在表里的工具/参数一律不动（避免把普通字符串改掉）
URL_PARAM_TOOLS: dict[str, tuple[str, ...]] = {
    "to_material_md5": ("url",),
    "similar_by_image": ("url",),
    "identify_by_logo": ("url", "candidate_result"),
    "identify_by_ocr": ("url",),
    "exposure_detail": ("landing", "dplink"),
    "landing_to_creative": ("landing_dp",),
}

CREATIVE_PARAM_TOOLS: dict[str, tuple[str, ...]] = {
    "creative_get_tag": ("creative_md5",),
    "creative_get_url": ("creative_md5",),
    "creative_to_material": ("creative_md5",),
    "exposure_metrics": ("creative_md5",),
    "exposure_detail": ("creative_md5", "origin_creative_md5"),
    "stage_detail": ("creative_md5", "origin_creative_md5"),
}

#: 小结果才附 HTML 表格给前端渲染；超过这个行数只给链接
PREVIEW_ROW_LIMIT = 20


def dedupe(values: Sequence[str]) -> list[str]:
    """去重保序：同一组里重复出现的值只保留第一次。"""
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)


def _collapse_markers(text: str, marker: str) -> str:
    """连续或逗号分隔的多个同一标记压成一个（原句里并排贴多个 URL 时会出现）。"""
    pattern = re.escape(marker)
    return re.sub(rf"{pattern}(?:[\s,，;；、]+{pattern})+", marker, text)


def _replace_matches(text: str, matches: Sequence[Any], marker: str) -> str:
    """从后往前替换，避免位置偏移。"""
    for match in reversed(list(matches)):
        text = text[: match.start()] + marker + text[match.end() :]
    return text


def index_urls(text: str) -> tuple[str, list[str]]:
    """文本里的 URL → `[__URL_LIST__]`，返回 (新文本, 本轮命中的 URL 去重保序列表)。

    只索引「可能是广告素材」的链接：对象存储的通用文件链接不算，图片类后缀例外
    （没命中的链接保持原文，不做隐式替换）。
    """
    matches = [
        match
        for match in re.finditer(URL_PATTERN, text)
        if ("oss-cn" not in match.group() and "aliyuncs.com" not in match.group())
        or re.search(r"\.(png|jpg|jpeg|gif|webp|bmp|svg)(\?|$)", match.group(), re.IGNORECASE)
    ]
    if not matches:
        return text, []
    urls = dedupe([match.group() for match in matches])
    return _collapse_markers(_replace_matches(text, matches, URL_MARKER), URL_MARKER), urls


def index_creatives(text: str) -> tuple[str, list[str]]:
    """文本里的创意 ID → `[__CREATIVE_LIST__]`，返回 (新文本, 本轮命中的创意去重保序列表)。"""
    matches = list(re.finditer(CREATIVE_PATTERN, text))
    if not matches:
        return text, []
    creatives = dedupe([match.group() for match in matches])
    return _collapse_markers(_replace_matches(text, matches, CREATIVE_MARKER), CREATIVE_MARKER), creatives


def group_mapping(marker: str, values: Sequence[str]) -> dict[str, str]:
    """整组映射：`{标记: "v1,v2"}`；没有命中就返回空映射（调用方据此清掉旧组）。"""
    joined = ",".join(dedupe(values))
    return {marker: joined} if joined else {}


def restore_urls(text: str, url_mapping: dict[str, str] | None) -> str:
    """`[__URL_LIST__]`（含变体）→ 本轮整组 URL；映射里没有就原样返回。"""
    value = str((url_mapping or {}).get(URL_MARKER) or "")
    if not text or not value:
        return text
    return URL_MARKER_RE.sub(lambda _match: value, text)


def restore_creatives(text: str, creative_mapping: dict[str, str] | None) -> str:
    """`[__CREATIVE_LIST__]`（含变体）→ 本轮整组创意 ID；映射里没有就原样返回。"""
    value = str((creative_mapping or {}).get(CREATIVE_MARKER) or "")
    if not text or not value:
        return text
    return CREATIVE_MARKER_RE.sub(lambda _match: value, text)


def has_url_marker(value: Any) -> bool:
    """参数里是否残留 URL 占位符（含老序号形态与各种变体）。"""
    if value is None:
        return False
    if isinstance(value, (list, tuple, set)):
        return any(has_url_marker(item) for item in value)
    return bool(URL_LEFTOVER_RE.search(str(value)))


def has_creative_marker(value: Any) -> bool:
    """参数里是否残留创意占位符（含老序号形态与各种变体）。"""
    if value is None:
        return False
    if isinstance(value, (list, tuple, set)):
        return any(has_creative_marker(item) for item in value)
    return bool(CREATIVE_LEFTOVER_RE.search(str(value)))


def _restore_value(value: Any, mapping: dict[str, str] | None, restore) -> Any:
    """字符串逐项还原；列表只还原其中的字符串项；其它类型原样返回。"""
    if isinstance(value, list):
        return [restore(item, mapping) if isinstance(item, str) else item for item in value]
    if isinstance(value, str):
        return restore(value, mapping)
    return value


def restore_args(
    tool_name: str,
    args: dict[str, Any],
    url_mapping: dict[str, str] | None = None,
    creative_mapping: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """工具执行前的还原：**只还原该工具白名单里的参数**。

    返回 `(新参数, 未还原残留)`。残留非空表示映射缺失或标记写坏——
    宁可拦下让模型重试，也不要让占位符进 SQL 静默查空。
    """
    restored = dict(args)
    leftover: dict[str, Any] = {}

    for name in URL_PARAM_TOOLS.get(tool_name, ()):
        if name not in restored:
            continue
        restored[name] = _restore_value(restored[name], url_mapping, restore_urls)
        if has_url_marker(restored.get(name)):
            leftover[name] = restored.get(name)

    for name in CREATIVE_PARAM_TOOLS.get(tool_name, ()):
        if name not in restored:
            continue
        restored[name] = _restore_value(restored[name], creative_mapping, restore_creatives)

    return restored, leftover


def deliver_policy(row_count: int, *, preview_row_limit: int = PREVIEW_ROW_LIMIT) -> dict[str, Any]:
    """结果投递策略：一律写对象存储；只有小结果才额外附 HTML 表格给前端。

    模型侧永远只拿「行数 + 列名 + 链接」，所以这里不需要"大结果/小结果"两条分支。
    """
    rows = max(int(row_count), 0)
    return {
        "oss_link": True,
        "inline_html": rows <= preview_row_limit,
        "rows": rows,
    }


__all__ = [
    "URL_MARKER",
    "CREATIVE_MARKER",
    "URL_PARAM_TOOLS",
    "CREATIVE_PARAM_TOOLS",
    "PREVIEW_ROW_LIMIT",
    "dedupe",
    "index_urls",
    "index_creatives",
    "group_mapping",
    "restore_urls",
    "restore_creatives",
    "has_url_marker",
    "has_creative_marker",
    "restore_args",
    "deliver_policy",
]
