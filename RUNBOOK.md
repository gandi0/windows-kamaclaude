# 运维手册（RUNBOOK）

## S8 Windows 本地开发

当前 S8 分支位于 `F:\exercise\2026\kamaclaude - v1\KamaClaude`，从本地 `stage/s7` 创建。
沿用此仓库已有的 Python 3.12.14 `.venv` 和 `.python-version=3.12`，没有重建环境。
S8.1 已通过 Windows 11/Python 3.12 全量及现有 Python 3.13 专项测试；Linux 未实测。
支持声明为 `>=3.12,<3.14`。在项目根目录 PowerShell 执行：

```powershell
# 使用现有虚拟环境和源码安装；已运行的旧 daemon 需先 Ctrl+C 退出
.venv/Scripts/kama-core.exe
# 另开终端检查连通；前台 daemon 可用 Ctrl+C 关闭
.venv/Scripts/kama.exe ping
.venv/Scripts/kama-tui.exe
```

daemon 启动、ping、会话创建与查看不需要模型 Key；真正模型调用仍需配置。
缺 Key 的 run 会明确失败，daemon 保持运行。不要用真实 API 验证机制测试：

```powershell
.venv/Scripts/python.exe scripts/s8/check_baseline.py --check s81 --output docs/s8/evidence/s81-local
.venv/Scripts/python.exe scripts/s8/probe_windows.py
```

工具 `bash` 在 Windows 使用 `cmd.exe /d /s /c`，不能直接套用 POSIX `sleep`、
`export` 等语法。每次 Shell 运行属于独立 Job Object，命令结束、超时和取消都会
清理所属进程树，因此不能用该工具留下长期后台进程。任意 Shell/写工具默认不重试；
超时或无法确认结果时当前 run 停止，TUI 显示 `needs review`。S8.3 的恢复与核查入口见下文；
未知副作用不会被自动重放。

完整基线、已知限制与下一阶段入口见 [S8 状态](docs/s8/STATUS.md)。

## S8.2 本地执行记录

新会话元数据、消息、工具调用与检查点写入 `~/.kama/sessions/execution.sqlite3`。
旧 `meta.json/thread.jsonl` 保留，不自动改写或导入。事件 JSONL 只用于诊断。
数据库须放本机专用目录；运行中备份请用 SQLite backup API，不要只复制主 DB 而遗漏 WAL。

无需模型 Key 的验证命令（仍使用已有 Python）：

```powershell
.venv/Scripts/python.exe scripts/s8/check_baseline.py --check s82 --output docs/s8/evidence/s82-local
.venv/Scripts/python.exe scripts/s8/check_baseline.py --check s82-crash --output docs/s8/evidence/s82-crash-local
```

需要离线导入旧会话时，显式指定旧会话目录和绝对数据库路径：

```powershell
.venv/Scripts/python.exe scripts/s8/import_legacy.py C:/kama-test/old-session --database C:/kama-test/sessions/execution.sqlite3
```

导入保留源 bytes，重复导入不重复生成消息；源重写会隔离快照并返回 conflict。
缺失 workspace 的旧会话保持未绑定。S8.3 可在 TUI 列表查看其历史，但不能把导入记录当作可自动执行的任务。

工具结果提交失败会停止当前执行并报告 needs_review/storage_error；数据库可能仍停在 dispatching，
不得据此重发同一工具。S8.3 重启扫描会据已提交事实分类；结果未知时暂停核查。
手动 `/compact` 使用 SQLite 持久摘要，原始记录仍只追加且不会删除。

## S8.3 TUI 恢复操作

升级前停止旧 daemon，并使用 SQLite backup API 备份数据。启动新 daemon 时自动把 v1/v2 库迁移
到 v3，不删除会话。旧版程序不能打开 v3 库。相同数据目录只能有一个 daemon，换端口不能
绕过数据锁；不要删除正在使用的锁文件或运行中的数据库。

在原工作目录打开 `kama-tui`，使用以下操作：

