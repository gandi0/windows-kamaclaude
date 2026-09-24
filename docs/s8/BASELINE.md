# S8.0 实测基线

日期：2026-09-23。仅建立基线、实验与设计，没有实现恢复功能。

> 本文及原 `evidence/upstream`、`evidence/final`、`reproduction*.json` 是 S8.0
> 的历史记录。S8.1 后源码已变化，F03 和 Windows 启动脚本已改为正向回归；
> 当前运行结果及环境声明以 [STATUS.md](STATUS.md) 和 `evidence/s81/` 为准。

## 1. 现场、源码与约束

- 原工作区 `F:\wend\ChatGPT\kamaclaude优化` 是无提交的 Git 仓库，无远端，只有未跟踪的 `S8_IMPLEMENTATION_TASK.md`。用户最初给出的 `S8\_IMPLEMENTATION\_TASK.md` 不存在，实际读取根目录任务书，原文件未修改。
- 独立检出：`F:\wend\ChatGPT\kamaclaude优化\KamaClaude`，本地分支 `codex/s8-reliability`。
- 基线：`a7adac2c3ecb5f66460d006840aea1e5743db45f`，提交时间 `2026-09-08 16:42:02 +0800`。首次克隆时 `origin/main` 与它相同，没有混入更新。远端配置保留克隆默认值；用户要求本地开发后未再访问 GitHub。没有提交、推送、PR 或发布。
- 已读 `AGENT.md`、`CLAUDE.md`、`RUNBOOK.md`、`README.md`、`pyproject.toml`、`Makefile`；检出内未找到额外 `AGENTS.md`。TUI 为主要产品入口；新函数遵循中文注释约定。
- 用户后续明确 **Windows 原生优先、使用现有 Python、本地开发测试**，覆盖任务书中 Linux/WSL2 优先的旧设定。WSL2 不再是继续 S8 的前置条件。
- 默认固定假 Provider，实验只操作新建临时目录。当前生产源码、原有测试、配置及协议文件均与基线相同。

## 2. 实际环境与安装方式

| 项目 | 实际值 |
| --- | --- |
| OS | Windows 11，10.0.26100，AMD64 |
| Python | 已有 `G:\toolsr\python3.13-exe\python.exe`，3.13.0 |
| 隔离解释器 | `.venv\Scripts\python.exe`，由上述解释器 `python -m venv .venv` 创建 |
| Git / uv | 2.54.0.windows.1 / 0.12.13 |
| 模型 SDK / UI | anthropic 1.8.0 / textual 8.2.8 |
| 数据与 HTTP | pydantic 2.13.5 / python-dotenv 1.2.3 / httpx 0.28.1 |
| 检查工具 | pytest 9.1.1 / pytest-asyncio 1.4.0 / Ruff 0.16.8 / mypy 2.3.1 |
| 完整依赖 | [environment.json](evidence/upstream/environment.json)、[requirements-observed.txt](evidence/upstream/requirements-observed.txt) |

上游 `requires-python = ">=3.12,<3.13"`、`.python-version = 3.12`。本轮依用户指示使用 3.13，**没有改写版本声明，也没有声称标准项目安装已支持 3.13**。通过 `PYTHONPATH=src` 直接测试源码；没有安装项目本身。`uv sync` / `make verify-s0` 未完成执行：正常同步会选择 3.12，且仓库没有跟踪 `uv.lock`。本轮没有下载或切换 Python。

最初读取 uv 默认缓存遇到拒绝访问；包含下载 Python 3.12 的提权请求被用户拒绝，未执行。用户随后要求使用已有 Python，因此仅用现有解释器创建虚拟环境，依赖安装到该环境，uv 缓存位于工作区 `..\.s8-env\uv-cache`。没有修改全局 Python 包。

实际依赖安装命令（项目目录内 PowerShell）：

```powershell
python -m venv .venv
uv --cache-dir '..\.s8-env\uv-cache' pip install --python .venv/Scripts/python.exe 'pydantic>=2.0' 'python-dotenv>=1.0' 'anthropic>=0.25' 'textual>=0.75' 'httpx[socks]>=0.28.1' 'ruff>=0.4' 'mypy>=1.10' 'pytest>=8.0' 'pytest-asyncio>=0.23'
```

安装日志：[dependency-install.txt](evidence/dependency-install.txt)。上游只规定下限，未固定依赖，因此此次结果是“指定源码 + 上表依赖 + Windows/Python 3.13”的基线，不能反推上游原开发环境也有相同失败。重建相同包版本可在已有 Python 创建的新虚拟环境内执行：

```powershell
.venv/Scripts/python.exe -m pip install -r docs/s8/evidence/upstream/requirements-observed.txt
```

