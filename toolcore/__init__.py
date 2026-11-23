"""toolcore —— 生产 agent 的纯语义内核（无 I/O、无框架依赖）。

从 `ad_agent_sample3/tools3/` 的真实实现中提取语义并精简为可独立测试的版本：
契约校验、时间/小时归一化、SQL 拼接、结果摘要、图片类型判定。

设计约束：
- 只依赖标准库（测试只依赖 pytest）；
- 所有失败都以「可读的字符串」返回，不抛业务异常（便于模型自纠）；
- 与生产实现保持语义一致，但去掉 OSS / ADK / 数据库依赖。
"""
from __future__ import annotations

import functools
import html as _html
import inspect
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

# =========================================================================
# 1. 通用：多值入参
# =========================================================================


def split_csv_values(value: Any) -> list[str]:
    """任意多值入参 → 去空白值列表（兼容英文逗号、中文逗号/顿号与 list/tuple/set）。

    只有 None / 空串 / 空容器归为空列表；0 是合法取值（bns_hour=0）。
    """
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,，、]+", value) if item.strip()]
    return [str(value).strip()] if str(value).strip() else []


def dedupe(values: Iterable[str]) -> list[str]:
    """去重保序（空值丢弃）。"""
    seen = set()
    result: list[str] = []
    for raw in values:
        value = str(raw).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def join_values(value: Any) -> str:
    """任意多值入参 → 英文逗号串；无有效值返回空串。"""
    return ",".join(split_csv_values(value))


# =========================================================================
# 2. 日期 / 时间 / 小时
# =========================================================================

_SEPARATED_DATE_RE = re.compile(r"(\d{4})[年/.-](\d{1,2})[月/.-](\d{1,2})[日号]?")
_DATETIME_FORMATS: tuple[tuple[str, bool], ...] = (
    ("%Y-%m-%d %H:%M:%S", True),
    ("%Y%m%d %H:%M:%S", True),
    ("%Y-%m-%d %H:%M", False),
    ("%Y%m%d %H:%M", False),
)
_DATE_ONLY_FORMATS = ("%Y-%m-%d", "%Y%m%d")


def normalize_datetime_text(value: str | None) -> str:
    """把时间文本中的日期部分归一化为 YYYY-MM-DD，时间部分不变。"""
    text = str(value or "").strip()

    def _replace(match: re.Match[str]) -> str:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"

    return _SEPARATED_DATE_RE.sub(_replace, text)


def parse_datetime(
    value: str | None,
    *,
    allow_date_only: bool = False,
    date_only_end: bool = False,
) -> tuple[datetime | None, bool]:
    """任意时间文本 → (datetime, 是否精确到秒)；失败返回 (None, False)。"""
    if value is None or str(value).strip() == "":
        return None, False
    text = normalize_datetime_text(value)
    for fmt, has_second in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt), has_second
        except ValueError:
            continue
    if allow_date_only:
        for fmt in _DATE_ONLY_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError:
                continue
            if date_only_end:
                parsed = parsed.replace(hour=23, minute=59, second=59)
            return parsed, False
    return None, False


def event_time_bounds(
    event_time: str | None,
    event_time_start: str | None = None,
    event_time_end: str | None = None,
) -> tuple[datetime | None, datetime | None, datetime | None]:
    """生成查询边界 (event_dt, start_dt, end_dt)；HH:MM 输入默认覆盖整分钟。"""
    start_dt, start_has_second = parse_datetime(event_time_start)
    end_dt, end_has_second = parse_datetime(event_time_end)
    if start_dt or end_dt:
        if not start_dt or not end_dt:
            return None, None, None
        if not start_has_second:
            start_dt = start_dt.replace(second=0)
        if not end_has_second:
            end_dt = end_dt.replace(second=59)
        return start_dt, start_dt, end_dt

    event_dt, has_second = parse_datetime(event_time)
    if not event_dt:
        return None, None, None
    if has_second:
        return event_dt, event_dt, event_dt
    return event_dt, event_dt.replace(second=0), event_dt.replace(second=59)


def normalize_query_date(value: str | None) -> str:
    """宽松提取日期为 YYYYMMDD；不校验合法性，提取不到返回空串。"""
    if not value:
        return ""
    text = str(value).strip()
    match = _SEPARATED_DATE_RE.search(text) or re.search(
        r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})", text
    )
    if not match:
        return ""
    return f"{match.group(1)}{int(match.group(2)):02d}{int(match.group(3)):02d}"