1. `Ctrl+O` 或 `/sessions` 打开持久会话列表，用方向键和 Enter 选择旧会话。
2. `Ctrl+S` 或 `/status` 查看当前任务、完整调用参数、目录、attempt 和核查原因。
3. 对 interrupted 任务点击 Continue，或输入 `/resume`。已提交工具结果会复用；未派发操作
   会重新检查当前权限，出现新的审批时按界面选择。TUI 的当前目录必须匹配持久工作区。
4. 对 needs_review 任务先阅读完整事实。填写说明后选择 Pause 保持暂停，或选择 Abandon
   放弃任务并关闭该会话。放弃保留历史和未知操作，**不撤销已产生的副作用**。

文件发生外部变化、workspace 身份改变或 junction 换向时，界面给出阻塞原因。确认并恢复
原目录/相关文件后可再次请求恢复；不能简单改库跳过检查。迁移前没有目录身份证据的旧中断
任务不会自动取得新身份，需保留核查或放弃。首版不提供未知调用的一键重试/人工成功结果注入。
后台子任务可查看父子关系和已提交结果，重启后中断的 child 不会自动继续。

TUI 为每次用户输入维护业务 request_id，网络重试保留该 ID 和原 session。脚本客户端调用
`session.send_message` 也应这样做；该方法在持久接受后返回 run_id，完成由事件或 status 查询。
`permission.respond` 必须回传当前 `permission.requested` 的 approval_id，旧 token 不能使用。
详细字段见生成的 [WIRE_PROTOCOL.md](WIRE_PROTOCOL.md)。

免 API 检查使用现有解释器、独立 profile、假模型和专用测试目录：

```powershell
.venv/Scripts/python.exe scripts/s8/check_baseline.py --check s83 --output docs/s8/evidence/s83-local
.venv/Scripts/python.exe scripts/s8/probe_s83.py --output docs/s8/evidence/s83-local/daemon.json
.venv/Scripts/python.exe scripts/s8/check_baseline.py --output docs/s8/evidence/s83-full-local
```

真实 daemon 测试只强制结束夹具自己创建的进程；不要把测试 profile 换成真实用户数据目录。
TUI 演示证据和准确检查结果见 [S8 状态](docs/s8/STATUS.md)。

## S8.4 持久压缩

`/compact` 成功后，响应包含摘要 ID、版本和覆盖的消息 seq 范围。SQLite 中的原始消息不会被
摘要替换；daemon 重启后会用有效摘要和未覆盖尾部重建模型上下文。`summary_<id>.md` 只是诊断
附件，丢失或写入失败不影响 SQLite 权威状态。自动压缩默认仍关闭；只有显式配置非零
`compaction.auto_threshold` 或 `KAMA_COMPACT_THRESHOLD` 才启用。

升级前停止旧 daemon，并用 SQLite backup API 备份数据库及 WAL/SHM。v3 数据库不能由旧程序
安全打开。压缩失败时不要手工修改 checkpoint 或 summaries 表；旧有效摘要和原始历史会保留。
发现未配对工具调用时先按 S8.3 核查流程处理，不能用压缩绕过 unknown 副作用。

免 API 的 S8.4 专项和完整检查：

```powershell
.venv/Scripts/python.exe scripts/s8/check_baseline.py --check s84 --output docs/s8/evidence/s84-local
.venv/Scripts/python.exe scripts/s8/check_baseline.py --output docs/s8/evidence/s84-full-local
```

专项包含真实 CoreApp 子进程的停止、重启和 TCP 会话验证，只使用固定假 provider 与临时 profile。
S8.5 已完成最终矩阵、测量和发布准备，结果见下节。

## S8.5 最终验证与恢复处置

一条命令运行免 API 故障矩阵：

```powershell
.venv/Scripts/python.exe scripts/s8/check_baseline.py --check s85 --output docs/s8/evidence/s85/matrix-local
```