WSL 初检：沙箱内 `wsl --status` / `wsl --list --verbose` 返回 `E_ACCESSDENIED`；沙箱外读到 Ubuntu-22.04、Stopped、VERSION 2；启动 `wsl -d Ubuntu-22.04 -- bash -lc ...` 返回 `Wsl/Service/CreateInstance/CreateVm/HCS/HCS_E_SERVICE_NOT_AVAILABLE`。没有 Linux 验证结果，没有修改虚拟化或系统服务设置。

## 3. 原有检查结果

在项目根目录执行，脚本为每项检查创建独立临时 Windows 用户目录、剔除继承的模型密钥和 KAMA 配置、显式启用 pytest-asyncio。源码通过子进程的绝对 `PYTHONPATH` 导入。此次检出无 `.env`；真实 API 测试文件在收集前被排除。未更改全局用户目录或原有测试。

```powershell
.venv/Scripts/python.exe scripts/s8/check_baseline.py --output docs/s8/evidence/upstream
```

| 子命令（前缀均为 `.venv/Scripts/python.exe`） | 实际结果 | 证据 |
| --- | --- | --- |
| `-m ruff check src tests scripts` | 退出 1，48 项既有问题 | [ruff.txt](evidence/upstream/ruff.txt) |
| `-m mypy src` | 退出 0，85 个源码文件通过 | [mypy.txt](evidence/upstream/mypy.txt) |
| `-m pytest tests/unit -v --tb=short` | 退出 1，255 通过、7 失败 | [unit.txt](evidence/upstream/unit.txt) |
| `-m pytest tests/integration -v --tb=short --ignore=tests/integration/test_run_e2e.py -m "not integration"` | 退出 1，3 通过、7 个 fixture 错误 | [integration.txt](evidence/upstream/integration.txt) |
| `scripts/gen_protocol_doc.py --check` | 退出 0，协议文档一致 | [protocol.txt](evidence/upstream/protocol.txt) |

完整命令、退出码、用时见 [checks.json](evidence/upstream/checks.json)。计时只是本次检查耗时，不是恢复性能指标。未执行付费 `test_run_e2e.py`，没有用全局 skip、放宽规则或改断言隐藏失败。

交付前用最终脚本复跑全部上述检查，输出改为 `docs/s8/evidence/final`，各项结论及失败数不变，见 [最终 checks.json](evidence/final/checks.json)。最终隔离环境还将 `ANTHROPIC_API_KEY` 固定为空，阻止 dotenv 默认加载行为补入用户真实 Key。

失败分类：

1. `test_bash_timeout` 使用 `sleep 5`；本机原生 cmd 路径中该命令未按 POSIX sleep 执行，返回 `runtime_error` 而非 `timeout`。这是当前测试命令的平台依赖，不能据此判断所有 Windows 超时机制已坏。
2. `test_compactor.py` 六项同步测试使用 `asyncio.get_event_loop().run_until_complete(...)`。整组运行到这里时主线程没有当前 loop；只运行该文件则 **6 通过**。证据说明存在运行顺序/事件循环生命周期依赖，未进一步区分 Python 与 pytest-asyncio 版本的影响，未调整依赖来掩盖它。
3. 7 项集成错误均在 daemon 启动 fixture 中触发：`CoreApp.run()` 无条件构造压缩用 `AnthropicProvider`，无 Key 就退出。连 ping/session.create 这类不发模型请求的测试也无法启动。这是基线启动耦合问题。
4. Ruff 的 48 项均在原有文件中，包含行长、导入排序、未使用导入等。新增 `scripts/s8` 单独 lint 通过。

压缩测试隔离复查使用相同环境隔离器，实际命令如下；日志见 [compactor-isolated.txt](evidence/compactor-isolated/compactor-isolated.txt)：

```powershell
.venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'scripts/s8'); import check_baseline; check_baseline.CHECKS={'compactor-isolated':['-m','pytest','tests/unit/test_compactor.py','-v','--tb=short']}; sys.argv=['check_baseline.py','--output','docs/s8/evidence/compactor-isolated']; raise SystemExit(check_baseline.main())"
```

Windows 启动障碍另作确定性验证：

```powershell
.venv/Scripts/python.exe scripts/s8/probe_windows.py --output docs/s8/evidence/windows-startup.json
```

该入口在测试子进程里仅替换配置和 Provider 构造器，让真实 `CoreApp.run()` 监听随机本地端口；执行到 `loop.add_signal_handler(signal.SIGINT, ...)` 时实际抛出 `NotImplementedError`。没有替换信号接口，也没有调用模型。子进程退出后端口释放；不是成功启动的 daemon 演示。[windows-startup.json](evidence/windows-startup.json) 保留堆栈。

## 4. F01–F05 可执行复现

