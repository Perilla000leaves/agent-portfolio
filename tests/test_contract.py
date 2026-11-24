"""契约层用例：脏参数必须在进 SQL 之前被拦，且失败要回可自纠的错误。

这一组测的不是「校验器写得对不对」，而是三条线上事故：

- 小时解析失败被降级成「不过滤小时」→ 扫全天分区；
- 未知字段被静默忽略 → 回退成全字段大查询；
- 失败抛异常 → 框架把它 re-raise 成整轮 error，模型拿不到可改的提示。
"""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from toolcore import preview_state_key
from toolcore.contract import (
    CONTRACT_ERROR_PREFIX,
    DetailQueryParams,
    contract_error_payload,
    detail_query_contract,
)


def _validate(**kwargs) -> DetailQueryParams:
    return DetailQueryParams.model_validate(kwargs)


def _error_text(exc: ValidationError) -> str:
    return " ".join(str(item.get("msg") or "") for item in exc.errors())


def test_numeric_ids_reject_names_and_accept_pandas_suffix():
    with pytest.raises(ValidationError) as box:
        _validate(app_id="示例媒体-安卓", creative_md5="abc")
    assert "只能是数字 id" in _error_text(box.value)

    # pandas 导出的 "9044.0" 是历史兼容写法，不能误拦
    assert _validate(app_id="9044.0,9045", creative_md5="abc").app_id == "9044.0,9045"


def test_bad_hour_rejected_but_zero_is_valid():
    with pytest.raises(ValidationError) as box:
        _validate(bns_hour="3点-5点", creative_md5="abc")
    assert "扫全天分区" in _error_text(box.value)

    # 0 点是合法取值，不能被当成空值
    assert _validate(bns_hour=0, creative_md5="abc").bns_hour == 0


def test_invalid_calendar_date_rejected():
    with pytest.raises(ValidationError) as box:
        _validate(bns_date_start="20261345", creative_md5="abc")
    assert "无法识别" in _error_text(box.value)


def test_invalid_source_rejected():
    with pytest.raises(ValidationError) as box:
        _validate(source="6", creative_md5="abc")
    assert "source='6'" in _error_text(box.value)


def test_invalid_stage_rejected():
    with pytest.raises(ValidationError) as box:
        _validate(stage="曝光", creative_md5="abc")
    assert "stage='曝光'" in _error_text(box.value)
    # 生产里 fill / click 两个工具已合并成一个 stage 参数
    assert _validate(stage="click", creative_md5="abc").stage == "click"


def test_unknown_requested_fields_rejected():
    with pytest.raises(ValidationError) as box:
        _validate(requested_fields="foo,bar", creative_md5="abc")
    assert "没有可识别的字段" in _error_text(box.value)


def test_no_filter_rejected():
    with pytest.raises(ValidationError) as box:
        _validate()
    assert "至少需要提供一个筛选条件" in _error_text(box.value)


def test_valid_args_pass_and_values_untouched():
    args = {
        "creative_md5": "abc",
        "app_id": "9044",
        "bns_hour": "08,09",
        "source": "4.0",
        "requested_fields": "dspid",
    }
    params = _validate(**args)
    # 只校验，不写回归一化值：入参原样保留
    for key, value in args.items():
        assert getattr(params, key) == value


def test_contract_decorator_missing_required_returns_error():
    @detail_query_contract
    async def fake(stage: str, creative_md5: str | None = None):
        return {"ok": True}

    payload = asyncio.run(fake(creative_md5="abc"))
    assert payload["status"] == "error"
    assert "缺少必填参数" in payload["summary"]


def test_contract_decorator_invalid_arg_blocks_execution():
    calls: list[dict] = []

    @detail_query_contract
    async def fake(stage: str = "点击", creative_md5=None, app_id=None):
        calls.append({"stage": stage, "app_id": app_id})
        return {"ok": True}

    payload = asyncio.run(fake(app_id="示例媒体-安卓", creative_md5="abc"))
    assert payload["status"] == "error"
    assert "只能是数字 id" in payload["summary"]
    assert calls == []  # 校验失败绝不能执行到函数体


def test_contract_error_payload_stashes_preview_not_in_model_summary():
    """失败的预览只给前端：summary 里只有提示，完整 HTML 进 state。"""
    state: dict[str, str] = {}
    payload = contract_error_payload("示例错误", tool_name="query_demo", state=state)

    assert payload["status"] == "error"
    assert payload["summary"].startswith("status: error")
    assert CONTRACT_ERROR_PREFIX in payload["summary"]
    assert preview_state_key("query_demo") in state
