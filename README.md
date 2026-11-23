# agent-portfolio

面试用的「可验证工程证据」三件套。不是 demo：把生产 agent 的**稳定语义**抽成无 I/O 内核，本地一条命令可验证。

| 件 | 目录 | 交付物 |
|---|---|---|
| ① 工程证据 | `toolcore/` + `tests/` + `.github/workflows/ci.yml` | 21 条单测 + CI 门禁，`pytest` 秒级跑完 |
| ② 项目故事 | `docs/architecture.md`、`docs/interview-notes.md` | 架构图（Mermaid）+ 3 个 STAR（OOM / 进度恢复 / 口径漂移）+ ADK vs LangGraph |
| ③ 框架广度 | `langgraph-replica/` | LangGraph 复刻「素材→创意→落地页→合并」，含确认挂起与恢复 |

## 30 秒验证

```bash
cd ~/agent-portfolio
python3 -m venv .venv && ./.venv/bin/pip install -e ".[test]"
./.venv/bin/python -m pytest -q      # 21 passed
bash scripts/verify.sh               # 单测 + LangGraph 演示
```

## 目录

```
agent-portfolio/
├── README.md
├── pyproject.toml               # 包定义 + pytest/ruff 配置
├── toolcore/__init__.py         # 契约校验 / 日期小时 / SQL 拼接 / 结果摘要 / MIME
├── tests/test_toolcore.py       # 21 条回归用例
├── docs/
│   ├── architecture.md          # 生产架构 + 设计决策 + 失败模式
│   └── interview-notes.md       # 3 个 STAR + 框架边界
├── .github/workflows/ci.yml     # GitHub Actions：ruff + pytest（3.11/3.12）
├── langgraph-replica/
│   ├── graph.py                 # StateGraph + 条件路由 + 确认挂起
│   ├── requirements.txt
│   └── README.md
└── scripts/verify.sh
```

## 21 条测试在防什么

核心不是「算得对不对」，而是**参数错了但没报错、查询范围被静默放大**这类线上最常见的失败：

| 组 | 条数 | 覆盖 |
|---|---|---|
| 契约 | 9 | 非数字 id、坏小时、非法日期、非法 source、未知字段、空筛选、合法放行且不改写入参、缺必填、校验失败不执行 |
| 时间/字段 | 6 | 分钟窗口、多小时展开、跨小时区间、单日折叠、字段别名+补 ts、日期格式兼容 |
| SQL/摘要/MIME | 6 | 空 IN、引号与反斜杠转义、分钟分区条件、HTML 表格摘要、超长截断、图片魔数/后缀 |

## 与生产仓库的关系

`toolcore/` 是从 `ad_agent_sample3/tools3/` 的纯函数**提取语义**后的无 I/O 实现（去掉 OSS/ADK/数据库依赖），断言与生产口径一致；为了可独立运行做了精简，不是逐行复制。生产仓库重构稳定后，同一批用例可把 import 指回 `tools3.param_parsing` 等模块。

## 面试怎么用

- **30 秒**：一句话 + `docs/architecture.md` 第一张图。
- **3 分钟**：讲一个 STAR（推荐「进度恢复」），落点是「约束 → 决策 → 可验证结果」。
- **10 分钟**：`pytest -q` 跑一遍，再讲「为什么是这 21 条而不是 200 条」——测的是静默失败，不是覆盖率数字。
- **15 分钟**：打开 `langgraph-replica`，讲「同一业务语义在 ADK 与 LangGraph 下的控制流差异」。