项目根目录 PowerShell，一条脚本运行所有实验；每个实验重新创建临时目录，完成后清理。没有复用手工 session、API Key 或原有用户文件。默认最大单项 60 秒，当前没有发生超时。

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
.venv/Scripts/python.exe scripts/s8/reproduce.py --output docs/s8/evidence/reproduction.json
```

仅运行一个问题：`.venv/Scripts/python.exe scripts/s8/reproduce.py --case F03`（同样先设置上述 PYTHONPATH）。`F01` 至 `F05` 均可作为 `--case`。需要保留已有证据时将 `--output` 指向新的文件。

这些是**基线行为断言**，退出 0 表示问题被复现，不表示恢复能力通过验收。修复相应问题后应改为正常的正向回归测试；本轮没有使用 xfail，也没有生产故障开关。完整实际值见 [reproduction.json](evidence/reproduction.json)。

审查后加强 F02 对原始 JSONL 中 tool_use/tool_result 配对及 session 状态的断言，再以相同命令（输出为 `reproduction-repeat.json`）运行全部五项，退出 0，结果一致，见 [第二次复现](evidence/reproduction-repeat.json)。每次均从新临时目录开始。F02 写入发生在 Runner 子进程内的真实 WriteFileTool；未将其描述为 Shell 后代进程崩溃实验。

| 编号 | 应满足的行为 / 实验构造 | 实际结果 | 实测边界 |
| --- | --- | --- | --- |
| F01 | 磁盘上已有会话应可被新管理器访问；先完成固定回复，再在同一存储上构造新 SessionManager | 磁盘 2 条消息及 waiting_for_input 元数据仍在；get_history 和 send_message 都报 `-32010` | 对象重建复现索引缺失，未声称 daemon 重启 E2E |
| F02 | 副作用与执行状态之间需有恢复依据；真实写文件成功、tool_result 已进入内存，在第二次模型入口 `os._exit(86)` | marker=`effect`；thread 仅 1 条用户消息；无 tool_result、无 run.finished；meta 仍 active。正常对照 thread 为 4 条，结果及结束事件存在 | 真实 Runner 子进程硬退出，故障注入窗口明确；不模拟掉电、磁盘故障或真实 daemon 重启 |
| F03 | 非幂等命令失败不能通用重跑；Python 子程序先计数 +1 再 exit 7 | 直接 BashTool 调用计数 1；一次 invoke_tool 调用计数 **3**，attempt 为 1/2/3 | Windows 上用真实 cmd.exe，Python 可执行文件及参数按 Windows 引号规则传入；只把退避等待设为 0，不改重试次数 |
| F04 | 压缩后摘要和新增历史应可在下一轮重建；预填 9 条，开启阈值 0.8，压缩后再执行工具并结束 | 工作上下文缩为 5 条；旧切片从 9 开始，未新增任何历史；摘要文件存在，两次写文件均成功；下一轮无摘要/最终回复。禁用压缩对照保存 14 条并包含最终回复 | 用真实 Loop/Compactor/Runner + 固定摘要，默认阈值实际为 **0.0（关闭）**；不能称为默认配置必现，更未评测摘要质量 |
| F05 | 同一会话下一轮应能查询已派生子任务；真实后台 spawn 完成，以完成事件同步 | 原 Runner 查询得到 `child done`；第二轮新 Runner 的 agent_result 返回 `Unknown run_id` | 实测同进程跨 turn；未验证重启后的子任务中断展示、运行中任务树恢复 |

关键代码定位：`session/manager.py::_get_session/send_message`；`runner.py::run_and_capture` 的 `prefill_len` 与末尾 append；`tools/invocation.py::invoke_tool`、`tools/builtin/bash.py::invoke`；`compact/compactor.py::compact`；`runner.py::__init__` 与 `subagent/tool.py::AgentResultTool`。

F02 同时观察到 `events.jsonl` 有 `tool.call_finished`，但 thread 缺结果。事件不是统一检查点。另经源码核对，`EventWriter.handle()` 对写入异常只记日志；**本轮未注入事件写盘失败**，不能据此声称已实测日志丢失。

## 5. 未验证与下一步

- 尚无跨重启恢复实现、SQLite 事务、幂等导入、权限重审、并发恢复、工作区冲突检查或压缩检查点。
- 原生 Windows daemon 已确认存在启动阻塞；TUI 现有单测通过，但未进行真实 TUI/daemon 恢复演示。
- Linux、WSL2、Python 3.12、Windows 其他版本/文件系统、进程树取消、MCP 外部副作用均未验证。
- 未测真实模型、摘要质量、恢复耗时、额外开销；没有性能或成功率提升数字。
- 下一阶段从这些实际复现进入 S8.1：先明确 Windows/Python 3.13 支持范围，再处理重试、取消与进程清理。详见 [DESIGN.md](DESIGN.md) 和 [STATUS.md](STATUS.md)。
