"""21 条回归用例：全部围绕「参数错了但没报错、查询范围被静默放大」。

分三组：
- 契约（9）：脏参数必须被拦，合法参数必须原样放行且不执行被装饰函数；
- 时间/字段（6）：分钟窗口、多小时展开、单日折叠、字段别名；
- SQL/摘要/MIME（6）：空 IN、转义、分钟分区条件、表格摘要、截断、图片魔数。
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from toolcore import (
    ToolResult,
    collapse_single_day_date,
    detail_query_contract,
    event_time_bounds,
    extend_hour_by_range,
    mime_by_url,
    minute_partition_condition,
    normalize_detail_requested_fields,
    normalize_hour,
    sniff_image_mime,
    sql_in_clause,
    sql_in_params,
    sql_quote,
    strip_tables_for_model,
    summarize_text,
    validate_detail_args,
)

# ---------------------------------------------------------------- 契约（9）


def test_numeric_ids_reject_names_and_accept_pandas_suffix():
    errors = validate_detail_args({"app_id": "示例媒体-安卓", "creative_md5": "abc"})
    assert any("只能是数字 id" in message for message in errors)

    # pandas 导出的 "9044.0" 是历史兼容写法，不能误拦
    assert validate_detail_args({"app_id": "9044.0,9045", "creative_md5": "abc"}) == []


def test_bad_hour_rejected_but_zero_is_valid():
    errors = validate_detail_args({"bns_hour": "3点-5点", "creative_md5": "abc"})
    assert any("无法解析为小时" in message for message in errors)

    # 0 点是合法取值，不能被当成空值
    assert validate_detail_args({"bns_hour": 0, "creative_md5": "abc"}) == []


def test_invalid_calendar_date_rejected():
    errors = validate_detail_args({"bns_date_start": "20261345", "creative_md5": "abc"})
    assert any("无法识别" in message for message in errors)


def test_invalid_source_rejected():
    errors = validate_detail_args({"source": "6", "creative_md5": "abc"})
    assert any("source='6'" in message for message in errors)


def test_unknown_requested_fields_rejected():
    errors = validate_detail_args({"requested_fields": "foo,bar", "creative_md5": "abc"})
    assert any("没有可识别的字段" in message for message in errors)


def test_no_filter_rejected():
    errors = validate_detail_args({})
    assert any("至少需要提供一个筛选条件" in message for message in errors)


def test_valid_args_pass_and_values_untouched():
    args = {
        "creative_md5": "abc",
        "app_id": "9044",
        "bns_hour": "08,09",
        "source": "4.0",
        "requested_fields": "dspid",
    }
    snapshot = dict(args)
    assert validate_detail_args(args) == []
    assert args == snapshot  # 只校验，不写回归一化值


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


# ------------------------------------------------------------ 时间/字段（6）


def test_event_time_bounds_minute_window():
    _, start, end = event_time_bounds("2026-11-10 12:00")
    assert start == datetime(2026, 11, 10, 12, 0, 0)
    assert end == datetime(2026, 11, 10, 12, 0, 59)


def test_normalize_hour_multi_and_zero():
    assert normalize_hour("8点、9点") == "08,09"
    assert normalize_hour(0) == 0


def test_extend_hour_by_range():
    start = datetime(2026, 11, 10, 9, 0)
    end = datetime(2026, 11, 10, 11, 0)
    assert extend_hour_by_range("9", start, end) == "09,10,11"
    assert extend_hour_by_range("08,09", start, end) == "08,09"


def test_collapse_single_day_date():
    args = {"bns_date": "20261110"}
    collapse_single_day_date(args)
    assert args == {"bns_date_start": "20261110", "bns_date_end": "20261110"}

    explicit = {"bns_date": "20260101", "bns_date_start": "20260102"}
    collapse_single_day_date(explicit)
    assert explicit == {"bns_date_start": "20260102"}


def test_requested_fields_aliases_and_ts():
    fields = normalize_detail_requested_fields("落地页,dspid,不存在")
    assert fields == ["landing", "dsp_id", "ts"]


def test_normalize_query_date_formats():
    from toolcore import normalize_query_date

    for text in ("20261110", "2026-11-10", "2026/11/10", "2026.11.10", "2026年11月10日"):
        assert normalize_query_date(text) == "20261110"


# ------------------------------------------------------- SQL/摘要/MIME（5）


def test_sql_in_params_empty():
    assert sql_in_params([]) == ("NULL", [])


def test_sql_quote_escapes_quote_and_backslash():
    assert sql_quote("a'b") == "a\\'b"
    assert sql_quote("c\\d") == "c\\\\d"
    assert sql_in_clause(["a'b", "c"]) == "a\\'b','c"


def test_minute_partition_condition():
    start = datetime(2026, 11, 10, 10, 5)
    end = datetime(2026, 11, 10, 11, 7)
    condition = minute_partition_condition(start, end)
    assert ">= ('20261110', '10', '05')" in condition
    assert "<= ('20261110', '11', '07')" in condition
    assert minute_partition_condition(end, start) == "1 = 0"


def test_strip_tables_for_model():
    html = (
        "<p>结果</p><table><thead><tr><th>a</th><th>b</th></tr></thead>"
        "<tbody><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></tbody></table>"
    )
    output = strip_tables_for_model(html)
    assert "表格约 2 行" in output
    assert "列：a、b" in output
    assert "<table" not in output


def test_summarize_truncates_plain_text():
    text = "x" * 2000
    summary = summarize_text(text)
    assert len(summary) == 1501
    assert summary.endswith("…")
    assert ToolResult.from_text(text).message == summary


def test_sniff_and_mime_by_url():
    assert sniff_image_mime(b"\x89PNG\r\n\x1a\n" + b"0" * 8) == "image/png"
    assert sniff_image_mime(b"\xff\xd8\xff\xe0" + b"0" * 8) == "image/jpeg"
    assert sniff_image_mime(b"hello world") == ""
    assert mime_by_url("https://cdn.example.com/a/b.JPG?k=1") == "image/jpeg"
    assert mime_by_url("https://cdn.example.com/a/b") == ""
