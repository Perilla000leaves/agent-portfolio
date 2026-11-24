"""工具入参契约层（Pydantic）：拦下「确定会炸 / 会静默失真」的脏参数。

背景：模型传来的参数没有运行时校验。ADK 的 function declaration 只是给模型看的说明书，
`FunctionTool.run_async` 把 args 原样透传给函数，所以脏参数会一路走到 SQL：

- `bns_hour="3点-5点"` 解析失败后被降级成「不过滤小时」，查询直接扩成全天扫描；
- `requested_fields` 全是未知字段时被静默忽略，回退成全字段大查询；
- `app_id="示例媒体-安卓"` 被当成 id 字符串拼进 SQL，查不到数据却不报错。

三条硬约束（改这里之前先读）：

1. **不改工具签名**：装饰器用 `functools.wraps`，框架通过 `inspect.signature`
   （跟随 `__wrapped__`）拿到的仍是原始签名，模型看到的工具说明书一个字不变。
2. **只校验、不改写入参**：归一化产物不写回 kwargs，避免把 `app_id="9044,9045"`
   这类既有语义悄悄改掉；真正的归一化仍归 `toolcore` 的解析函数（同一套口径只留一份）。
3. **失败必须回给模型让它自纠**：返回 `status="error"` 的结果，**不得抛异常**——
   框架会把工具异常 re-raise 成整轮 error，模型拿不到可自纠的提示。
"""
from __future__ import annotations

import functools
import inspect
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from . import (
    _DETAIL_FILTER_FIELDS,
    _DETAIL_OPTIONAL_FIELDS,
    _DETAIL_OUTPUT_FIELDS,
    ToolResult,
    normalize_detail_requested_fields,
    normalize_hour,
    normalize_source_filter,
    parse_date,
    split_csv_values,
    stash_preview_html,
)

#: 失败前缀：模型侧与用户侧都靠它识别「参数问题，需要改参数重试」
CONTRACT_ERROR_PREFIX = "[参数校验未通过] "

#: 纯数字 id 字段：名称/URL 传进来会被 SQL 当成 id 字符串
_NUMERIC_ID_FIELDS = ("app_id", "place_id", "dsp_id")

#: 阶段取值（生产里 fill / click 两个工具合并成一个 stage 参数）
_STAGE_VALUES = {"填充", "fill", "点击", "click"}


def _split_id_tokens(value: Any) -> list[str]:
    """按逗号拆 id；pandas 导出的 `9044.0` 先还原成整数再判数字。"""
    tokens = []
    for token in split_csv_values(value):
        if token.endswith(".0") and token[:-2].isdigit():
            token = token[:-2]
        tokens.append(token)
    return tokens


def _format_validation_error(exc: ValidationError) -> str:
    """把 ValidationError 收敛成一句中文提示（多条去重保序）。"""
    messages: list[str] = []
    for error in exc.errors():
        message = str(error.get("msg") or "").strip()
        # pydantic v2 会把 ValueError 的文案包成 "Value error, xxx"
        if message.startswith("Value error, "):
            message = message[len("Value error, ") :]
        if message and message not in messages:
            messages.append(message)
    return "；".join(messages) or "入参不合法，请检查参数名与取值后重试。"


