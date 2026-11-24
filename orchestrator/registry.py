"""工具注册表：让「注册表 / 权限白名单 / 实际实现」三方不一致在导入时就炸。

## 为什么需要它

工具清单在三个地方各有一份，历史上就是这样漏的：

1. **注册表**（`SPECS`）：工具的元数据——归属哪个子 Agent、是否只读、参数名、给模型看的说明；
2. **权限白名单**（`AGENT_TOOLS`）：每个子 Agent 能看见哪些工具；
3. **实现**（`IMPLEMENTATIONS`）：真正被调用的函数对象。

三份名单手工维护，任何一方新增、改名、删除而另一方没跟上，表现都是**静默的**：
工具在提示词里写着、模型调了、注册表里查不到 → 被当成未知工具丢掉，或者反过来
实现留着但没人能调到。线上发现时通常已经隔了几天。

所以这里把一致性做成**导入期断言**：三方向不一致就抛 `RegistryError`，
让问题停在 CI，而不是停在线上。

## 一条容易忽略的规则

实现的**必填参数**必须是 spec 里声明过的参数。模型是按 spec 的参数表填参的，
实现里多出一个必填参数就意味着"这个工具永远调不通"——这种错误比缺实现更难查。
"""
from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field


class RegistryError(Exception):
    """三方不一致：注册表 / 白名单 / 实现。"""


@dataclass(frozen=True)
class ToolSpec:
    """工具的元数据。参数用元组，方便直接比对。"""

    name: str
    agent: str
    readonly: bool
    params: tuple[str, ...]
    description: str = ""


@dataclass
class Registry:
    """三方名单的容器。构造时不校验，`validate()` 时才校验（便于测试构造坏数据）。"""

    specs: Mapping[str, ToolSpec] = field(default_factory=dict)
    agent_tools: Mapping[str, Sequence[str]] = field(default_factory=dict)
    implementations: Mapping[str, Callable] = field(default_factory=dict)


def _required_params(func: Callable) -> set[str]:
    """实现的必填参数名（不含 *args/**kwargs 与自身）。"""
    signature = inspect.signature(func)
    return {
        name
        for name, param in signature.parameters.items()
        if param.default is inspect.Parameter.empty
        and param.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }


def validate(registry: Registry) -> list[str]:
    """返回所有不一致项（空列表表示通过）。

    逐条检查而不是"发现第一个就抛"，是因为一次把问题全列出来才能一次改完。
    """
    problems: list[str] = []
    specs = registry.specs
    agent_tools = registry.agent_tools
    impls = registry.implementations

    # 1) 白名单里出现的工具，注册表必须有
    referenced: set[str] = set()
    for agent, names in agent_tools.items():
        for name in names:
            referenced.add(name)
            if name not in specs:
                problems.append(f"白名单里有、注册表里没有：{agent} -> {name}")

    # 2) 注册表里的工具，必须至少被一个 Agent 引用（否则是孤儿，谁也调不到）
    for name in specs:
        if name not in referenced:
            problems.append(f"注册表里有、但没有任何 Agent 引用（孤儿工具）：{name}")

    # 3) 注册表里的工具，必须有实现
    for name in specs:
        if name not in impls:
            problems.append(f"注册表里有、但没有实现：{name}")

    # 4) 实现的必填参数必须都在 spec 声明的参数里
    for name, func in impls.items():
        spec = specs.get(name)
        if spec is None:
            problems.append(f"有实现、但注册表里没有：{name}")
            continue
        extra = _required_params(func) - set(spec.params)
        if extra:
            problems.append(
                f"{name} 的实现比 spec 多出必填参数：{', '.join(sorted(extra))}"
                "（模型只会按 spec 填参，这个工具永远调不通）"
            )

    # 5) spec 声明的归属 Agent 必须真的把这个工具列进白名单
    for name, spec in specs.items():
        if name in referenced and name not in (agent_tools.get(spec.agent) or ()):
            problems.append(f"{name} 的 spec 归属 {spec.agent}，但该 Agent 的白名单里没有它")

    return problems


def build_registry(
    specs: Mapping[str, ToolSpec],
    agent_tools: Mapping[str, Sequence[str]],
    implementations: Mapping[str, Callable],
) -> Registry:
    """构造并立即校验；不一致直接抛错（导入期失败，不留到运行时）。"""
    registry = Registry(specs=dict(specs), agent_tools=dict(agent_tools), implementations=dict(implementations))
    problems = validate(registry)
    if problems:
        raise RegistryError("工具注册表不一致：\n- " + "\n- ".join(problems))
    return registry


def tools_for(registry: Registry, agent: str) -> list[Callable]:
    """取某个子 Agent 的工具实现列表（顺序与白名单一致）。"""
    return [registry.implementations[name] for name in registry.agent_tools.get(agent, ())]


def readonly_tools(registry: Registry) -> list[str]:
    """所有只读工具名：写操作必须另行走审批，不能混在只读集合里。"""
    return sorted(name for name, spec in registry.specs.items() if spec.readonly)


__all__ = [
    "Registry",
    "RegistryError",
    "ToolSpec",
    "build_registry",
    "readonly_tools",
    "tools_for",
    "validate",
]