def parse_date(value: Any) -> date | None:
    """任意日期文本 → date；非法日历日期返回 None（日期语义的唯一校验入口）。"""
    digits = normalize_query_date(value)
    if not digits:
        return None
    try:
        return datetime.strptime(digits, "%Y%m%d").date()
    except ValueError:
        return None


def normalize_adx_partition_hours(value: str | None) -> list[str]:
    """分区小时串 → 两位小时列表；非法 token 抛 ValueError。"""
    if value is None or not str(value).strip():
        return []
    hours: list[str] = []
    for token in re.split(r"[,，、\s]+", str(value).strip()):
        if not token:
            continue
        match = re.fullmatch(r"(\d{1,2})(?::00)?(?:点)?", token)
        if not match:
            raise ValueError("bns_hour 请用数字小时，多个小时请用逗号分隔")
        normalized = f"{int(match.group(1)):02d}"
        if normalized not in hours:
            hours.append(normalized)
    return hours


def extract_hour(value: str | None) -> int | None:
    """从 'YYYYMMDD HH:MM' / 'HH:MM' 文本中提取小时；提取不到返回 None。"""
    if not value:
        return None
    text = str(value).strip()
    match = re.search(r"(?:\d{4}[-/]?\d{2}[-/]?\d{2})[ T]?(\d{1,2}):\d{2}", text)
    if not match:
        match = re.search(r"^(\d{1,2}):\d{2}", text)
    if not match:
        return None
    return int(match.group(1))


def normalize_hour(value: Any | None) -> Any | None:
    """单值 → int，多值 → 零填充逗号串；解析失败返回 None（忽略小时过滤）。"""
    if value is None or str(value).strip() == "":
        return None
    text = join_values(value) if isinstance(value, (list, tuple, set)) else str(value).strip()
    try:
        tokens = normalize_adx_partition_hours(text)
    except ValueError:
        return extract_hour(text)
    if not tokens:
        return None
    if len(tokens) == 1:
        return int(tokens[0])
    return ",".join(tokens)


def extend_hour_by_range(
    explicit_hour: Any | None,
    start_dt: datetime | None,
    end_dt: datetime | None,
) -> Any | None:
    """同天跨小时区间：显式单值落在区间内时扩展为完整小时列表，防漏数据。"""
    covered: Any | None = None
    if start_dt is not None and end_dt is not None and start_dt.date() == end_dt.date():
        hours = list(range(start_dt.hour, end_dt.hour + 1))
        covered = hours[0] if len(hours) == 1 else ",".join(f"{h:02d}" for h in hours)
    if explicit_hour is None or (isinstance(explicit_hour, str) and not explicit_hour.strip()):
        return covered
    if not isinstance(covered, str):
        return explicit_hour
    if isinstance(explicit_hour, (list, tuple, set)):
        return explicit_hour
    try:
        tokens = normalize_adx_partition_hours(str(explicit_hour).strip())
    except ValueError:
        return explicit_hour
    if len(tokens) == 1 and tokens[0] in covered.split(","):
        return covered
    return explicit_hour


def collapse_single_day_date(args: dict[str, Any]) -> None:
    """把单日 bns_date 折叠成 start=end 后移除（ADK 会裁掉签名外参数）。"""
    bns_date = args.get("bns_date")
    empty = (None, "", [], ())
    if bns_date in empty:
        return
    args.pop("bns_date", None)
    if args.get("bns_date_start") in empty and args.get("bns_date_end") in empty:
        args["bns_date_start"] = bns_date
        args["bns_date_end"] = bns_date


# =========================================================================
# 3. 明细字段与 source
# =========================================================================

