"""轨迹判定用例：先归一化、再匹配，最后把自定义指标作为信息项附上。

重点是三类「判定器自己制造的错误」：

- 期望只写工具名，实际参数带日期/ID → 严格比较全判 0；
- 工具都调了但顺序不对，不同匹配语义给出不同结论；
- 同一工具被重复调用，工具名匹配发现不了。
"""
from __future__ import annotations

import evalcore as E

# ---------------------------------------------------------------- 归一化


def test_normalize_pair_keys_mode_ignores_value_drift():
    actual = [{"name": "query_demo", "args": '{"bns_date_start": "20261110", "limit": 10}'}]
    expected = [{"name": "query_demo", "args": {"bns_date_start": "20260101"}}]
    act, exp = E.normalize_pair(actual, expected, args_mode="keys")

    # 只比 key，值统一成占位符 → 日期漂移不影响判定
    assert act[0]["args"] == {"bns_date_start": E.ANY_VALUE}
    assert exp[0]["args"] == {"bns_date_start": E.ANY_VALUE}
    assert act[0]["args"] == exp[0]["args"]


def test_normalize_pair_full_mode_keeps_values():
    actual = [{"name": "t", "args": {"d": "20261110"}}]
    expected = [{"name": "t", "args": {"d": "20260101"}}]
    act, exp = E.normalize_pair(actual, expected, args_mode="full")
    assert act[0]["args"] != exp[0]["args"]


def test_normalize_pair_without_expected_args_skips_comparison():
    actual = [{"name": "t", "args": {"anything": 1}}]
    expected = [{"name": "t"}]
    act, exp = E.normalize_pair(actual, expected)
    assert act[0]["args"] is None
    assert exp[0]["args"] is None


def test_normalize_pair_reports_missing_key():
    actual = [{"name": "t", "args": {"a": 1}}]
    expected = [{"name": "t", "args": {"a": 1, "b": 2}}]
    act, exp = E.normalize_pair(actual, expected)
    # 期望要的 b 没传 → 实际侧少 key，比较自然不相等
    assert act[0]["args"] != exp[0]["args"]


# ---------------------------------------------------------------- 匹配语义


def test_match_exact_requires_same_order():
    assert E.match_names(["a", "b"], ["a", "b"], "exact") is True
    assert E.match_names(["b", "a"], ["a", "b"], "exact") is False
    assert E.match_names(["a", "b", "c"], ["a", "b"], "exact") is False


def test_match_in_order_allows_extra_calls():
    assert E.match_names(["x", "a", "y", "b"], ["a", "b"], "in_order") is True
    assert E.match_names(["b", "a"], ["a", "b"], "in_order") is False


def test_match_any_order_ignores_order():
    assert E.match_names(["b", "x", "a"], ["a", "b"], "any_order") is True
    assert E.match_names(["a"], ["a", "b"], "any_order") is False


def test_match_unknown_mode_falls_back_to_any_order():
    assert E.match_names(["b", "a"], ["a", "b"], "nonsense") is True


# ---------------------------------------------------------------- 打分


def test_score_case_passes_when_all_tools_hit():
    result = E.score_case(
        expected_tools=["a", "b"],
        actual_calls=[{"name": "a"}, {"name": "b"}],
    )
    assert result["pass"] is True
    assert result["score"] == 1.0


def test_score_case_fails_with_missing_tool_reason():
    result = E.score_case(
        expected_tools=["a", "b"],
        actual_calls=[{"name": "a"}],
    )
    assert result["pass"] is False
    assert "未调用期望工具" in result["reason"]
    assert "b" in result["reason"]


def test_score_case_undetermined_when_no_expected_tools():
    """没给期望工具 → 未判定（None），绝不能被算成通过。"""
    result = E.score_case(expected_tools=[], actual_calls=[{"name": "a"}])
    assert result["pass"] is None
    assert result["score"] is None


def test_score_case_fails_on_error():
    result = E.score_case(expected_tools=["a"], actual_calls=[], error="timeout")
    assert result["pass"] is False
    assert "本轮报错" in result["reason"]


def test_score_case_fails_on_duplicate_calls():
    """工具都调了，但同一工具被重复调用 → 长任务翻车的典型形态。"""
    result = E.score_case(
        expected_tools=["a"],
        actual_calls=[{"name": "a"}, {"name": "a"}],
    )
    assert result["pass"] is False
    assert "重复调用" in result["reason"]


def test_score_case_reports_sql_metric_for_query_tools():
    """SQL 类工具要人工核对 SQL：指标只做提示，不影响通过与否。"""
    result = E.score_case(
        expected_tools=["query_adx_exposure_detail"],
        actual_calls=[{"name": "query_adx_exposure_detail", "args": '{"sql": "SELECT 1"}'}],
    )
    assert result["pass"] is True
    names = {metric["name"]: metric for metric in result["metrics"]}
    assert "query_adx_exposure_detail" in names["sql_reported"]["reason"]
    assert names["sql_reported"]["pass"] is None
