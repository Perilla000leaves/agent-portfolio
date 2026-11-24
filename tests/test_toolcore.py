"""12 条回归用例：全部围绕「参数错了但没报错、查询范围被静默放大」。

分两组（契约用例已迁到 test_contract.py，由 Pydantic 校验器承担）：
- 时间/字段（6）：分钟窗口、多小时展开、单日折叠、字段别名；
- SQL/摘要/MIME（6）：空 IN、转义、分钟分区条件、表格摘要、截断、图片魔数。
"""
from __future__ import annotations

from datetime import datetime

from toolcore import (
    ToolResult,
    collapse_single_day_date,
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
)

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
