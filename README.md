# KamaClaude (Windows 版)

本地 AI Agent 系统。`kama-core` 作为常驻守护进程处理所有任务，`kama`（CLI）和 `kama-tui`（TUI）通过 TCP loopback 与之通信。

本仓库基于上游 [youngyangyang04/KamaClaude](https://github.com/youngyangyang04/KamaClaude) 二次开发，
专注 **Windows 原生适配**（进程树管理、`cmd.exe` Shell Worker、Job Object 等），保留 MIT 许可证和原版权声明。

分支结构：`stage/s0`~`stage/s7`（上游功能线）→ `s8`（可恢复执行）→ `s9`（统一摘要 + 智能截断）。

## 环境要求

| 依赖 | 版本 |
|------|------|
| 操作系统 | **Windows 10/11**（主要） / macOS / Linux |
| Python | 3.12.x |
| [uv](https://docs.astral.sh/uv/) | ≥ 0.4 |

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

## 快速开始

```bash
# 1. 克隆并安装依赖
git clone https://github.com/gandi0/windows-kamaclaude.git
cd windows-kamaclaude
uv sync

# 2. 配置环境变量
cp .env.example .env        # Windows: copy .env.example .env
# 编辑 .env 填入你的 API Key 和 base_url

# 3. 启动守护进程
uv run kama-core            # Windows: 直接运行（无 & 后台符号）
                            # macOS/Linux: uv run kama-core &

# 4. 验证
uv run kama ping            # 应返回 pong
uv run kama --version
uv run kama-tui             # 启动 TUI 交互界面
```

## 运行测试

```bash
# 单元测试（不需要 API Key）
uv run pytest tests/unit/ -q

# S8.5 故障矩阵（覆盖 20 种崩溃场景）
uv run python scripts/s8/run_s85_matrix.py --output docs/s8/results/matrix.json

# S8.3 恢复 + S8.4 压缩集成测试
uv run pytest tests/integration/test_s83_recovery.py tests/integration/test_s84_compaction.py -v
```

## 功能特性

### S0~S7 — 上游基础功能

对话管理、工具系统、技能系统、MCP 协议、子 Agent 嵌套、TUI 交互等。详见各 `stage/s*` 分支。

### S8 — 可恢复执行 ✅

| 能力 | 说明 |
|------|------|
| **SQLite 执行记录** | 每次 run/step/tool_call 全持久化 |
| **跨重启恢复** | daemon 崩溃或电脑重启后，`kama resume` 从断点继续 |
| **安全暂停** | Shell/写/MCP 等未知副作用的工具结果不明时，自动暂停等待人工核查 |
| **Windows Job Object** | Shell Worker 注册到 Job Object，父进程挂时子进程被干净清理 |
| **Shell Worker 进程隔离** | 独立 Python 子进程执行 `cmd.exe`，支持超时取消 + 进程树清理 |

- [S8 验证报告](./docs/s8/VALIDATION.md) — 故障矩阵 20/20 通过
- [S8 迁移说明](./docs/s8/MIGRATION.md)
- [S8 已知限制](./docs/s8/KNOWN_LIMITATIONS.md)

### S9 — 统一摘要 + 智能截断 ✅

| 能力 | 说明 |
|------|------|
| **统一摘要表** | `summaries` 表加 `summary_kind` 列，compactor (`handoff`) 和 skill (`human_readable`) 共存 |
| **Summarize Skill 自动持久化** | `/summarize` 执行完后自动写入 summaries 表 |
| **Tool Result 截断** | 超过 8000 字符的工具输出自动截断为前 4000 + 后 2000，避免爆 context |
| **双重截断保护** | bash worker 端 64KB 硬截断 + loop 端 8KB 智能截断 |

## 文档

- **[RUNBOOK.md](./RUNBOOK.md)** — 完整操作参考：配置、开发命令、故障排查
- **[WIRE_PROTOCOL.md](./WIRE_PROTOCOL.md)** — IPC 协议定义（由代码生成，勿手动编辑）
- **[docs/s8/](./docs/s8/)** — S8 可恢复执行全部文档
- **[docs/s9/](./docs/s9/)** — S9 统一摘要 + 智能截断文档

## 安全说明

- `.env`（API Key）、`.venv/`、`runs/`、`workspace/` 均在 `.gitignore` 中，**不会被提交**
- 源码零硬编码 API Key 或绝对路径
- 所有用户路径通过 `expanduser()` 适配，跨平台通用
