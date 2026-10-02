# S8 状态交接

更新：2026-09-23。**S8.0 已完成并停止；S8.1 尚未开始。**

## 当前目标与现场

本轮完成独立检出、环境基线、F01–F05 最小实验、Windows 启动探测及后续设计。用户最新约束为本地新分支开发、Windows 原生优先、使用现有 Python；不再以 WSL2 可用为前置条件。

- 工作目录：`F:\wend\ChatGPT\kamaclaude优化\KamaClaude`。
- 基线 / 本地分支：`a7adac2c3ecb5f66460d006840aea1e5743db45f` / `codex/s8-reliability`。
- 原任务书：父目录 `S8_IMPLEMENTATION_TASK.md`，未修改；用户首次给出的子目录路径不存在。
- 初始克隆时上游 main 与指定基线一致；用户要求本地开发后无 GitHub 仓库访问。无新提交、push、PR、发布。
- 实际解释器：已有 Python 3.13.0 的本地 `.venv`。原项目仍声明 Python `>=3.12,<3.13`，这轮通过 PYTHONPATH 测试源码，未宣称标准安装已兼容 3.13。

## 实际文件和行为变化

| 新增文件 | 作用 |
| --- | --- |
| `docs/s8/BASELINE.md` | 现场、版本、检查命令/结果、实验与未验证范围 |
| `docs/s8/DESIGN.md` | Windows 优先方案；SQLite、状态机、事务、恢复、权限、工作区、压缩、导入决策 |
| `docs/s8/STATUS.md` | 本阶段交接与下一阶段入口 |
| `scripts/s8/check_baseline.py` | 临时 Windows 用户环境内运行原有检查，剔除密钥，输出完整日志和退出码 |
| `scripts/s8/reproduce.py` | F01–F05 的真实组件实验、固定 Provider、正常对照和硬退出窗口 |
| `scripts/s8/probe_windows.py` | 假 Provider 下验证真实 CoreApp 的 Windows 信号启动障碍 |
| `docs/s8/evidence/` | 安装记录、依赖快照、原有检查/最终复查、隔离压缩测试、两次实验及 Windows 堆栈 |

变更均为新增的文档与测试/诊断脚本。`src/`、原有 `tests/`、`pyproject.toml`、`.python-version`、`WIRE_PROTOCOL.md` 无 diff；没有恢复、重试或压缩生产修复。新增脚本中的故障注入只从显式实验入口运行，正常产品入口不导入它们。

本地非源码产物：项目 `.venv/` 与检查缓存受上游 ignore 规则保护；父工作区 `.s8-env/uv-cache` 用于依赖缓存，保留以便继续本地开发。未删除已有文件，未改全局 Python、用户配置或系统服务。新文档/脚本仍未跟踪，未暂存。

## 验证命令、结果与证据

以下在项目根目录 PowerShell 执行；检查脚本内部使用临时配置目录和绝对 PYTHONPATH。

```powershell
.venv/Scripts/python.exe scripts/s8/check_baseline.py --output docs/s8/evidence/final
$env:PYTHONPATH = (Resolve-Path src).Path
.venv/Scripts/python.exe scripts/s8/reproduce.py --output docs/s8/evidence/reproduction-repeat.json
.venv/Scripts/python.exe scripts/s8/probe_windows.py --output docs/s8/evidence/windows-startup.json
.venv/Scripts/python.exe -m ruff check scripts/s8
.venv/Scripts/python.exe -m compileall -q scripts/s8
git diff --check
git diff --quiet -- src tests pyproject.toml .python-version WIRE_PROTOCOL.md
```

