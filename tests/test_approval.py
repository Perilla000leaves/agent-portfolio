"""审批用例：快照保留模型侧形态、决策幂等、重放才还原、执行后不可重放。"""
from __future__ import annotations

import contextcore as C
from orchestrator.approval import (
    ApprovalStatus,
    can_execute,
    decide,
    mark_executed,
    replay_payload,
    request_approval,
)


def _approved_request():
    request = request_approval("exposure_detail", {"landing": C.URL_MARKER})
    return decide(request, approved=True)


def test_snapshot_keeps_placeholder_not_real_value():
    """快照要能落盘等人审批，所以存模型侧形态；真实值等重放时才还原。"""
    request = request_approval("exposure_detail", {"landing": C.URL_MARKER})

    assert request.status is ApprovalStatus.PENDING
    assert request.payload_snapshot == {"landing": C.URL_MARKER}
    assert can_execute(request) is False


def test_replay_pending_is_rejected():
    request = request_approval("exposure_detail", {"landing": C.URL_MARKER})
    payload, reason = replay_payload(request, url_mapping={C.URL_MARKER: "https://x/a.png"})

    assert payload is None
    assert "尚未审批" in reason


def test_replay_after_approval_restores_placeholder():
    request = _approved_request()
    payload, reason = replay_payload(request, url_mapping={C.URL_MARKER: "https://x/a.png"})

    assert reason == ""
    assert payload == {"landing": "https://x/a.png"}


def test_replay_without_mapping_is_blocked():
    """跨进程重放拿不到当轮映射时，只能拒绝执行，不能把占位符当参数传下去。"""
    request = _approved_request()
    payload, reason = replay_payload(request, url_mapping={})

    assert payload is None
    assert "无法还原" in reason


def test_double_approval_is_idempotent():
    """审批是人工触发的，重复点击不能让同一个请求被批两次。"""
    request = _approved_request()
    again = decide(request, approved=True, actor="retry")

    assert again.status is ApprovalStatus.APPROVED
    assert any(item.startswith("ignored:") for item in again.decisions)


def test_rejected_cannot_execute():
    request = decide(request_approval("exposure_detail", {"landing": C.URL_MARKER}), approved=False)
    payload, reason = replay_payload(request)

    assert payload is None
    assert "已被拒绝" in reason


def test_executed_cannot_be_replayed_again():
    """写操作执行后置终态，否则重试会把同一笔写操作执行多次。"""
    request = mark_executed(_approved_request())
    payload, reason = replay_payload(request, url_mapping={C.URL_MARKER: "https://x/a.png"})

    assert payload is None
    assert "已执行过" in reason


def test_same_args_produce_same_request_id():
    first = request_approval("exposure_detail", {"landing": C.URL_MARKER})
    second = request_approval("exposure_detail", {"landing": C.URL_MARKER})
    assert first.request_id == second.request_id
