"""跨框架一致性：同一业务语义，三种编排，结果必须逐字段一致。

这是这个仓库最核心的断言：**框架换来换去，业务语义不能变。**
三个实现共享 `chaincore` 的阶段语义，只各自回答「下一步走哪」，
所以任何一处控制流写错（比如 LangChain 的 `RunnableBranch` 分支顺序颠倒、
ADK 的守卫漏判），都会在这里被抓住。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

import chaincore as C

#: 实现文件 → 该实现缺哪个依赖就跳过（让没装框架的环境也能跑 toolcore 的用例）
IMPLEMENTATIONS: dict[str, tuple[str, str]] = {
    "langgraph": ("langgraph/chain.py", "langgraph.graph"),
    "langchain": ("langchain/chain.py", "langchain_core.runnables"),
    "adk": ("adk/chain.py", "google.adk.agents"),
}


def load(relpath: str) -> ModuleType:
    """按文件路径加载实现。

    不写成 `import adk.chain`：`langgraph/` 与 PyPI 的 `langgraph` 库同名，
    以包的方式导入会遮蔽真实库。按路径加载可以绕开这个命名冲突。
    """
    module_name = f"impl_{Path(relpath).parent.name}_{Path(relpath).stem}"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relpath)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("index", range(len(C.scenarios())))
def test_three_frameworks_agree(index: int) -> None:
    """三个场景下，三个实现的最终结果必须完全相同。"""
    scenario = C.scenarios()[index]
    views = {}
    for name, (relpath, dependency) in IMPLEMENTATIONS.items():
        pytest.importorskip(dependency, reason=f"{name} 依赖未安装")
        views[name] = C.result_view(load(relpath).run(**scenario["kwargs"]))

    baseline = views["langgraph"]
    for name, view in views.items():
        assert view == baseline, f"{name} 与 langgraph 的结果不一致：{view} != {baseline}"


def test_pending_scenario_really_pends() -> None:
    """第二个场景必须真的停在挂起态——否则一致性断言可能只是"都错了"。"""
    view = C.result_view(load("langgraph/chain.py").run(**C.scenarios()[1]["kwargs"]))
    assert view["status"] == "pending_approval"
    assert view["merged"] is None