_DETAIL_OUTPUT_FIELDS = [
    "creative_md5", "app_id", "dsp_id", "landing", "dplink",
    "bns_date", "ts", "make", "model", "adx_place_id",
]
_DETAIL_OPTIONAL_FIELDS = ["origin_creative_md5", "oaid", "idfa", "ip", "os", "osv"]
_DETAIL_FILTER_FIELDS = (
    "creative_md5", "origin_creative_md5", "landing", "dplink", "oaid",
    "app_id", "place_id", "dsp_id", "make", "model", "os", "osv", "idfa", "ip",
)
_DETAIL_OUTPUT_FIELD_ALIASES = {
    "creative": "creative_md5", "creative_id": "creative_md5", "cr_id": "creative_md5",
    "final_cr_id": "creative_md5", "素材id": "creative_md5", "素材md5": "creative_md5",
    "创意md5": "creative_md5", "origin_creative_md5": "origin_creative_md5",
    "origin_creative_id": "origin_creative_md5", "替换前创意": "origin_creative_md5",
    "app_id": "app_id", "adx_app_id": "app_id", "媒体id": "app_id", "应用id": "app_id",
    "dspid": "dsp_id", "dsp_id": "dsp_id", "dsp": "dsp_id",
    "landing": "landing", "landing_link": "landing", "landing_url": "landing",
    "落地页": "landing", "dp": "dplink", "dplink": "dplink", "deep_link": "dplink",
    "深链": "dplink", "bns_date": "bns_date", "日期": "bns_date",
    "ts": "ts", "event_time": "ts", "时间": "ts", "具体时间": "ts",
    "make": "make", "品牌": "make", "model": "model", "机型": "model",
    "adx_place_id": "adx_place_id", "place_id": "adx_place_id",
    "space_id": "adx_place_id", "广告位": "adx_place_id", "广告位id": "adx_place_id",
    "oaid": "oaid", "oa": "oaid", "idfa": "idfa", "ip": "ip", "ip地址": "ip",
    "os": "os", "系统": "os", "osv": "osv", "系统版本": "osv",
}
_ALLOWED_DETAIL_FIELDS = set(_DETAIL_OUTPUT_FIELDS + _DETAIL_OPTIONAL_FIELDS)


def normalize_detail_requested_fields(requested_fields: Any | None) -> list[str]:
    """明细 requested_fields → 展示层字段；未知字段忽略；非空时末尾补 ts。"""
    raw_fields = split_csv_values(requested_fields)
    expanded: list[str] = []
    for raw in raw_fields:
        expanded.extend(part for part in re.split(r"[,，、\s]+", str(raw)) if part)

    fields: list[str] = []
    for raw in expanded:
        key = str(raw).strip().lower()
        if not key:
            continue
        normalized = _DETAIL_OUTPUT_FIELD_ALIASES.get(key, key)
        if normalized in _ALLOWED_DETAIL_FIELDS and normalized not in fields:
            fields.append(normalized)
    if fields and "ts" not in fields:
        fields.append("ts")
    return fields


def normalize_source_filter(source: Any | None) -> str:
    """广告版本入参 → '4' / '5'；无法识别返回空串（不过滤）。"""
    if source is None:
        return ""
    raw = str(source).strip().lower()
    if raw in {"4", "4.0"}:
        return "4"
    if raw in {"5", "5.0"}:
        return "5"
    return ""


_STAGE_ALIASES = {"填充": "填充", "fill": "填充", "点击": "点击", "click": "点击"}


# =========================================================================
# 4. 契约校验：脏参数不得静默进入 SQL
# =========================================================================

CONTRACT_ERROR_PREFIX = "[参数校验未通过] "


def _id_tokens(value: Any) -> list[str]:
    tokens = []
    for token in split_csv_values(value):
        if token.endswith(".0") and token[:-2].isdigit():
            token = token[:-2]
        tokens.append(token)
    return tokens


def validate_detail_args(args: dict[str, Any]) -> list[str]:
    """返回错误列表；空列表表示通过。只校验，不改写入参。"""
    errors: list[str] = []

    for name in ("app_id", "place_id", "dsp_id"):
        value = args.get(name)
        if value in (None, "", [], ()):
            continue
        tokens = _id_tokens(value)
        invalid = [token for token in tokens if not token.isdigit()]
        if invalid:
            errors.append(
                "app_id/place_id/dsp_id 只能是数字 id，多个用英文逗号分隔，"
                f"收到 {', '.join(invalid[:3])}。媒体名/广告位名请先用 "
                "space_name_to_space_id 解析成 id。"
            )
            break

    for name in ("bns_date_start", "bns_date_end"):
        value = args.get(name)
        if value in (None, "", [], ()):
            continue
        if parse_date(value) is None:
            errors.append(
                f"日期 {value!r} 无法识别，请传 YYYYMMDD（如 20261110）或 YYYY-MM-DD。"
            )
            break

    hour = args.get("bns_hour")
    if hour not in (None, "", [], ()) and normalize_hour(hour) is None:
        errors.append(
            f"bns_hour={hour!r} 无法解析为小时，解析失败会被当成不过滤小时，从而扫全天分区。"
            "请传整点（9）或两位小时列表（\"08,09,10\"）。"
        )

    source = args.get("source")
    if source not in (None, "", [], ()) and not normalize_source_filter(source):
        errors.append(
            f"source={source!r} 无法识别，只能是 \"4\"（4.0）或 \"5\"（5.0）。"
        )

    stage = args.get("stage")
    if stage not in (None, "", [], ()):
        if str(stage).strip().lower() not in _STAGE_ALIASES:
            errors.append(f"stage={stage!r} 无法识别，只能是 \"填充\" 或 \"点击\"。")

    requested = args.get("requested_fields")
    if requested not in (None, "", [], ()) and not normalize_detail_requested_fields(requested):
        valid = "、".join(_DETAIL_OUTPUT_FIELDS + _DETAIL_OPTIONAL_FIELDS)
        errors.append(
            f"requested_fields={requested!r} 里没有可识别的字段；可选：{valid}。"
        )

    if not any(split_csv_values(args.get(name)) for name in _DETAIL_FILTER_FIELDS):
        errors.append(
            "至少需要提供一个筛选条件：creative_md5、origin_creative_md5、landing、"
            "dplink、oaid、app_id、place_id、dsp_id、make、model、os、osv、idfa、ip。"
        )
    return errors


