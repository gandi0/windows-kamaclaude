更好的Kamaclaude模型（拓展分支）加上测试适配windows平台的修复
！！！【本项目readme还在和codex交流重写中，下述借用下卡哥原项目的介绍先】

### 项目特色

这个项目，我采用全新的讲解方式，不是一下子直接给大家全部项目代码。

而且分成了 8个阶段，一步一步，带大家实现完整的kamaClaude。

每个阶段都不是堆功能，而是解决一个真实的 Agent 工程问题。

![](docs/images/2026-06-10_11-01-32.jpg)

| 阶段 | 主题 | 这一阶段真正解决的问题 |
| --- | --- | --- |
| S0 | 骨架与协议契约 | CLI 和 daemon 通过真实 IPC 完成一次 ping/pong |
| S1 | Agent 最小闭环 | 一次 `kama run` 从 goal 到 LLM、工具、事件文件完整跑通 |
| S2 | 事件流外化 | AgentRunner 搬进 daemon，CLI/TUI 通过 IPC 订阅同一份事件流 |
| S3 | 自主规划与 TUI | Agent 能用任务工具拆解复杂目标，TUI 展示完整执行过程 |
| Trace | 系统级时间线 | IPC / EventBus / LLM 三层数据流可追踪、可回放 |
| S4 | 会话与记忆 | 多轮 run 进入同一个 session，thread 和 notes 接住上下文 |
| S5 | 工具安全 | 工具调用前有参数校验、权限审批、失败分类和重试 |
| S6 | 上下文治理 | 长会话下有 context 水位、tool_result 截断和 compact |
| S7 | 扩展边界 | Skills、Subagents、MCP 让 Agent 可组织、可派生、可接外部工具 |

从第一章开始，项目就不是“先写一个脚本，后面再慢慢重构”。

KamaClaude 在 S0 就先把 `kama` CLI 和 `kama-core` daemon 拆开，通过 TCP NDJSON + JSON-RPC 2.0 通信。

这一步看起来比普通脚手架更重，但它换来的是后面所有能力都不用推倒重来：

* TUI 可以复用同一套 IPC
* 事件订阅可以复用同一套通道
* 权限审批可以通过事件推到前端
* trace 可以记录完整请求和响应
* 后续 Web 前端也可以接入同一个 Core

这就是工程项目里真正值钱的地方。

不是“能不能跑”，而是系统边界一开始就立住。

### 项目架构图

![](docs/images/20260610114820_KamaClaude架构图-分层版.png)

KamaClaude 的核心不是一个 prompt，而是一套完整的本地 Agent 运行链路：

```latex
用户目标
  → CLI / TUI
  → JSON-RPC over NDJSON
  → kama-core daemon
  → AgentRunner
  → AgentLoop
  → LLM Provider
  → ToolRegistry
  → PermissionManager
  → EventBus
  → Session Store
  → TUI 实时渲染 / events.jsonl 持久化 / trace 回放
```

你学完以后，面试官再问 AI Agent 项目，你就不是说：

“我调用了大模型 API。”

而是能说：

* 我实现了 ReAct AgentLoop 和工具调用闭环
* 我用 EventBus 把 Agent 执行过程外化成事件流
* 我实现了 TUI 实时渲染、工具折叠块、权限审批卡片
* 我实现了 Session、thread、notes 三层记忆体系
* 我实现了上下文水位检测、tool_result 截断、自动 compact 和手动 compact
* 我实现了 Skills、Subagents、MCP 外部工具接入
* 我用 pytest、mypy strict、ruff 保证项目质量
* 我实现了守护进程 + 多客户端架构
* 我设计了 JSON-RPC 2.0 + NDJSON 的类型化 IPC 协议

这就不是“AI 套壳项目”了。

这是一个能拿去讲系统设计、异步并发、协议建模、工具安全、上下文工程、多 Agent 编排的高质量项目。

### 项目亮点

![](docs/images/2026-06-10_11-48-11.jpg)

KamaClaude 最大的亮点，是把 Claude Code 这类 AI 编程 Agent 背后的核心机制，用一个 mini 版工程完整跑通：它不是单进程脚本，而是 `kama-core` daemon + CLI/TUI 多客户端架构；

不是一次性调大模型，而是 ReAct AgentLoop，支持模型思考、工具调用、结果回填和多步执行；

不是让模型说执行就执行，而是把工具调用放进 `ToolRegistry` 和 `PermissionManager`，先做参数校验、权限审批、失败分类，再把 tool result 返回给模型；

不是只展示最终答案，而是通过 `EventBus`、events、trace 和 TUI，把 token 流、工具调用、审批、上下文水位都实时展示并可回放；

不是简单拼接聊天历史，而是用 session、thread、notes、context 和 compact 做上下文治理；

最后还支持 Skills、Subagents、MCP，把工作流、子 Agent 和外部工具统一接进同一套运行链路。

也就是说，这个项目真正能讲的不是“我接了一个大模型接口”，而是“我实现了一个本地 Agent 运行时”。


### 这个项目适合谁？

如果你正在准备秋招、春招、实习、社招，想做一个 AI 项目，想了解Agent工作原理，这个项目很适合你。

如果你已经做过 RAG、聊天机器人、AI 助手，想把项目深度往 Agent 工程方向拔高，这个项目也很适合。

如果你想理解 Claude Code、Codex、Cursor 这类 AI 编程工具背后的运行时设计，这个项目同样值得系统学一遍。

它不是教你背概念。

**它是带你从 S0 到 S7，八个阶段，把一个本地 Agent 工具从零搭出来**。

每一章都有明确的执行路径，每一阶段都能运行、能验证、能留下文件证据。

你不是最后拿到一个黑盒项目。

你会知道它每一层为什么存在。

### 项目专栏

**本项目为文字专栏讲解方式，不过在项目环境配置，启动，使用上 给大家录制了视频**。

项目专栏把 简历写法、项目亮点、常见面试题 都准备好了，大家做完这个项目可以直接用。

![](docs/images/2026-06-10_12-11-45.jpg)

本项目分成8个阶段完成，每一阶段都有详细讲解：

S0、项目基础架构：

![](docs/images/2026-06-10_12-04-20.jpg)

S1、Agent 第一次运行

![](docs/images/2026-06-10_12-04-40.jpg)

S2、把事件流外化为 IPC

![](docs/images/2026-06-10_12-04-59.jpg)

S3、trace

![](docs/images/2026-06-10_12-05-39.jpg)

S3、Agent 的自主规划

![](docs/images/2026-06-10_12-05-39.jpg)

S4、把 Agent 变成会话伙伴

![](docs/images/2026-06-10_12-05-56.jpg)

S5、给工具加上安全锁

![](docs/images/2026-06-10_12-06-13.jpg)


S6、让上下文可控、可压缩、可续航

![](docs/images/2026-06-10_12-06-32.jpg)

S7、Skills、Subagents 与 MCP

![](docs/images/2026-06-10_12-06-51.jpg)