| 检查 | 实际结果 |
| --- | --- |
| 原有 unit | **255 passed，7 failed**；一次 Shell 平台命令失败、六次当前 event loop 缺失 |
| 原有免付费 integration | **3 passed，7 errors**；均为 daemon 启动要求 Key；真实 API 文件显式排除 |
| 原有 Ruff | **48 errors**，均在上游已有文件；未自动修复 |
| mypy / 协议同源 | **通过**，mypy 检查 85 个源码文件 |
| 原有 compactor 文件单独执行 | **6 passed**，完整命令见 BASELINE；证明整组失败具有顺序/loop 生命周期依赖 |
| F01–F05 | 五项全部复现，使用全新临时数据复跑后结果相同，两次退出 0 |
| Windows CoreApp 探测 | 假 Provider 下明确复现 `add_signal_handler` 的 `NotImplementedError`；脚本退出 0 表示障碍复现 |
| 新增脚本 Ruff / compileall | **通过** |
| 生产 diff / whitespace | **无生产 diff**，`git diff --check` 通过；新增文件另外经过 lint/内容审查 |

证据入口：[最终检查](evidence/final/checks.json)、[复现结果](evidence/reproduction-repeat.json)、[Windows 启动](evidence/windows-startup.json)。逐项预期/实际及上游失败原因见 [BASELINE.md](BASELINE.md)。

实际发现：F01 新管理器无法访问仍在磁盘的会话；F02 写 marker 后硬退出，thread 仅保留用户消息且 meta 仍 active；F03 一次 Shell 调用计数 3 次；F04 **显式启用**自动压缩后摘要与后续结果漏存（默认关闭）；F05 同会话下一轮失去已完成子任务查询入口。

## 验收范围与尚未验证部分

S8.0 的基线、设计、可重复 F01/F03/F04、F02 故障注入及 F05 最小验证均已交付。真实实验否定了“只是静态猜测”，但其范围有限：

- F01 是管理器重建，F02 是真实 Runner 子进程硬退出；**不是 daemon 中断—重启—恢复 E2E**。
- F05 只验证同进程跨 turn 的已完成任务查询；未验证重启中断展示/任务树接管。
- Windows daemon 目前存在无 Key 与信号接口两层启动问题；尚不能宣称 Windows 产品运行或恢复已支持。现有 TUI 单测通过，不等于实际 TUI 恢复演示。
- Python 3.13 超出原声明范围；未运行 3.12，也未通过标准 `uv sync`/`make verify-s0`。依赖按上游下限解析，已记录具体版本；失败不能外推到未知旧环境。
- 未运行付费模型、真实摘要质量评测、Linux/WSL2、断电/磁盘失败、SQLite 提交故障、进程树取消、MCP 副作用、权限恢复、并发恢复、工作区冲突或性能测试。
- 没有遗漏的必需 S8.0 实现工作；上述缺口为有证据的基线限制或后续阶段验收项，不能算作恢复成功。

## 设计取舍与兼容性

[DESIGN.md](DESIGN.md) 选定 SQLite 为后续唯一执行权威；结果/消息/checkpoint 同事务提交，Shell/未知 MCP 无结果时 needs_review，已知结果按 call_id 复用。原始消息稳定序号与可压缩工作上下文分离，旧 JSONL 无损幂等导入。一次性审批跨重启失效，同 session 主 run 通过数据库约束防重入。

Windows 默认 Shell 语义明确为 cmd，保留旧工具名；后续显式配置 PowerShell。进程取消须验证进程树清理，Job Object 与路径身份检查的实际限制仍需实现时验证。本轮只记录设计，不带来数据迁移或生产兼容性变化。

## 下一阶段前置条件与入口

**等待用户明确开始 S8.1，本轮到此停止。** 下一轮先读取任务书、本文件、BASELINE、DESIGN、AGENT.md/CLAUDE.md，并重新检查分支与未提交文件。

S8.1 的基线用例和设计已经可用，无需重新克隆或联网获取源码。继续使用本地 `.venv` 和固定 Provider；优先修正 F03 的通用重试及取消语义，加入安全读取重试和取消清理回归。按 Windows/Python 3.13 主目标安排必要的支持声明、启动/进程适配，保留目前失败基线，逐项注明变化原因。正式扩展 Python 元数据需实际安装/测试验证，不能只放宽 requires-python。

S8.2/3 前还必须解决免模型 daemon 启动及 Windows 生命周期问题，才能开展真实恢复 E2E/TUI 验收。WSL2、真实模型 API、远端仓库连接和发布均不是 S8.1 前置条件。不要在 S8.1 中提前宣称或一并实现整套跨重启恢复。
