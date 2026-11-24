# agent-portfolio

面试用的「可验证工程证据」。不是 demo：把生产 agent 的**稳定语义**抽成无 I/O 内核，再把同一条链路用三种编排框架各实现一次，本地一条命令可验证。

| 件 | 目录 | 交付物 |
|---|---|---|
| ① 工程证据 | `toolcore/` + `tests/` + `.github/workflows/ci.yml` | 25 条测试 + CI 门禁，`pytest` 秒级跑完 |
| ② 框架广度 | `adk/`、`langchain/`、`langgraph/` | 同一链路的三种编排实现，**输出逐字段一致** |
| ③ 项目故事 | `docs/architecture.md`、`docs/interview-notes.md` | 架构图（Mermaid）+ 3 个 STAR + ADK vs LangGraph |

## 30 秒验证

```bash
cd ~/agent-portfolio
python3 -m venv .venv && ./.venv/bin/pip install -e ".[test,frameworks]"
./.venv/bin/python -m pytest -q      # 25 passed
bash scripts/verify.sh               # 单测 + 三套框架演示
```

只关心内核、不想装框架依赖时：`pip install -e ".[test]"`，跨框架用例会自动跳过。

## 同一语义，三种编排

`chaincore/` 里是三个实现共享的**阶段语义**（每个阶段做什么、返回什么），
三个目录只回答同一句话：**「下一步走哪，由谁决定」**。

| 目录 | 框架 | 控制流怎么表达 | 分支在哪 |
|---|---|---|---|
| `langgraph/` | LangGraph | 显式状态图 + 条件边 | 边 |
| `langchain/` | LangChain（LCEL） | `RunnableBranch` 运行时选 | 运行时条件（有顺序陷阱） |
| `adk/` | Google ADK | 顺序 Agent + 节点内守卫 | 节点内部 if |

正因为语义都下沉到 `chaincore`，三个实现可以**逐行 diff**，差异全是编排，不掺业务。

`tests/test_frameworks.py` 把这件事变成断言：三个场景下，三个实现的
`status / route / creatives / merged / message` 必须完全相同。
任何一处控制流写错（比如 `RunnableBranch` 分支顺序颠倒、ADK 守卫漏判）都会在这里失败。

> 命名注意：安装的 `langgraph` 是**命名空间包**（没有 `__init__.py`），仓库里的 `langgraph/`
> 目录会被并进它的 `__path__`。因此 ① 必须用脚本方式运行（`python langgraph/chain.py --demo`），
> `python -m langgraph.chain` 会把本目录当包导入；② 本目录里不能出现与库同名的子模块
> （`graph.py` 会撞上库的 `langgraph/graph/`），所以入口文件叫 `chain.py`。

## 目录

```
agent-portfolio/
├── README.md
├── pyproject.toml               # 包定义 + pytest/ruff 配置 + frameworks 可选依赖
├── toolcore/__init__.py         # 契约校验 / 日期小时 / SQL 拼接 / 结果摘要 / MIME
├── chaincore/__init__.py        # 素材链路语义（三个框架共享）
├── adk/
│   ├── chain.py                 # 顺序 Agent + 状态守卫
│   ├── requirements.txt
│   └── README.md
├── langchain/
│   ├── chain.py                 # LCEL 组合 + RunnableBranch
│   ├── requirements.txt
│   └── README.md
├── langgraph/
│   ├── chain.py                 # StateGraph + 条件边 + 确认挂起
│   ├── requirements.txt
│   └── README.md
├── tests/
│   ├── test_toolcore.py         # 21 条内核用例
│   └── test_frameworks.py       # 跨框架一致性断言
├── docs/
│   ├── architecture.md          # 生产架构 + 设计决策 + 失败模式
│   └── interview-notes.md       # 3 个 STAR + 框架边界
├── .github/workflows/ci.yml     # GitHub Actions：ruff + pytest + 三套 demo（3.11/3.12）
└── scripts/verify.sh
```

## 25 条测试在防什么

核心不是「算得对不对」，而是**参数错了但没报错、查询范围被静默放大**这类线上最常见的失败：

| 组 | 条数 | 覆盖 |
|---|---|---|
| 契约 | 9 | 非数字 id、坏小时、非法日期、非法 source、未知字段、空筛选、合法放行且不改写入参、缺必填、校验失败不执行 |
| 时间/字段 | 6 | 分钟窗口、多小时展开、跨小时区间、单日折叠、字段别名+补 ts、日期格式兼容 |
| SQL/摘要/MIME | 6 | 空 IN、引号与反斜杠转义、分钟分区条件、HTML 表格摘要、超长截断、图片魔数/后缀 |
| 跨框架一致 | 4 | 三个场景 × 三种编排输出一致；挂起场景必须真的挂起 |

## 与生产仓库的关系

`toolcore/` 是从 `ad_agent_sample3/tools3/` 的纯函数**提取语义**后的无 I/O 实现（去掉 OSS/ADK/数据库依赖），断言与生产口径一致；为了可独立运行做了精简，不是逐行复制。

`adk/`、`langchain/`、`langgraph/` 里验证的不是生产代码的搬运，而是**同一条确定性链路在三种编排模型下的表达方式**：生产用 ADK（模型自主编排 + 回调链），这里把「如果换一套控制流模型，会写成什么样、代价是什么」变成可运行、可 diff 的代码。

## 面试怎么用

- **30 秒**：一句话 + `docs/architecture.md` 第一张图。
- **3 分钟**：讲一个 STAR（推荐「进度恢复」），落点是「约束 → 决策 → 可验证结果」。
- **10 分钟**：`pytest -q` 跑一遍，再讲「为什么是这 25 条而不是 200 条」——测的是静默失败与跨框架语义漂移，不是覆盖率数字。
- **15 分钟**：打开三个框架目录，讲「同一业务语义，控制流归模型还是归代码」的差异与选型边界。
