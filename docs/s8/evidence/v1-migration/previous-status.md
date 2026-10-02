# S8 状态交接

更新：2026-09-23。**S8.0、S8.1 已完成；本轮停在 S8.1，未启动 S8.2。**

## 当前目标与现场

本轮修正进程内工具重试、中断和清理语义，以 Windows 原生和现有 Python 为主目标。
原任务书是父目录 `S8_IMPLEMENTATION_TASK.md`；最初给出的嵌套路径不存在。

- 工作目录：`F:\wend\ChatGPT\kamaclaude优化\KamaClaude`。
- 基线 / 分支：`a7adac2c3ecb5f66460d006840aea1e5743db45f` / `codex/s8-reliability`。
- 环境：Windows 11 10.0.26100、已有 Python 3.13.0、本地 `.venv`；具体依赖见 [环境快照](evidence/s81/final/environment.json)。
- 本轮未访问 GitHub、未调用付费 API、未下载或更换 Python、未升级依赖；无新提交、暂存、push、PR 或发布。
- 保留 S8.0 文档和历史证据；原 STATUS 另存 [S8.0 交接快照](evidence/s81/s80-status.md)。所有修改仍为本地未提交变更。

## 实际文件与行为变化

| 文件 | 本轮改动及原因 |
| --- | --- |
| `core/tools/base.py`、`errors.py`、`invocation.py` | 默认未知副作用、默认不重试；仅显式安全且暂时的已知失败允许最多两次重试；结构化 outcome、call/attempt UUID、清理结论；前置校验/审批、超时与取消分开处理 |
| `core/tools/builtin/*.py` | 读取工具声明安全；写工具声明 write；任务创建/查询/更新补参数模型；Shell 保留工具名 bash，在 Windows 显式使用 cmd |
| 新增 `core/tools/process.py`、`windows_job.py`、`_shell_worker.py` | Job 接管并握手后才发送命令；取消/超时及正常退出清理所属进程树；重复取消等待清理；未确认清理返回 unknown |
| `core/permissions/manager.py`、`core/loop.py` | 取消移除审批 pending；停止同批后续工具及后续模型请求；未知结果进入 needs_review |
| `core/mcp/client.py`、`tool.py` | 正确识别 MCP `isError=true`；调用失败/失联保守返回 unknown，默认不重试 |
| `core/bus/events.py`、`WIRE_PROTOCOL.md` | 向诊断事件增加调用/尝试标识和结果/清理信息，字段有默认值以兼容旧事件；同步生成协议文档 |
| `tui/app.py` | needs_review 显示为需要核查，避免呈现成已完成或已知失败 |
| 新增 `core/llm/lazy.py`；修改 `core/app.py`、`runner.py` | 惰性创建模型客户端；无 Key 可启动 daemon，真正请求模型时明确失败；Windows 主线程信号桥接 asyncio |
| `.python-version`、`pyproject.toml`、`RUNBOOK.md` | 开发版本改为现有 3.13；支持声明扩为 `>=3.12,<3.14`，本地 editable 安装并检查依赖；补 Windows 启动和测试命令 |
| 新增 `tests/unit/test_s81_execution.py`、`test_s81_process.py` | F03、副作用分类、安全读取重试、ID、参数/权限、取消、MCP、Windows 真实进程树与清理失败回归 |
| `test_tool_retry.py`、`test_loop.py`、`test_builtin_tools.py`、`test_compactor.py`、`test_tui_app.py` | 旧 stub 显式声明重试契约，保留次数断言；用当前 Python 替代 POSIX sleep；六个 compactor 用例自建 event loop；增加 TUI 状态断言 |
| `scripts/s8/*.py`、`docs/s8/*.md`、`evidence/s81/` | F03 和 Windows 启动改为正向回归；保留其他问题实验；补专项入口、设计和实际验证记录 |

上表源码路径相对于 `src/kama_claude/`，测试文件相对于 `tests/unit/`。
调用 ID 当前只用于进程内关联和诊断；**未实现 SQLite、可靠意图提交、检查点、持久去重或跨重启恢复**。
传入同一 call_id 也不会复用持久结果；两次合法相同命令仍分别执行。

## 验证命令与结果

以下在项目根目录 PowerShell 执行。检查脚本隔离用户配置并清空 API Key；真实模型 E2E 显式排除。

```powershell
.venv/Scripts/python.exe scripts/s8/check_baseline.py --check s81 --output docs/s8/evidence/s81/targeted-complete
.venv/Scripts/python.exe scripts/s8/check_baseline.py --output docs/s8/evidence/s81/final
.venv/Scripts/python.exe scripts/s8/reproduce.py --output docs/s8/evidence/s81/reproduction.json
.venv/Scripts/python.exe scripts/s8/probe_windows.py --output docs/s8/evidence/s81/windows-startup.json
.venv/Scripts/python.exe scripts/gen_protocol_doc.py --check
.venv/Scripts/python.exe -m pip check
git diff --check
```

本轮已执行本地安装：`uv --cache-dir '..\.s8-env\uv-cache' pip install --python .venv/Scripts/python.exe --no-deps -e .`。
没有执行 `uv sync`，避免重新解析和变更已有依赖。

