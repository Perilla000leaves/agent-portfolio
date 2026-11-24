"""回调链用例：三段接力的顺序、占位符只改本轮、工具前还原与错位拦截。"""
from __future__ import annotations

import contextcore as C
from orchestrator.hooks import LlmRequest, Message, before_tool, check_id_only_params, run_before_model


def _request(*texts: str) -> LlmRequest:
    return LlmRequest(messages=[Message(role="user", text=text) for text in texts])


def test_hooks_run_in_fixed_order():
    request = run_before_model(_request("查一下工单"))
    assert request.trace[0] == "sanitize_context"
    assert request.trace[-1] == "inject_hints"
    assert any(entry.startswith("index_placeholders") for entry in request.trace)


def test_table_in_message_is_summarized():
    html = (
        "<p>结果</p><table><thead><tr><th>a</th><th>b</th></tr></thead>"
        "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
    )
    request = run_before_model(_request(html))
    text = request.messages[0].text

    assert "<table" not in text
    assert "表格约 1 行" in text


def test_only_latest_user_message_gets_placeholders():
    """历史消息保持原文：重复索引会把映射覆盖成更小的一组。"""
    old = "https://old.example.com/a.png"
    new = "https://new.example.com/b.jpg"
    request = run_before_model(_request(old, new))

    assert request.messages[0].text == old
    assert C.URL_MARKER in request.messages[1].text
    assert new not in request.messages[1].text


def test_state_keeps_group_mapping():
    state: dict = {}
    request = _request("https://a.example.com/1.png 和 https://b.example.com/2.jpg")
    run_before_model(request, state)

    assert state["url_mapping"] == {
        C.URL_MARKER: "https://a.example.com/1.png,https://b.example.com/2.jpg"
    }


def test_pending_approval_injects_hint():
    state = {"pending_approval": True}
    request = run_before_model(_request("继续"), state)
    assert any("等审批" in hint for hint in request.hints)


def test_before_tool_restores_whitelisted_param():
    state = {"url_mapping": {C.URL_MARKER: "https://cdn.example.com/a.png"}}
    restored, error = before_tool("exposure_detail", {"landing": C.URL_MARKER}, state)

    assert error == ""
    assert restored["landing"] == "https://cdn.example.com/a.png"


def test_before_tool_blocks_when_mapping_missing():
    """映射缺失时宁可拦下，也不要让占位符进 SQL 静默查空。"""
    restored, error = before_tool("exposure_detail", {"landing": C.URL_MARKER}, {})
    assert restored["landing"] == C.URL_MARKER
    assert "参数还原失败" in error


def test_check_id_only_params_rejects_url():
    error = check_id_only_params("creative_get_tag", {"creative_md5": "https://cdn.example.com/a.png"})
    assert "参数错位" in error
    assert "只接受业务 ID" in error


def test_check_id_only_params_rejects_unrestored_marker():
    error = check_id_only_params("creative_get_tag", {"creative_md5": C.URL_MARKER})
    assert "参数还原失败" in error


def test_check_id_only_params_accepts_normal_value():
    assert check_id_only_params("creative_get_tag", {"creative_md5": "native-260-abc123456789"}) == ""
