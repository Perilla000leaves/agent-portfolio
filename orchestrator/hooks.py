"""回调链：模型看到之前的三段接力，以及工具执行前的还原与守卫。

## 为什么拆三段而不是一个大回调

模型请求进来时要干三件互不相干的事：

1. **清理上下文**：把工具结果里的 HTML 表格缩成「N 行 + 列名」，记 trace，取长任务进度 key；
2. **占位符化**：把本轮用户消息里的长 URL / 业务 ID 换成整组标记（见 `contextcore`）；
3. **注入提示**：按状态追加系统提示（等审批、有上传文件、需要追问）。

拆开的好处是**顺序可读、可单独测**；合成一个函数则只能整体断言。三段都返回 `None`
（不短路），因为任一段失败都不应该让这一次请求直接失败——这和"工具失败要回可读错误"
是同一个取舍。

## 工具执行前：还原 + 守卫

模型可能把占位符当 URL 传进来、也可能把 URL 传到只接受业务 ID 的参数上。
前者会让查询**静默查空**（占位符被当普通字符串），后者更糟：SQL 里出现一个 URL，
跑一遍大表作业再返回空。所以这里两个顺序做完：先还原，再检查是否还有残留/错位。
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import contextcore as C
from toolcore import strip_tables_for_model

#: 只接受业务 ID 的参数：这些参数收到 URL 或未还原的占位符必然是错位
ID_ONLY_PARAMS: Mapping[str, tuple[str, ...]] = {
    "creative_get_tag": ("creative_md5",),
    "creative_get_url": ("creative_md5",),
    "creative_to_material": ("creative_md5",),
    "exposure_metrics": ("creative_md5",),
    "exposure_detail": ("creative_md5", "origin_creative_md5"),
    "stage_detail": ("creative_md5", "origin_creative_md5"),
}

_URL_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


@dataclass
class Message:
    """一条模型消息。`raw` 是真值，`text` 是给模型看的形态（可能被占位符化）。"""

    role: str
    text: str


@dataclass
class LlmRequest:
    """一次模型请求。三段回调就地修改它，返回值恒为 None。"""

    messages: list[Message] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)

    def last_user_message(self) -> Message | None:
        for message in reversed(self.messages):
            if message.role == "user":
                return message
        return None


# --------------------------------------------------------------------------- #
# before_model：三段接力
# --------------------------------------------------------------------------- #


def sanitize_context(request: LlmRequest, state: dict) -> None:
    """第一段：缩表格、记 trace、取长任务进度 key。"""
    for message in request.messages:
        if "<table" in message.text.lower():
            message.text = strip_tables_for_model(message.text)
    progress_key = state.get("progress_key")
    if progress_key:
        request.trace.append(f"progress_key={progress_key}")
    request.trace.append("sanitize_context")


def index_placeholders(request: LlmRequest, state: dict) -> None:
    """第二段：只处理**本轮最新一条用户消息**，历史消息保持原文。

    只改最新一条是有意的：历史里的 URL 已经在上一次索引时进过映射，
    重复索引会把映射覆盖成更小的一组，模型反而拼错。
    """
    message = request.last_user_message()
    if message is None:
        return
    text, urls = C.index_urls(message.text)
    text, creatives = C.index_creatives(text)
    message.text = text
    if urls:
        state["url_mapping"] = C.group_mapping(C.URL_MARKER, urls)
    if creatives:
        state["creative_mapping"] = C.group_mapping(C.CREATIVE_MARKER, creatives)
    request.trace.append(f"index_placeholders(urls={len(urls)}, creatives={len(creatives)})")


def inject_hints(request: LlmRequest, state: dict) -> None:
    """第三段：按状态追加提示。顺序固定，便于提示词侧对齐。"""
    if state.get("pending_approval"):
        request.hints.append("有一笔写操作在等审批：不要重复提交，先告知用户等待结果。")
    if state.get("uploaded_files"):
        request.hints.append("本轮带了上传文件，已解析成文本注入，不要向用户索要内容。")
    if not request.hints:
        request.hints.append("无额外提示。")
    request.trace.append("inject_hints")


#: 固定顺序：清理 → 占位符 → 提示（提示要看得到前两段的结果）
BEFORE_MODEL_HOOKS: tuple[Callable[[LlmRequest, dict], None], ...] = (
    sanitize_context,
    index_placeholders,
    inject_hints,
)


def run_before_model(
    request: LlmRequest,
    state: dict | None = None,
    hooks: Sequence[Callable[[LlmRequest, dict], None]] = BEFORE_MODEL_HOOKS,
) -> LlmRequest:
    """按序接力执行；每段返回 None，不短路。"""
    session_state = state if state is not None else {}
    for hook in hooks:
        hook(request, session_state)
    return request


# --------------------------------------------------------------------------- #
# before_tool：还原 + 守卫
# --------------------------------------------------------------------------- #


def check_id_only_params(tool_name: str, args: Mapping[str, object]) -> str:
    """业务 ID 参数收到 URL 或未还原的占位符 → 返回错误文案（空串表示没问题）。"""
    for name in ID_ONLY_PARAMS.get(tool_name, ()):
        value = args.get(name)
        if value is None:
            continue
        flattened = value if isinstance(value, (list, tuple)) else [value]
        for item in flattened:
            text = str(item)
            if C.has_url_marker(text):
                return (
                    f"[参数还原失败] {tool_name} 的 {name} 仍是未还原的占位符（{text[:40]}）。"
                    "占位符必须作为字符串值传入，系统会在工具执行前还原；"
                    "若这里仍是标记，说明本轮映射缺失，请让用户重新提供或直接传真实 URL。"
                )
            if _URL_SCHEME_RE.match(text):
                return (
                    f"[参数错位] {tool_name} 的 {name} 只接受业务 ID，收到的是 URL（{text[:40]}）。"
                    "请先把它换成 ID 再调用；直接把 URL 传进来会在 SQL 里当字符串匹配，必然查空。"
                )
    return ""


def before_tool(
    tool_name: str,
    args: Mapping[str, object],
    state: Mapping[str, object] | None = None,
) -> tuple[dict[str, object], str]:
    """工具执行前：先按白名单还原占位符，再查错位。

    返回 `(还原后的参数, 错误文案)`；错误文案非空时调用方**不得执行**该工具。
    """
    session = state or {}
    restored, leftover = C.restore_args(
        tool_name,
        dict(args),
        url_mapping=session.get("url_mapping") or {},
        creative_mapping=session.get("creative_mapping") or {},
    )
    if leftover:
        names = ", ".join(f"{name}={str(value)[:40]}" for name, value in leftover.items())
        return restored, f"[参数还原失败] 已阻止 {tool_name} 执行：{names} 仍是未还原的 URL 占位符。"

    error = check_id_only_params(tool_name, restored)
    return restored, error


__all__ = [
    "BEFORE_MODEL_HOOKS",
    "ID_ONLY_PARAMS",
    "LlmRequest",
    "Message",
    "before_tool",
    "check_id_only_params",
    "index_placeholders",
    "inject_hints",
    "run_before_model",
    "sanitize_context",
]
