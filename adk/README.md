# ADK 实现：顺序 Agent + 节点内守卫

同一业务链路的三种编排实现之一。阶段语义来自 [`chaincore`](../chaincore/__init__.py)，这里只回答「下一步走哪」。

## 怎么表达控制流

```python
SequentialAgent(
    name="material_chain",
    sub_agents=[
        _stage("parse_request", C.parse_request),
        _stage("resolve_creatives", C.resolve_creatives),
        _stage("route_by_date", C.route_by_date),
        _stage("confirm", confirm),      # 未批准 → 写入 pending 状态
        _stage("query", query),          # 内部判断查 CK 还是 ODPS
        _stage("merge", merge),          # pending 时直接跳过
        _stage("summarize", summarize),  # pending 时直接跳过
    ],
)
```

- **ADK 的 `SequentialAgent` 只有「依次执行」，没有条件边。** 分支只能收敛到节点内部判断，或者交给模型用 `transfer` 决定走哪个子 Agent。
- 所以这里的分支表现为「**先写状态，后续节点自己看状态决定要不要跳过**」，而不是图上的两条边。
- 自定义 Agent 只需实现 `_run_async_impl`：往事件流里 `yield Event`，状态变更通过 `EventActions(state_delta=...)` 交给 `SessionService` 落盘。

## 跑起来

```bash
pip install -r adk/requirements.txt
python adk/chain.py --demo
```

## 与其他两个实现的差异

| 维度 | 本实现 | LangGraph | LangChain |
|---|---|---|---|
| 控制流 | Agent 顺序 + 节点守卫 | 图的边 | Runnable 组合 |
| 分支 | 节点内部 if | 条件边 | 运行时条件 |
| 状态 | Session state + `state_delta` | 图状态 | 普通 dict |
| 人工介入 | 原生 `request_confirmation`（本实现用状态模拟） | `interrupt()` | 自行实现 |
| 调试 | 事件流逐条 `yield`，可审计 | 图结构 | tracing |

## 边界

- **没有接模型**：全部阶段是确定性函数，`SequentialAgent` 不需要 LLM 也能跑。生产里同一个位置是「LLM 决定调哪个工具」，这里的对照点是执行模型本身。
- 没用原生 `request_confirmation`，而是用状态标志模拟挂起——原生确认要配合 Runner 的重放机制，超出本目录想验证的范围。
- ADK 2.15 中 `SequentialAgent` 已标记 deprecated（官方建议迁移到 `Workflow`，但当前版本尚未完全可用），这里保留是为了对照，升级时需要注意。
