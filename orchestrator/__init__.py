"""编排层：注册表、回调链、审批、长任务。

这四个模块对应生产 Agent 的「框架胶水层」——不是业务逻辑，而是让业务逻辑
能安全跑起来的那部分：工具清单不许漂、模型看到的是占位符、写操作要人批、
长任务要能续跑。

它们都写成**不依赖具体框架**的形式：状态是普通 dict，请求是普通 dataclass，
所以能脱离 ADK 单测；接进框架时只写一层薄适配。
"""
from __future__ import annotations

from .approval import (
    ApprovalRequest,
    ApprovalStatus,
    can_execute,
    decide,
    mark_executed,
    replay_payload,
    request_approval,
)
from .hooks import (
    BEFORE_MODEL_HOOKS,
    ID_ONLY_PARAMS,
    LlmRequest,
    Message,
    before_tool,
    check_id_only_params,
    run_before_model,
)
from .registry import (
    Registry,
    RegistryError,
    ToolSpec,
    build_registry,
    readonly_tools,
    tools_for,
    validate,
)
from .task import (
    Progress,
    TaskStatus,
    finish,
    record,
    resume,
    retry_failed,
    start,
    submit,
    summary,
)

__all__ = [
    "BEFORE_MODEL_HOOKS",
    "ID_ONLY_PARAMS",
    "ApprovalRequest",
    "ApprovalStatus",
    "LlmRequest",
    "Message",
    "Progress",
    "Registry",
    "RegistryError",
    "TaskStatus",
    "ToolSpec",
    "before_tool",
    "build_registry",
    "can_execute",
    "check_id_only_params",
    "decide",
    "finish",
    "mark_executed",
    "readonly_tools",
    "record",
    "replay_payload",
    "request_approval",
    "resume",
    "retry_failed",
    "run_before_model",
    "start",
    "submit",
    "summary",
    "tools_for",
    "validate",
]