daemon 重启只扫描并分类旧 run，不会无人值守继续。`interrupted` 且没有未知副作用时，可在原
workspace 通过 TUI Continue 或 `session.resume` 显式恢复；恢复会重新核对 workspace、文件状态
和审批，并复用已提交结果。`needs_review` 必须检查完整 call/attempt/workspace 事实：Pause 保持
暂停并记录说明，Abandon 终止后续安排但不会撤销外部副作用。不得直接改库把 unknown 标成功。

升级或迁移前停止旧 daemon，使用 SQLite backup API 备份数据库及 WAL/SHM。schema v1/v2 会
自动迁移到 v3；v3 不支持用旧程序回退打开。旧 JSONL 只能通过显式导入脚本无损导入。详细步骤
见 [MIGRATION.md](docs/s8/MIGRATION.md)，已知边界见
[KNOWN_LIMITATIONS.md](docs/s8/KNOWN_LIMITATIONS.md)。

真实 daemon/TUI 演示：

```powershell
.venv/Scripts/python.exe scripts/s8/probe_s83.py --output docs/s8/evidence/s85/demo-local/probe.json
```

该探针使用随机 loopback 端口、固定假 provider 和专用临时 profile，只结束自己创建的子进程。
不要把输出改到真实 `~/.kama` 或项目 `.test-tmp`。完整结果和性能口径见
[VALIDATION.md](docs/s8/VALIDATION.md)。

## 日常操作

### 启动守护进程

```bash
uv run kama-core
```

默认监听 `127.0.0.1:7437`，按 `Ctrl+C` 优雅退出。

### 验证连通

```bash
uv run kama ping
# → pong server=0.0.1 uptime=12ms latency=2ms
```

### 停止守护进程

```bash
kill $(pgrep -f kama-core)
```

---

## 配置

优先级（低 → 高）：**内建默认值 → `~/.kama/config.toml` → `.env` → 系统环境变量**。

### `~/.kama/config.toml`

```toml
[core]
host = "127.0.0.1"
port = 7437

[logging]
level  = "INFO"
file   = "~/.kama/logs/core.log"
format = "text"    # "text" | "json"
```

### `.env`

从 `.env.example` 复制后修改，存放本机配置与密钥（不提交 git）：

```bash
cp .env.example .env
```

### 系统环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `KAMA_CONFIG` | `~/.kama/config.toml` | 覆盖配置文件路径 |
| `KAMA_HOST` | `127.0.0.1` | TCP 监听地址 |
| `KAMA_PORT` | `7437` | TCP 监听端口 |
| `KAMA_LOG_LEVEL` | `INFO` | 日志级别（DEBUG / INFO / WARNING / ERROR） |
| `KAMA_LOG_FILE` | `~/.kama/logs/core.log` | 日志文件路径（留空则仅输出 stderr） |
| `KAMA_LOG_FORMAT` | `text` | 日志格式（`text` 或 `json`） |

---

## 开发

```bash
uv run ruff check src tests scripts   # lint
uv run mypy src                       # 类型检查
uv run pytest tests/ -v               # 全量测试
uv run pytest tests/unit/ -v         # 仅单元测试（无需启动 daemon）

make docs                             # 重新生成 WIRE_PROTOCOL.md
make verify-s0                        # 完整验证（lint + 类型 + 测试 + 协议同源检查）
```

---

## 日志

```bash
tail -f ~/.kama/logs/core.log
```

---

## 常见错误

| 报错 | 原因 | 处理 |
|------|------|------|
| `core already running at 127.0.0.1:7437` | 已有守护进程在运行 | `kill $(pgrep -f kama-core)` |
| `core not running` | 未启动守护进程 | `uv run kama-core` |
| `Address already in use` | 端口被其他进程占用 | `KAMA_PORT=8000 uv run kama-core` |
| `Config error: KAMA_PORT must be an integer` | `.env` 或环境变量中端口值非整数 | 检查 `KAMA_PORT` 的值 |
