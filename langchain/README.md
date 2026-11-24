# LangChain 实现：控制流写成 Runnable 的组合

同一业务链路的三种编排实现之一。阶段语义来自 [`chaincore`](../chaincore/__init__.py)，这里只回答「下一步走哪」。

## 怎么表达控制流

```python
chain = (
    _step(C.parse_request)
    | _step(C.resolve_creatives)
    | _step(C.route_by_date)
    | RunnableBranch(
        (lambda s: s["route"] == "ck", _tail(_step(C.query_ck))),
        (lambda s: not s["approved"], _step(C.pending)),
        _tail(_step(C.query_odps)),
    )
)
```

- **顺序**由 `|` 保证：每个 `RunnableLambda` 接收上一段的状态，返回合并后的新状态。
- **分支**由 `RunnableBranch` 表达：按顺序检查条件，**命中第一条就执行**，所以「CK / 挂起 / ODPS」这三条的顺序本身就是语义。
- **没有显式图结构**：链路是运行时拼出来的，看图只能读代码。

## 跑起来

```bash
pip install -r langchain/requirements.txt
python langchain/chain.py --demo
```

## 与其他两个实现的差异

| 维度 | 本实现 | LangGraph | ADK |
|---|---|---|---|
| 控制流 | Runnable 组合 | 图的边 | Agent 顺序 + 节点守卫 |
| 分支 | 运行时条件（有顺序陷阱） | 条件边，编译期定型 | 节点内部 if |
| 状态 | 普通 dict 逐层传递 | 图状态 | Session state + `state_delta` |
| 可观测 | 回调 / tracing | 图结构 | 事件流 |

## 边界

- `RunnableBranch` 的分支顺序是隐式语义：如果把「未批准」放在 CK 判断之前，ODPS 场景就会错误挂起。这是三个实现里最容易写错的地方。
- 只用了 `langchain-core`（LCEL），没有引入 `langchain` 主包和任何模型调用。