def contract_error_payload(message: str) -> dict[str, Any]:
    """校验失败结果：status=error + 摘要；不抛异常（ADK 会 re-raise 成整轮 error）。"""
    text = message if message.startswith(CONTRACT_ERROR_PREFIX) else CONTRACT_ERROR_PREFIX + message
    return ToolResult.from_text(text, status="error").to_model_payload()


def detail_query_contract(func: Callable) -> Callable:
    """明细工具契约装饰器：签名不变，校验失败返回 error payload。"""
    signature = inspect.signature(func)

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        tool_context = kwargs.get("tool_context")
        try:
            bound = signature.bind_partial(*args, **kwargs)
            payload = {k: v for k, v in bound.arguments.items() if k != "tool_context"}
            tool_context = tool_context or bound.arguments.get("tool_context")
        except TypeError:
            payload = {k: v for k, v in kwargs.items() if k != "tool_context"}

        missing = [
            name
            for name, param in signature.parameters.items()
            if param.default is inspect.Parameter.empty
            and param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            and name not in payload
        ]
        if missing:
            return contract_error_payload(f"缺少必填参数：{'、'.join(missing)}。")

        errors = validate_detail_args(payload)
        if errors:
            return contract_error_payload("；".join(errors))
        return await func(*args, **kwargs)

    return wrapper


# =========================================================================
# 5. SQL 文本拼接
# =========================================================================


def sql_quote(value: Any) -> str:
    """SQL 字符串字面量转义：反斜杠与单引号双写。"""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def sql_in_clause(values: list[Any]) -> str:
    """ClickHouse/ODPS 的 IN 字面量串（驱动不支持参数化，只能转义拼接）。"""
    return "','".join(sql_quote(v) for v in values)


def sql_in_params(values: list[Any]) -> tuple[str, list[Any]]:
    """MySQL 参数化 IN：值列表 → ("%s,%s", 值列表)；空列表 → ("NULL", [])。"""
    values = list(values)
    return (",".join(["%s"] * len(values)) or "NULL"), values


def minute_partition_condition(
    start_dt: datetime,
    end_dt: datetime,
    date_col: str = "bns_date",
    hour_col: str = "hour",
    minute_col: str = "minute",
) -> str:
    """分钟级时间范围 → (date, hour, minute) 元组范围条件。"""
    start_dt = start_dt.replace(second=0, microsecond=0)
    end_dt = end_dt.replace(second=0, microsecond=0)
    if end_dt < start_dt:
        return "1 = 0"
    start_key = (start_dt.strftime("%Y%m%d"), f"{start_dt.hour:02d}", f"{start_dt.minute:02d}")
    end_key = (end_dt.strftime("%Y%m%d"), f"{end_dt.hour:02d}", f"{end_dt.minute:02d}")
    return (
        f"({date_col}, {hour_col}, {minute_col}) >= "
        f"('{start_key[0]}', '{start_key[1]}', '{start_key[2]}') "
        f"AND ({date_col}, {hour_col}, {minute_col}) <= "
        f"('{end_key[0]}', '{end_key[1]}', '{end_key[2]}')"
    )


# =========================================================================
# 6. 结果摘要：模型看摘要，前端看 HTML
# =========================================================================

MODEL_TEXT_LIMIT = 1500
_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://[^\s\"'<>）)]+", re.IGNORECASE)
_OSS_HINTS = ("oss", "download", "file")