| 检查 | 实测结果 / 证据 |
| --- | --- |
| S8.1 专项及受影响测试 | **88 passed**；[专项日志](evidence/s81/targeted-complete/s81.txt) |
| 全量 unit | **289 passed**，S8.0 为 255 passed / 7 failed；[单元日志](evidence/s81/final/unit.txt) |
| 免 API integration | **10 passed**，S8.0 为 3 passed / 7 errors；[集成日志](evidence/s81/final/integration.txt) |
| mypy | **通过**，89 个源码文件；[类型日志](evidence/s81/final/mypy.txt) |
| 协议同源 | **通过**；[协议日志](evidence/s81/final/protocol.txt) |
| 全仓 Ruff | **42 项已有诊断**，S8.0 为 48；检查脚本因此退出 1，不能称全仓检查全绿；[日志](evidence/s81/final/ruff.txt) |
| 新增执行代码及脚本 Ruff | **通过**；范围为 tools 目录、lazy.py、loop.py、events.py、mcp/tool.py、scripts/s8 和两份新测试 |
| Ruff 基线对照 | 将当前每个诊断文件与 `git show HEAD:<path>` 比较 code/message 数量，**新增诊断 0**；[对照记录](evidence/s81/lint-comparison.json) |
| 安装与依赖、diff 空白检查 | editable 安装成功；临时 cwd 且无 PYTHONPATH 时导入/CLI 版本查询成功（[证据](evidence/s81/installed-smoke.json)）；pip check 无依赖冲突；git diff --check 通过 |
| Windows 启动/关闭 | 独立测试进程内真实 CoreApp 无 Key 启动、TCP ping 成功、内部 raise_signal(SIGINT) 后干净退出；[结果](evidence/s81/windows-startup.json) |
| F01–F05 | F03 已转正向回归，其余四项仍复现；[结果](evidence/s81/reproduction.json) |

复现对照：F03 修复前一次 invoke 使计数器增加 **3** 次（[修复前](evidence/s81-before-f03.json)），
修复后增加 **1** 次，attempt 为 `[1]`。专项还验证相同参数的第二次合法调用使计数器到 2。
真实 read_file 两次 EAGAIN 后第三次成功，每次 attempt ID 不同；旧测试继续验证重试耗尽上限。
非法参数/拒绝/审批超时均不调用本体；取消期间不开始下一工具或模型请求。
Windows 子/孙进程用 TCP 就绪屏障和内核进程句柄验证退出，覆盖取消及外层超时；
另覆盖重复取消、清理失败和 Job 创建/加入失败不产生 marker。

## 设计取舍、兼容性与未验证部分

- Shell 非零退出是已知失败，但不授权自动重试；取消/超时可能留下已完成的部分副作用。清理确认只证明受控本地进程退出，不是副作用回滚。
- Windows Shell 为系统 `cmd.exe /d /s /c`，不是 PowerShell。命令结束也会清理后台后代，不支持以 bash 工具留下长期后台任务。含空格和中文的脚本路径已实测。
- Job 的 KILL_ON_JOB_CLOSE 提供父进程丢失句柄时的内核回收机制；本轮未做父进程硬杀验证，也未覆盖任意并发派生、特殊 Job 嵌套或恶意逃逸。不能把普通取消测试外推为硬崩溃可靠性。
- Shell 经服务管理器、任务计划程序等外部机制启动的工作可能不属于该 Job；本地进程树清理结论不覆盖这些外部工作。
- needs_review 仅停止当前 run 并提供 TUI 诊断，没有持久核查/恢复入口；下一次用户消息仍新建 run，未对整个 session 加持久执行锁。
- MCP 只用固定客户端响应/异常测试。其远端 JSON Schema 尚无本地验证器；本阶段“参数错误不进入本体”的保证覆盖有参数模型的内置工具。MCP 参数拒绝也按未知处理；远端取消和 stdio 进程树接管未验证/未实现。
- F01 会话内存索引、F02 工具结果收尾才保存、F04 显式启用自动压缩后漏存、F05 跨 turn 子代理查询仍待后续阶段。自动压缩默认仍关闭；本轮未改压缩生产代码。
- 未运行真实付费模型、真实摘要质量、Linux/WSL2、Python 3.12、PowerShell、控制台窗口关闭、SQLite/断电/并发恢复/工作区冲突和性能测试；未演示 TUI 恢复流程。当前只验证 Windows/Python 3.13 和已记录依赖组合。
- S8.1 必需的进程内执行验收已通过；全仓 Ruff 历史问题仍保留，未放宽 lint 规则、删除断言或用大范围 skip 掩盖失败。

## 下一阶段前置条件

**等待用户明确启动 S8.2；本轮到此结束。** 已有 S8.1 语义和本地 Windows 免 Key daemon 可供下一阶段使用。

下一轮先读取任务书、本文件、BASELINE、DESIGN、AGENT.md/CLAUDE.md，并检查分支和未提交 diff；
不重新克隆、不覆盖现有文件。S8.2 应把 call/attempt 接到可靠存储：SQLite 作为唯一执行权威，
执行前提交意图；执行后原子提交结果、原始消息与 checkpoint；存储失败停止推进。
同时实现稳定消息序号、工作区记录及旧 JSONL 无损幂等导入，并验证执行前失败没有 marker、
事务故障后只读取一致的已提交状态。S8.3 再验收真实 daemon 中断—重启—恢复和 TUI 核查。

本地开发、现有 Python、固定 Provider 继续适用；联网 GitHub、真实模型、WSL2 和发布均不是前置条件。
