"""写操作审批：快照存储模型侧形态，重放时才还原。

## 两条设计约束

**① 快照存的是「模型侧参数」，不是最终参数。**
审批要跨进程、跨轮次等人，快照得能落盘。如果把还原后的真实 URL 存进去，
等于把用户原值写进持久化状态（可能进日志、进对象存储）——所以快照保留占位符，
真正执行前再用当轮映射还原。代价是重放时必须能拿到映射，拿不到就只能拒绝执行。

**② 决策与执行必须幂等。**
审批是人工触发的，重复点击、前端重试、消息重投都会让同一个请求被批两次。
所以「已批准」「已执行」都是终态，重复操作不改状态、不重复执行——否则写操作会执行多次。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

import contextcore as C


class ApprovalStatus(StrEnum):
    """审批状态。`EXECUTED` 是批准后的终态，用来阻止重复执行。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"


@dataclass
class ApprovalRequest:
    """一笔待审批的写操作。"""

    request_id: str
    tool_name: str
    payload_snapshot: dict[str, Any]
    status: ApprovalStatus = ApprovalStatus.PENDING
    decisions: list[str] = field(default_factory=list)


def _fingerprint(tool_name: str, args: dict[str, Any]) -> str:
    """由工具名 + 参数生成稳定 id：同一请求重复提交得到同一个 id。"""
    raw = json.dumps({"tool": tool_name, "args": args}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def request_approval(
    tool_name: str,
    args: dict[str, Any],
    *,
    request_id: str | None = None,
) -> ApprovalRequest:
    """发起审批。快照原样保存（含占位符），不做还原。"""
    identifier = request_id or _fingerprint(tool_name, args)
    return ApprovalRequest(
        request_id=identifier,
        tool_name=tool_name,
        payload_snapshot=dict(args),
        decisions=[f"created:{identifier}"],
    )


def decide(request: ApprovalRequest, *, approved: bool, actor: str = "user") -> ApprovalRequest:
    """人工决策。已决（批准/拒绝/已执行）后重复调用不改变状态。"""
    if request.status is not ApprovalStatus.PENDING:
        return replace(
            request,
            decisions=[*request.decisions, f"ignored:{actor}:{request.status.value}"],
        )
    status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
    return replace(
        request,
        status=status,
        decisions=[*request.decisions, f"{status.value}:{actor}"],
    )


def replay_payload(
    request: ApprovalRequest,
    *,
    url_mapping: dict[str, str] | None = None,
    creative_mapping: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """批准后重放：把快照里的占位符还原成真实值。

    返回 `(可执行参数, 错误文案)`；第一个值为 None 表示**不得执行**。
    """
    if request.status is ApprovalStatus.PENDING:
        return None, "尚未审批，不能执行"
    if request.status is ApprovalStatus.REJECTED:
        return None, "已被拒绝，不能执行"
    if request.status is ApprovalStatus.EXECUTED:
        return None, "已执行过，重复重放被忽略"

    restored, leftover = C.restore_args(
        request.tool_name,
        dict(request.payload_snapshot),
        url_mapping=url_mapping or {},
        creative_mapping=creative_mapping or {},
    )
    if leftover:
        names = ", ".join(sorted(leftover))
        return None, f"快照里的占位符无法还原（{names}），缺少当轮映射，请让用户重新发起"
    return restored, ""


def mark_executed(request: ApprovalRequest) -> ApprovalRequest:
    """执行完成后置终态，防止同一审批被重放多次。"""
    return replace(request, status=ApprovalStatus.EXECUTED, decisions=[*request.decisions, "executed"])


def can_execute(request: ApprovalRequest) -> bool:
    """当前状态是否允许执行（不看映射是否可用）。"""
    return request.status is ApprovalStatus.APPROVED


__all__ = [
    "ApprovalRequest",
    "ApprovalStatus",
    "can_execute",
    "decide",
    "mark_executed",
    "replay_payload",
    "request_approval",
]
