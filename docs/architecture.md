# 生产 Agent 架构

> 一句话：把「广告素材分析」的 30+ 个业务查询与写操作，组织成一个可审计、可恢复、可评测的多 Agent 系统，跑在 AgentKit Runtime 上，前面挂一个业务工作台（8888）、一个监控页（8890）、一个评测页（8891）。

## 1. 全景图

```mermaid
flowchart LR
    U[业务同学] --> WEB[8888 工作台<br/>SSE 对话 / 确认按钮 / 进度卡]
    WEB -->|HTTP + 进度头| APP[Agent Runtime<br/>FastAPI + ADK Runner]

    subgraph APP
        ROOT[root: 一级路由] --> ID[识别 agent<br/>OCR / logo / 多模态]
        ROOT --> ENT[实体 agent<br/>标签 / 分类 / md5 互查]
        ROOT --> SIM[相似图 agent<br/>向量检索 + 后续链路]
        ROOT --> DEL[交付 agent<br/>曝光 / 填充 / 点击明细]
        ROOT --> WRI[写入 agent<br/>标签 / 分类关系]
        HOOK[hooks: 回调链<br/>16 项 before_tool + 确认/审批]
        ROOT -.-> HOOK
        DEL -.-> HOOK
        TOOLS[tools: 30+ 个工具<br/>registry 单一事实源]
        ROOT --> TOOLS
        DEL --> TOOLS
    end

    TOOLS --> CK[(ClickHouse<br/>4.0/5.0 聚合)]
    TOOLS --> ODPS[(ODPS<br/>明细 / 长任务)]
    TOOLS --> MYSQL[(MySQL<br/>标签 / 分类)]
    TOOLS --> ES[(Elasticsearch<br/>5.0 创意库)]
    TOOLS --> OSS[(OSS<br/>结果 CSV + 进度文件)]

    APP --> MON[8890 日志/追踪]
    APP --> EVAL[8891 评测<br/>数据集 → 轨迹判定]
    EVAL -.回归.-> APP
```

## 2. 关键设计决策

| 决策 | 为什么 | 代价 |
|---|---|---|
| **root 只做一级路由，子 agent 禁 transfer** | 跨 agent 接力会让状态透传失控；每个子 agent 拥有完整工具集，链路在内部走完 | 子 agent 工具多，提示词更长 |
| **registry 作为工具的唯一事实源** | 历史上有三份手工名单（TOOL_META / FIELD_META / 回显名单），各自漏项 | 新增工具要改注册表 + 提示词 |
| **ToolResult 双读者**：模型看摘要，前端看 HTML | 表格进上下文会撑爆 token，且模型会复述行数据 | 渲染层要维护两套视图 |
| **所有结果落 OSS，模型只拿「行数 + 列名 + 链接」** | 大结果不驻留内存，模型无法编造行内容 | 下游要支持 OSS 链接作为输入 |
| **长查询 submit_only + 进度文件** | veFaaS 单请求 900 秒上限，ODPS 可能跑几十分钟 | 前端要轮询、要能恢复 |
| **写操作走原生 confirmation + 审批** | 不可逆动作必须人类确认；模型不能替用户点确认 | 确认态与重放要仔细处理 |
| **纯函数抽到 param_parsing / image_input** | 让「参数语义」可单测、可复用，不依赖 I/O | 多一层模块 |

## 3. 失败模式与防线

| 失败模式 | 症状 | 防线 |
|---|---|---|
| 参数静默失真 | `bns_hour="3点-5点"` 解析失败 → 不过滤小时 → 扫全天 | 契约层先拦，返回可自纠的 error |
| 查询范围被放大 | 媒体名未解析成 app_id，SQL 退化成全媒体 | 解析失败显式清空 + 后续工具缺 id 时拦截 |
| 模型跳步/乱序 | 素材 URL 直接当创意 md5 传 | 占位符还原 + 守卫 + 链路提示 |
| 重复调用 | 同一工具同参反复调 | 同参拦截、同工具最多 3 次、终态工具直出 |
| 长任务断流 | 页面刷新/服务重启后无结果 | 进度文件 + InstanceID + 前端轮询恢复 |
| 结果过大 | 50 万行一次性读入 → OOM | 逐行流式落盘 + 分片上传 |
| 提示词与工具职责漂移 | 路由表说 A，工具实际做 B | registry + 契约测试 + 评测集 |

## 4. 数字口径（面试可引用）

- 工具：30+ 个只读/写入工具，6 个子 agent；
- 数据源：4 类（CK / ODPS / MySQL / ES），4.0/5.0 双版本口径；
- 长任务：单请求 900 秒上限 → 提交即返回，进度文件 8 秒节流，心跳 15 秒；
- 大结果：50 万行实测曾 OOM（约 5.8GB），改造后内存只保留单行 + 分片缓冲；
- 评测：数据集 3 份（创意口径 / 事件口径 / 冒烟），判定「工具轨迹 + 是否真有 functionResponse」。

## 5. 演进时间线（讲成长用）

1. 单 agent + 20 个工具 → 路由混乱、提示词爆炸；
2. 拆 6 个子 agent + registry → 归属清晰，但状态透传靠 hook；
3. 引入 ToolResult / 进度文件 / 确认 → 可用性与安全性；
4. 评测页 + 监控页 + 静态检查 → 从「能跑」到「可回归」；
5. 正在做：精简抽象、补单测与 CI、把确定性流程往图编排迁移。