class DetailQueryParams(BaseModel):
    """曝光 / 填充 / 点击明细工具共用的入参契约（覆盖三个工具的签名并集）。

    字段一律用 `Any` + `mode="before"` 校验器：不改变模型传来的原始值，只判断
    「这个值现在拿去查会不会静默出错」。未知参数（limit / force_odps 等）直接忽略。
    """

    model_config = ConfigDict(extra="ignore")

    creative_md5: Any = None
    origin_creative_md5: Any = None
    landing: Any = None
    dplink: Any = None
    oaid: Any = None
    app_id: Any = None
    place_id: Any = None
    dsp_id: Any = None
    make: Any = None
    model: Any = None
    os: Any = None
    osv: Any = None
    idfa: Any = None
    ip: Any = None
    bns_date_start: Any = None
    bns_date_end: Any = None
    bns_hour: Any = None
    source: Any = None
    stage: Any = None
    requested_fields: Any = None

    @field_validator(*_NUMERIC_ID_FIELDS, mode="before")
    @classmethod
    def _check_numeric_ids(cls, value: Any) -> Any:
        if value in (None, "", [], ()):
            return value
        tokens = _split_id_tokens(value)
        # 空串清洗后可能什么都不剩（如 [None]），与「未传」等价，不拦
        if not tokens:
            return value
        invalid = [token for token in tokens if not token.isdigit()]
        if invalid:
            raise ValueError(
                "app_id/place_id/dsp_id 只能是数字 id，多个用英文逗号分隔，"
                f"收到 {', '.join(invalid[:3])}。媒体名/广告位名请先用 "
                "space_name_to_space_id 解析成 id，不要直接传名称或 URL。"
            )
        return value

    @field_validator("bns_date_start", "bns_date_end", mode="before")
    @classmethod
    def _check_date(cls, value: Any) -> Any:
        if value in (None, "", [], ()):
            return value
        if parse_date(value) is None:
            raise ValueError(
                f"日期 {value!r} 无法识别，请传 YYYYMMDD（如 20261110）或 YYYY-MM-DD；"
                "相对日期（今天/昨天/最近7天）请先换算成具体日期。"
            )
        return value

    @field_validator("bns_hour", mode="before")
    @classmethod
    def _check_bns_hour(cls, value: Any) -> Any:
        if value in (None, "", [], ()):
            return value
        if normalize_hour(value) is None:
            raise ValueError(
                f"bns_hour={value!r} 无法解析为小时，解析失败会被当成「不过滤小时」"
                '从而扫全天分区。请传整点（9）或两位小时列表（"08,09,10"），'
                "跨多小时区间必须展开成完整列表。"
            )
        return value

    @field_validator("source", mode="before")
    @classmethod
    def _check_source(cls, value: Any) -> Any:
        if value in (None, "", [], ()):
            return value
        if not normalize_source_filter(value):
            raise ValueError(
                f'source={value!r} 无法识别，只能是 "4"（4.0 / CK）或 "5"（5.0 / ES）；'
                "不指定数据源时不要传该参数。"
            )
        return value

    @field_validator("stage", mode="before")
    @classmethod
    def _check_stage(cls, value: Any) -> Any:
        if value in (None, "", [], ()):
            return value
        if str(value).strip().lower() in _STAGE_VALUES:
            return value
        raise ValueError(f"stage={value!r} 无法识别，只能是「填充」（fill）或「点击」（click）。")

    @field_validator("requested_fields", mode="before")
    @classmethod
    def _check_requested_fields(cls, value: Any) -> Any:
        if value in (None, "", [], ()):
            return value
        if normalize_detail_requested_fields(value):
            return value
        valid = "、".join(_DETAIL_OUTPUT_FIELDS + _DETAIL_OPTIONAL_FIELDS)
        raise ValueError(
            f"requested_fields={value!r} 里没有可识别的字段；未知字段会被静默忽略并"
            f"回退成全字段查询。可选字段：{valid}。不指定字段时不要传该参数。"
        )

    @model_validator(mode="after")
    def _require_one_filter(self) -> DetailQueryParams:
        # 与查询核心的「至少一个筛选条件」判定保持一致：这里先拦，能给模型更明确的改法提示
        if any(split_csv_values(getattr(self, name)) for name in _DETAIL_FILTER_FIELDS):
            return self
        raise ValueError(
            "至少需要提供一个筛选条件："
            "creative_md5（替换后创意）、origin_creative_md5（替换前创意）、"
            "landing、dplink、oaid、app_id、place_id（广告位）、dsp_id、"
            "make（品牌）、model（机型）、os、osv、idfa、ip；"
            "若用户信息不全，请先向用户确认缺哪个条件，不要空条件查询。"
        )


def contract_error_payload(message: str, *, tool_name: str = "", state: Any = None) -> dict[str, Any]:
    """校验失败结果：与工具正常返回同为 ToolResult payload，只是 status=error。

    预览文案同样进 `temp:tool_preview:<tool>`（模型侧只看摘要），不引入第二套返回结构。
    """
    text = message if message.startswith(CONTRACT_ERROR_PREFIX) else CONTRACT_ERROR_PREFIX + message
    result = ToolResult.from_text(text, status="error")
    stash_preview_html(state, tool_name, result.html)
    return result.to_model_payload()


def detail_query_contract(func):
    """明细工具入参契约装饰器：签名不变，校验失败直接返回 error payload。

    只做校验，不把归一化结果写回入参；函数体里原有的归一化与业务校验保持不变
    （分层：契约层拦「必然错」的形状问题，业务层负责口径）。
    """

    signature = inspect.signature(func)

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any):
        state = kwargs.get("state")
        try:
            bound = signature.bind_partial(*args, **kwargs)
            payload = {key: value for key, value in bound.arguments.items() if key != "state"}
            state = state or bound.arguments.get("state")
        except TypeError:
            payload = {key: value for key, value in kwargs.items() if key != "state"}

        # 必填参数缺失时回 error payload，不要落到函数调用才抛 TypeError
        missing = [
            name
            for name, param in signature.parameters.items()
            if param.default is inspect.Parameter.empty
            and param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            and name not in payload
        ]
        if missing:
            return contract_error_payload(
                f"缺少必填参数：{'、'.join(missing)}。", tool_name=func.__name__, state=state
            )

        try:
            DetailQueryParams.model_validate(payload)
        except ValidationError as exc:
            return contract_error_payload(
                _format_validation_error(exc), tool_name=func.__name__, state=state
            )
        return await func(*args, **kwargs)

    return wrapper


__all__ = [
    "CONTRACT_ERROR_PREFIX",
    "DetailQueryParams",
    "contract_error_payload",
    "detail_query_contract",
]
