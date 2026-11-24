"""注册表一致性用例：三方名单只要有一方没跟上，导入就该失败。

测的不是"校验函数写得对不对"，而是那些**静默失效**的形态：
工具在提示词里写着却查不到、实现留着却没人能调、参数对不上导致永远调不通。
"""
from __future__ import annotations

import pytest

from orchestrator.registry import (
    Registry,
    RegistryError,
    ToolSpec,
    build_registry,
    readonly_tools,
    tools_for,
    validate,
)


def _spec(name: str, agent: str = "query", readonly: bool = True, params=("q",)) -> ToolSpec:
    return ToolSpec(name=name, agent=agent, readonly=readonly, params=tuple(params))


def _impl(*, q=None):
    return {"ok": True}


def _registry(**overrides) -> Registry:
    specs = overrides.get("specs", {"query_ticket": _spec("query_ticket")})
    agents = overrides.get("agent_tools", {"query": ("query_ticket",)})
    impls = overrides.get("implementations", {"query_ticket": _impl})
    return Registry(specs=specs, agent_tools=agents, implementations=impls)


def test_valid_registry_builds():
    registry = build_registry(
        specs={"query_ticket": _spec("query_ticket")},
        agent_tools={"query": ("query_ticket",)},
        implementations={"query_ticket": _impl},
    )
    assert tools_for(registry, "query") == [_impl]


def test_whitelist_entry_without_spec_fails():
    registry = _registry(agent_tools={"query": ("query_ticket", "ghost_tool")})
    problems = validate(registry)
    assert any("白名单里有、注册表里没有" in item for item in problems)
    with pytest.raises(RegistryError):
        build_registry(registry.specs, registry.agent_tools, registry.implementations)


def test_orphan_spec_fails():
    """注册表里有、但没有任何 Agent 引用 —— 谁也调不到，等于没注册。"""
    registry = _registry(
        specs={"query_ticket": _spec("query_ticket"), "unused_tool": _spec("unused_tool")},
        implementations={"query_ticket": _impl, "unused_tool": _impl},
    )
    problems = validate(registry)
    assert any("孤儿工具" in item for item in problems)


def test_missing_implementation_fails():
    registry = _registry(implementations={})
    problems = validate(registry)
    assert any("没有实现" in item for item in problems)


def test_implementation_with_extra_required_param_fails():
    """实现比 spec 多一个必填参数 → 模型按 spec 填参永远调不通。"""
    def _impl_with_extra(*, q=None, secret_flag):
        return {}

    registry = _registry(implementations={"query_ticket": _impl_with_extra})
    problems = validate(registry)
    assert any("多出必填参数" in item and "secret_flag" in item for item in problems)


def test_implementation_with_optional_extra_param_passes():
    """多一个**可选**参数是允许的：模型不传也能跑。"""
    def _impl_optional(*, q=None, verbose=False):
        return {}

    registry = _registry(implementations={"query_ticket": _impl_optional})
    assert validate(registry) == []


def test_spec_agent_must_list_tool_in_its_whitelist():
    registry = _registry(
        specs={"query_ticket": _spec("query_ticket", agent="delivery")},
        agent_tools={"query": ("query_ticket",)},
    )
    problems = validate(registry)
    assert any("白名单里没有它" in item for item in problems)


def test_readonly_tools_excludes_write_operations():
    """只读集合里混进写操作，会让"不审批直接跑"的口子被打开。"""
    registry = _registry(
        specs={
            "query_ticket": _spec("query_ticket", readonly=True),
            "close_ticket": _spec("close_ticket", readonly=False, params=("id",)),
        },
        agent_tools={"query": ("query_ticket", "close_ticket")},
        implementations={"query_ticket": _impl, "close_ticket": _impl},
    )
    assert readonly_tools(registry) == ["query_ticket"]
