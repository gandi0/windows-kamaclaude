# 🤖 KamaClaude · Stage S3

> 让 Agent 学会自主规划 —— 任务拆解、依赖管理、八工具体系与可观测 Trace

<div align="center">

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Asyncio](https://img.shields.io/badge/Async-asyncio-2E8B57)](https://docs.python.org/3/library/asyncio.html)
[![TUI](https://img.shields.io/badge/TUI-Textual-1E1E1E)](https://github.com/Textualize/textual)
[![Pydantic](https://img.shields.io/badge/Validation-Pydantic-E92063)](https://docs.pydantic.dev/)
[![Stage](https://img.shields.io/badge/Stage-s3-FF8C00)](./docs/S3-Agent-Planning.md)

**自主规划 · 文件 CRUD · 依赖级联 · 流式 TUI · 统一 Trace**

</div>

---

## 📌 本分支：`stage/s3`

基于上游 S2（daemon + IPC + AgentLoop），**S3 引入自主规划能力和可观测基础设施**。这是 Agent 从"只会执行"到"会自己拆任务"的关键跃迁。

```
S1 单进程闭环  →  S2 双进程 daemon/IPC  →  S3 自主规划 + Trace（← 本分支）  →  S4+...
                                        │
                                        ├── 🧠 TaskManager + 任务工具
                                        ├── 🛠️ 八工具体系（4 任务 + 4 执行）
                                        ├── 🪟 Textual TUI（流式 + 折叠详情）
                                        └── 📍 统一 Trace 系统
```

---

## 🧠 S3 核心新增

### 1. Agent 自主规划

LLM 可以调用**任务工具**来拆计划、声明依赖、标记进度：

| 工具 | 类型 | 作用 |
|------|------|------|
| `task_create` | 📋 任务 | 创建任务并声明 `blocked_by` 依赖 |
| `task_update` | 📋 任务 | 更新状态（pending → in_progress → completed） |
| `task_list` | 📋 任务 | 查看当前计划摘要 |
| `task_get` | 📋 任务 | 读取单个任务详情 |
| `read_file` | 🔧 执行 | 读取文件内容 |
| `list_dir` | 🔧 执行 | 浏览目录结构 |
| `write_file` | 🔧 执行 | 创建或更新文件 |
| `bash` | 🔧 执行 | 运行非交互 Shell 命令 |

> 💡 关键设计：**任务也是 Tool**。这样 LLM 用同一套 tool_use 机制读/写计划，不需要额外的 prompt engineering 技巧。

### 2. 依赖级联解除

任务完成后，`TaskManager` 自动从其他任务的 `blocked_by` 列表中移除该任务 ID：

```
任务 1 完成 → 任务 2 的 blocked_by=[1] 变成 []（解除阻塞）
           → 任务 3 的 blocked_by=[1,2] 变成 [2]（仍需等任务 2）
```

> ⚠️ 自动级联**只解除依赖**，不会自动把后续任务设为 `in_progress`——下一步仍由 LLM 决定。

### 3. 统一 Trace 系统

两条时间线交叉验证：

| 文件 | 视角 | 回答的问题 |
|------|------|-----------|
| `runs/<run_id>/events.jsonl` | 单次任务 | 这次任务做了什么？ |
| `~/.kama/traces/daemon.jsonl` | daemon 全局 | 数据从哪来、经哪些层、最终去哪？ |

五个方向覆盖完整数据流：

```
CLIENT→CORE  ←→  CORE→CLIENT     (IPC 命令/响应)
CORE                              (EventBus 内部事件)
CORE→LLM     ←→  LLM→CORE        (模型调用/响应)
```

### 4. Textual TUI

- ✅ **流式输出原地累积**：同一段回复始终更新同一个 `LLMStreamBlock`，不膨胀组件树
- ✅ **工具调用块可折叠**：默认只显示工具名 + 状态 + 耗时，点击展开看参数和完整输出
- ✅ **Markdown 延迟渲染**：流式阶段显示纯文本，块结束后统一渲染 Markdown，避免闪烁

---

## 🏗️ 整体架构

```
┌─────────────┐     TCP :7437      ┌─────────────────────────┐
│  kama-tui   │ ◄────────────────► │    kama-core (daemon)    │
│  (Textual)  │                    │                         │
└─────────────┘                    │  ┌─ CoreApp（组件组装）  │
                                   │  │                       │
┌─────────────┐     Unix Socket    │  │  ├─ SocketServer      │
│  kama (CLI)  │ ◄────────────────► │  │  ├─ EventBus          │
└─────────────┘                    │  │  ├─ TraceWriter        │
                                   │  │  └─ AgentRunner        │
                                   │  │       │                │
                                   │  │       ▼                │
                                   │  │  AgentLoop（推理循环）  │
                                   │  │       │                │
                                   │  │       ▼                │
                                   │  │  ToolRegistry          │
                                   │  │   ├─ 4 任务工具 ─→ TaskManager (.tasks/*.json)
                                   │  │   └─ 4 执行工具 ─→ 文件系统 / 子进程
                                   │  │                        │
                                   │  │  TracingProvider（包装 LLMProvider）
                                   │  └─────────────────────────┘
                                   └─────────────────────────┘
```

---

## 📚 深度阅读

| 文档 | 内容 | 适合谁 |
|------|------|--------|
| [📋 S3 自主规划深度指南](./docs/S3-Agent-Planning.md) | Run 启动、TaskManager、blocked_by 级联、Bash 工具、流式 TUI、错误分层排查 | 想彻底理解 Agent 内部工作原理 |
| [📍 S3 Trace 系统深度指南](./docs/S3-Trace-System.md) | TraceRecord 格式、TraceWriter 队列、四个埋点、Wrapper 模式、daemon 全局时间线 | 想学习如何为 Agent 系统增加可观测性 |

---

## 🧪 运行

```bash
# 启动 daemon
uv run kama-core

# 另开终端，发一个需要规划的复杂目标
uv run kama-tui
# 输入: "分析 src/kama_claude/core 目录结构，找出可以拆分的模块"

# 查看 Trace
uv run kama trace --follow
uv run kama trace --layer llm --raw | jq 'select(.kind == "api_call")'
```

---

## 🗂️ S3 新增/修改的关键文件

| 文件 | 职责 |
|------|------|
| `src/kama_claude/core/task/model.py` | Task / TaskStatus 数据模型 |
| `src/kama_claude/core/task/manager.py` | 文件 CRUD + 依赖级联 |
| `src/kama_claude/core/tools/builtin/task_*.py` | 四个任务工具的 schema 与 invoke |
| `src/kama_claude/core/tools/builtin/bash.py` | 子进程、超时、退出码、编码 |
| `src/kama_claude/core/trace/writer.py` | TraceWriter + asyncio.Queue 后台写入 |
| `src/kama_claude/core/trace/provider.py` | TracingProvider（Wrapper 模式） |
| `src/kama_claude/core/app.py` | CoreApp 串联所有组件（构造函数注入） |
| `src/kama_claude/core/runner.py` | AgentRunner 组装 TaskManager + ToolRegistry |
| `src/kama_claude/tui/app.py` | Textual TUI（LLMStreamBlock + ToolCallBlock） |

---

## 🐛 常见坑速查

| 症状 | 可能原因 | 查哪里 |
|------|---------|--------|
| `bash ✗ [exit 255] 找不到文件 head` | Windows 用 cmd.exe 但模型生成了 Bash 语法 | `BashTool` 的 shell 启动参数 |
| `HTTP 200 OK` 但模型没生成 tool_use | 模型层问题，不是网络问题 | Trace 的 `api_call` messages 和 `api_response` |
| 任务创建成功但 `task_list` 看不到 | 四个工具没共享同一个 TaskManager 实例 | `AgentRunner._build_registry()` |
| TUI 工具块点不开 | 工具还在执行中（`_finished=False`） | `ToolCallBlock.on_click` |
| daemon 重启后任务丢失 | S3 只有文件 CRUD，还没有 SQLite 持久化 | 后续 S8 解决 |

---

## 🤝 分支说明

| 分支 | 定位 |
|------|------|
| `stage/s0` ~ `stage/s2` | S3 的前置基础能力（单进程 loop → 双进程 daemon/IPC） |
| **`stage/s3`** | **当前分支 — 自主规划 + Trace + 八工具体系** |
| `stage/s4` ~ `stage/s7` | 后续阶段 |
| `s8` | Windows 二次开发：SQLite 持久化 + 跨重启恢复 |
| `s9` | Windows 二次开发：统一摘要 + Tool Result 截断 |
