# 🤖 KamaClaude (Windows 版)

> 本地 AI Agent 系统，守护进程 + TUI 双端架构，跨重启可恢复执行

<div align="center">

[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D4.svg)](https://www.microsoft.com/windows)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![SQLite](https://img.shields.io/badge/Persistence-SQLite-003B57.svg)](https://www.sqlite.org/)

✨ **s8 分支聚焦：SQLite 持久化 · 跨重启恢复 · Windows 原生进程隔离**

</div>

---

## 📌 当前分支：`s8` — 可恢复执行

本仓库基于上游 [youngyangyang04/KamaClaude](https://github.com/youngyangyang04/KamaClaude) 二次开发，
`stage/s0`~`stage/s7` 为上游基础功能线，**本分支 s8 在此之上增加执行持久化与故障恢复能力**。

```
stage/s0 ──→ stage/s1 ──→ ... ──→ stage/s7 ──→ s8 ──→ (s9 后续迭代)
  上游：对话/工具/MCP/Skills/TUI    ← 本分支：SQLite + 恢复 + Windows 适配
```

---

## 🏗️ 架构概览

```
┌─────────────┐     TCP :7437      ┌─────────────────────┐
│  kama-tui   │ ◄────────────────► │    kama-core (daemon) │
│  (TUI 客户端) │                   │                     │
└─────────────┘                    │  AgentLoop (推理循环)  │
                                   │     │               │
┌─────────────┐     Unix Socket    │     ▼               │
│  kama (CLI)  │ ◄────────────────► │  ToolRouter (工具路由)│
└─────────────┘                    │     │               │
                                   │     ▼               │
                                   │  Shell Worker ◄─ Job Object
                                   │  (独立进程树)         │
                                   │     │               │
                                   │     ▼               │
                                   │  SQLite 持久化层      │
                                   │  sessions / runs     │
                                   │  messages / tool_calls
                                   │  checkpoints / summaries
                                   └─────────────────────┘
```

---

## 🧠 s8 核心功能

### 💾 SQLite 执行持久化层

原来 JSON 文件散落各处，现在**十二张表全量记录每次对话**：

```sql
sessions    -- 会话元数据
runs        -- 每次运行（含 daemon_epoch）
messages    -- 完整对话历史（system / user / assistant / tool）
tool_calls  -- 每次工具调用的参数 + 结果
checkpoints -- 可恢复快照 + CAS 乐观锁
summaries   -- compactor 压缩摘要
daemon_state -- daemon epoch + 心跳
```

> 为什么选 SQLite？LangGraph 官方 `SqliteSaver`、CrewAI `SqliteProvider` 都是业界共识方案。
> 2026 Zylos Research 调研报告明确指出：**"AI Agent ecosystem has converged on SQLite"** —— 零配置、单文件、WAL 模式 2 万+ 写/秒，完美匹配本地 agent。

### 🔄 跨重启恢复

```bash
# daemon 崩了 / 电脑重启了
uv run kama resume          # 从最近 checkpoint 续跑
```

- ✅ **已完成的工具结果直接复用**，不重放
- ✅ **恢复点精确到 step**，不会丢上下文
- ✅ **20 种崩溃场景故障矩阵全部通过**

### 🛡️ 副作用分级 + 安全暂停

不是所有工具都能安全重放：

| 工具类型 | 示例 | 重放策略 |
|---------|------|---------|
| 🔵 纯读 | `echo hello`、`cat file` | 可安全重放 |
| 🟡 有副作用 | `git push`、写文件、发请求 | 结果 unknown 时**自动暂停**等人审 |

daemon 被强杀时，正在执行的 shell 命令（比如 `ping -t`）可能状态不明——此时 TUI 弹提示，让用户选 `Pause` / `Abandon` / `Replay`。

### 🪟 Windows Shell Worker + Job Object

这是 s8 最复杂的 Windows 适配。解决了原 macOS/Linux 版本的三个问题：

```
❌ 原问题 1：subprocess.Popen 不做进程树管理
   → daemon 崩了，shell 子进程可能残留成僵尸

❌ 原问题 2：没有超时取消机制
   → 卡死的 shell 命令无法干净 kill

❌ 原问题 3：依赖 /bin/sh，Windows 用户无 bash 会报错
   → 必须装 Git Bash 或 WSL 才能跑
```

**s8 方案（三层防护）：**

```
┌────────────────────────────────────────────────┐
│  Windows Job Object                              │
│  ┌──────────────────────────────────────────┐  │
│  │  JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE       │  │
│  │  父进程持有的 Job 句柄关闭 → 内核自动       │  │
│  │  kill Job 内所有进程（包括子子进程）        │  │
│  └──────────────────────────────────────────┘  │
│          ▲          ▲          ▲                 │
│          │          │          │                 │
│    python worker  cmd.exe   git.exe / npm ...  │
│    (_shell_worker.py)                           │
└────────────────────────────────────────────────┘
```

**关键代码** — [_shell_worker.py](src/kama_claude/core/tools/_shell_worker.py)：

```python
def main() -> int:
    if os.name == "nt":
        join_job(sys.argv[1])    # 启动时先加入父进程的 Job，消除竞态
    # ... 握手等待 ...
    if os.name == "nt":
        shell = os.path.join(os.environ["SystemRoot"],
                             "System32", "cmd.exe")   # 只认系统 cmd.exe
        process = subprocess.Popen(
            f'"{shell}" /d /s /c "{command}"',
            stdin=subprocess.DEVNULL
        )
    else:
        process = subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdin=subprocess.DEVNULL
        )
    return process.wait()
```

---

## ⚙️ 环境要求

| 依赖 | 版本 |
|------|------|
| 🖥️ 操作系统 | **Windows 10/11**（主要） / macOS / Linux |
| 🐍 Python | 3.12.x |
| 📦 [uv](https://docs.astral.sh/uv/) | ≥ 0.4 |

### 安装 uv

**Windows（PowerShell）：**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS / Linux：**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Python 3.12 由 uv 自动管理，无需手动安装。

---

## 🚀 快速开始

```bash
# 1. 克隆并安装依赖
git clone https://github.com/gandi0/windows-kamaclaude.git
cd windows-kamaclaude
git checkout s8
uv sync

# 2. 配置环境变量
copy .env.example .env           # Windows cmd: copy
# 编辑 .env 填入你的 API Key 和 base_url

# 3. 启动守护进程
uv run kama-core                 # Windows: 直接运行（无 & 后台符号）

# 4. 验证
uv run kama ping                 # 应返回 pong
uv run kama --version
uv run kama-tui                  # 启动 TUI 交互界面

# 5. 崩溃恢复测试（可选）
#   启动 agent 执行长任务 → 强杀 daemon → 重开 → uv run kama resume
```

---

## 🧪 运行测试

```bash
# 单元测试（不需要 API Key）
uv run pytest tests/unit/ -q

# S8.5 故障矩阵 —— 20 种崩溃场景全部通过
uv run python scripts/s8/run_s85_matrix.py --output docs/s8/results/matrix.json

# S8.3 恢复 + S8.4 压缩集成测试
uv run pytest tests/integration/test_s83_recovery.py tests/integration/test_s84_compaction.py -v
```

### 故障矩阵覆盖的场景

| 场景类别 | 数量 | 示例 |
|---------|------|------|
| 🟢 自动恢复 | 6 | daemon kill -9、断电模拟、网络断开 |
| 🟡 安全暂停 | 10 | 工具结果 unknown、MCP 超时、shell 卡死 |
| 🔴 终止失败 | 4 | checkpoint 冲突、数据库损坏 |

---

## 🗂️ 目录结构

```
KamaClaude/
├── src/kama_claude/
│   ├── core/
│   │   ├── session/
│   │   │   ├── execution.py   ← SQLite 持久化层（十二张表 + CAS 锁）
│   │   │   ├── store.py       ← 存储接口代理
│   │   │   └── manager.py     ← Session 生命周期管理
│   │   ├── tools/
│   │   │   ├── _shell_worker.py   ← Shell Worker（跨平台）
│   │   │   ├── windows_job.py     ← Windows Job Object 封装
│   │   │   └── process.py         ← 进程管理（超时 + 取消）
│   │   ├── loop.py            ← Agent Loop（推理循环）
│   │   └── runner.py          ← Daemon 入口
│   └── tui/                   ← TUI 客户端
├── scripts/s8/                ← S8 故障矩阵脚本
├── tests/
│   ├── unit/                  ← 单元测试
│   └── integration/           ← S8.3/S8.4 集成测试
└── docs/s8/                   ← S8 文档
```

---

## 📚 相关文档

- [S8 STATUS.md](./docs/s8/STATUS.md) — 设计目标与验收标准
- [S8 VALIDATION.md](./docs/s8/VALIDATION.md) — 故障矩阵验证报告（20/20）
- [RUNBOOK.md](./RUNBOOK.md) — 完整操作参考
- [WIRE_PROTOCOL.md](./WIRE_PROTOCOL.md) — IPC 协议定义

---

## 🔒 安全说明

| 检查项 | 状态 |
|--------|------|
| `.env`（API Key）在 `.gitignore` 中 | ✅ 不会被提交 |
| `.venv/`、`runs/`、`workspace/` 在 `.gitignore` 中 | ✅ 不会被提交 |
| 源码零硬编码 API Key 或绝对路径 | ✅ 全部 `expanduser()` + `Path()` |
| Shell Worker 不执行 eval/exec 用户输入 | ✅ 只做命令转发 |
| Daemon 监听 127.0.0.1（loopback only） | ✅ 不暴露到公网 |

---

## 🤝 分支说明

| 分支 | 定位 | 说明 |
|------|------|------|
| `main` | 最新稳定版 | 包含 s0~s9 全部功能 |
| **`s8`** | **可恢复执行** | **当前分支** — SQLite 持久化 + 跨重启恢复 + Windows 适配 |
| `s9` | 统一摘要 + 智能截断 | summaries 表合并 + tool_result 自动截断 |
| `stage/s0` ~ `stage/s7` | 上游功能快照 | 对话/工具/MCP/Skills/TUI 基础能力 |

---

## 📄 License

基于上游 [MIT License](LICENSE) 二次开发。保留原版权声明，新增改动部分同样以 MIT 授权。