def _strip_tags(text: str) -> str:
    return _html.unescape(_ANY_TAG_RE.sub("", text or "")).strip()


def first_table(text: str) -> tuple[list[str], list[list[str]]]:
    """第一个表格的 (表头, 数据行)；非表格返回空。"""
    match = _TABLE_RE.search(text or "")
    if not match:
        return [], []
    header: list[str] = []
    data: list[list[str]] = []
    for index, row_html in enumerate(_TR_RE.findall(match.group(0))):
        cells = [_strip_tags(cell) for cell in _CELL_RE.findall(row_html)]
        if not cells:
            continue
        if index == 0 and "<th" in row_html.lower():
            header = cells
        else:
            data.append(cells)
    return header, data


def find_oss_url(text: str) -> str:
    """取文本中第一个疑似 OSS 结果链接。"""
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0)
        if any(hint in url.lower() for hint in _OSS_HINTS):
            return url
    return ""


def summarize_text(text: str, limit: int = MODEL_TEXT_LIMIT) -> str:
    """模型摘要：表格只给行数与列名；纯文本超长截断。"""
    if not text:
        return ""
    header, data = first_table(text)
    if header or data:
        parts = [f"表格约 {len(data)} 行"]
        if header:
            parts.append("列：" + "、".join(header))
        summary = "；".join(parts)
    else:
        summary = _strip_tags(text)
    return summary if len(summary) <= limit else summary[:limit] + "…"


def strip_tables_for_model(text: str) -> str:
    """把 HTML 表格替换成「（表格约 N 行；列：…）」，模型侧不看表格内容。"""
    raw = text or ""
    if "<table" not in raw.lower():
        return raw

    def _one(match: re.Match[str]) -> str:
        header, data = first_table(match.group(0))
        parts = [f"表格约 {len(data)} 行"]
        if header:
            parts.append("列：" + "、".join(header))
        return "（" + "；".join(parts) + "）"

    return _TABLE_RE.sub(_one, raw)


@dataclass
class ToolResult:
    """工具结果：html 是前端预览，message 是给模型的摘要。"""

    status: str = "success"
    html: str = ""
    message: str = ""
    columns: tuple[str, ...] = ()
    oss_url: str = ""
    rows: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_text(cls, text: str, *, status: str = "success") -> ToolResult:
        text = text or ""
        header, data = first_table(text)
        return cls(
            status=status,
            html=text,
            message=summarize_text(text),
            columns=tuple(header),
            oss_url=find_oss_url(text),
            rows=len(data),
        )

    def to_model_payload(self, limit: int = MODEL_TEXT_LIMIT) -> dict[str, Any]:
        """模型侧 function_response：不含 html。"""
        parts = [f"status: {self.status}"]
        if self.message:
            parts.append(self.message)
        if self.oss_url:
            parts.append("oss_url: " + self.oss_url)
        summary = "\n".join(parts)
        if len(summary) > limit:
            summary = summary[:limit] + "…"
        return {
            "status": self.status,
            "summary": summary,
            "columns": list(self.columns),
            "oss_url": self.oss_url,
        }


# =========================================================================
# 7. 图片输入：类型判定
# =========================================================================

_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}


def sniff_image_mime(raw: bytes) -> str:
    """按文件头魔数判图片类型；判不出返回空串。"""
    head = (raw or b"")[:12]
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:2] == b"BM":
        return "image/bmp"
    return ""


def mime_by_url(url: str) -> str:
    """按 URL 路径后缀判类型（忽略 query）；判不出返回空串。"""
    path = urlsplit(str(url or "")).path
    suffix = f".{path.rsplit('.', 1)[-1].lower()}" if "." in path else ""
    return _MIME_BY_SUFFIX.get(suffix, "")


__all__ = [
    "split_csv_values", "dedupe", "join_values",
    "normalize_datetime_text", "parse_datetime", "event_time_bounds",
    "normalize_query_date", "parse_date", "normalize_adx_partition_hours",
    "extract_hour", "normalize_hour", "extend_hour_by_range", "collapse_single_day_date",
    "normalize_detail_requested_fields", "normalize_source_filter",
    "validate_detail_args", "contract_error_payload", "detail_query_contract",
    "CONTRACT_ERROR_PREFIX",
    "sql_quote", "sql_in_clause", "sql_in_params", "minute_partition_condition",
    "ToolResult", "summarize_text", "strip_tables_for_model", "first_table", "find_oss_url",
    "sniff_image_mime", "mime_by_url",
]
