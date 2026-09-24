# S8 当前工作区与阶段交接

更新：2026-09-24。**S8.5 实施与验收已完成，主工作区为 KamaClaude v1、分支 `s8`；等待用户决定是否提交或发布。** S8.1-S8.5 仍是未提交工作区改动，不自动开始其他阶段。

## 工作区与分支

- 主目录：`F:\exercise\2026\kamaclaude - v1\KamaClaude`。
- 分支起点：本地 `stage/s7`，提交 `cc352fe653c26ab763838899785b6bdd6575f0c0`。
- 新分支：`s8`，从上述本地 S7 创建，包含既有 Windows daemon 修复。S7 分支指针不移动。
- 沿用此目录已有 Python **3.12.14**、`.venv`、`.python-version=3.12`；依赖未升级，未下载或切换 Python。
- 旧检出 `F:\wend\ChatGPT\kamaclaude优化\KamaClaude` 保留为历史，原 S8.1 基线为 `a7adac2` / `codex/s8-reliability`；原交接见 [迁移前 STATUS](evidence/v1-migration/previous-status.md)。
- 本轮没有联网 GitHub、模型 API、提交、推送或发布。S8 源码变更仍未提交；分支名本身不保存工作区未提交的修改。
- 目标原 `.env`、`.env.swp`、`test`、`.kama/context.md`、`uv.lock`、虚拟环境及 IDE 配置均保留；迁移脚本仅写入明确的 S8 文件清单并校验受保护文件摘要。

原任务书仍为 `F:\wend\ChatGPT\kamaclaude优化\S8_IMPLEMENTATION_TASK.md`。
今后以本文件和此 v1 仓库为入口，不再继续修改旧检出。

## S8.1 迁移记录（历史）

S8.1 已实现：副作用类别及保守重试、call/attempt UUID、参数与审批预检、取消传播、
Windows Job 进程树清理、MCP 错误转 unknown、未知结果停止当前 run 和 TUI 核查提示、无 Key 启动。
核心文件为 `src/kama_claude/core/tools/`、`core/loop.py`、`core/permissions/manager.py`、
`core/mcp/`、`core/llm/lazy.py`、`core/app.py`、`core/runner.py`、`core/bus/events.py` 和 `tui/app.py`。
对应新增 `test_s81_execution.py`、`test_s81_process.py`，调整原重试、Shell、compactor、TUI 测试，
并更新 `WIRE_PROTOCOL.md`、`pyproject.toml`、`RUNBOOK.md`、S8 文档和验证脚本。

迁移时没有覆盖整个 S7 仓库：保留 S7 的 README、既有 Ctrl+C 同步入口处理和清理逻辑；
`test_app.py` 保留清理断言，补 Windows 桥接与 Unix 回调两条路径。
目标 Python 3.12 暴露了超时被当成取消的问题，现以明确的 `asyncio.timeout` 截止状态区分，
同时保留清理失败结论；额外用现有 3.13 运行同一专项集验证。
测试脚本的集成子进程屏蔽项目 `.env` 与项目 MCP 配置，以免读取真实模型或服务设置。
每次 pytest 使用该次验证专属的 `--basetemp`，避免不同 Windows 账户复用旧临时目录时的 ACL 冲突。

## S8.1 验证命令与证据（历史）

在主项目根目录 PowerShell 执行，不需要 API Key：

```powershell
.\.venv\Scripts\python.exe scripts/s8/check_baseline.py --check s81 --output docs/s8/evidence/s8-local
.\.venv\Scripts\python.exe scripts/s8/check_baseline.py --output docs/s8/evidence/s8-full-local
.\.venv\Scripts\python.exe scripts/s8/reproduce.py --output docs/s8/evidence/s8-reproduction-local.json
.\.venv\Scripts\python.exe scripts/s8/probe_windows.py --output docs/s8/evidence/s8-windows-local.json
git diff --check
```

| 检查 | 迁移前本地 S7 | S8.1 迁移稿实测 |
| --- | --- | --- |
| Windows / 目标 Python 3.12 unit | 261 passed，7 failed | **300 passed** |
| 免 API integration | 3 passed，7 errors | **10 passed** |
| mypy | 通过 | **通过，89 个源码文件** |
| 协议同源 | 通过 | **通过** |
| 全仓 Ruff | 48 项已有诊断 | **42 项已有诊断**，总检查因此退出 1 |
| 现有 Python 3.13 专项 | 原检出已有验证 | **99 passed**，包括目标 S7 启动回归与新增兼容用例 |

准备过程先在工作区内的独立本地克隆完成，不向 GitHub取源码。
[S7 基线](evidence/v1-migration/s7-before/checks.json)、
[迁移稿最终检查](evidence/v1-migration/prepared-final/checks.json)、
[3.13 专项](evidence/v1-migration/targeted-py313/s81.txt) 已保留。
首次迁移试跑确实出现 1 项 Python 3.12 超时分类失败，原日志保留在 `s8-prepared/unit.txt`，
修复后才生成最终通过记录，没有删除断言或跳过该测试。
写入主仓库后同样运行全量检查、复现实验和 Windows 启动探测，结果保存在
`docs/s8/evidence/v1-migration/installed-final/`；`migration.json` 记录分支及用户文件保留校验。
目标目录第一次复验的临时目录权限错误保留在 `installed-first-attempt/`；
它们是测试 fixture 创建失败，修正独立临时目录后重新运行，未清理其他账户的临时文件。

S8.1 当时 F03 一次调用仅增加计数器 1 次；当时 F01/F02/F04/F05 仍能复现。
S8.2 的最新回归结果见下文。
Windows 无 Key daemon 的 ping 与 SIGINT 干净退出已验证。副作用、重试和清理边界见 [DESIGN.md](DESIGN.md)。

## S8.2 完成情况

本轮仅实施“持久化执行记录与检查点”。实际源码来自 v1 工作区的未提交 S8.1，
在 `.s82-work/main` 隔离副本实施，并由主线程集成两个独立 worktree 的存储和测试产物。
解释器始终使用 v1 原有 `.venv/Scripts/python.exe`（3.12.14），没有重建环境、升级依赖或调用模型 API。
写回前按逐文件摘要检查原始工作区，写回仅覆盖明确变更清单，用户受保护文件逐字节核对。

已完成：

- schema v1 的 SQLite session/run、不可变原始消息 UUID/seq、工具调用和每次 attempt、checkpoint。
- 接受消息、模型响应意图、每次派发、结果/消息/checkpoint、最终回复的短事务边界。
- 执行前提交失败不启动工具；执行后提交失败立即停止，不依赖补写失败标志；未知状态保留可查事实。
- 查询持久 call_id；校验实际调用与意图一致；已开始的 ID 不允许再次调用；相同参数的不同 ID 不合并。
- 原始记录脱离可压缩工作列表；模型投影不再裁掉未完成批次，手动压缩不覆盖旧 JSONL。
- 旧会话显式、无损、幂等导入：保留源快照与逐行 bytes/映射，追加验证前缀，重写隔离 conflict，不伪造旧 attempts。
- 会话绑定 workspace，主 run 文件工具和 Shell 显式传递目录；数据库关闭接入 daemon 现有资源清理。

实际源码清单：

| 文件 | 变化 |
| --- | --- |
| `core/session/execution.py`（新增） | SQLite schema、原子事务、状态及关联校验、稳定序号、结果查询 |
| `core/session/legacy.py`（新增） | 原始 BLOB/逐行映射、幂等与前缀追加、重写隔离 |
| `core/session/store.py`、`manager.py` | SQLite 唯一权威、接受请求事务、原始历史与工作投影、提交后发布状态事件 |
| `core/loop.py`、`core/runner.py` | 即时提交原文与意图，移除 prefill_len 收尾切片，提交故障停止推进 |
| `core/tools/invocation.py` | 持久意图核对、参数固定、每次派发前提交、结果先提交后重试/继续 |
| `core/tools/base.py`、`builtin/{bash,read_file,write_file,list_dir}.py`、`core/tools/process.py` | 固定 workspace/cwd |
| `core/app.py` | 关闭数据库连接 |
| `tests/unit/test_s82_{store,legacy,execution,session}.py`（新增） | DB 重开、marker、副作用与取消、导入、workspace/投影验收 |
| `tests/unit/test_session_store.py`、`test_session_manager.py` | 旧裁剪契约改为保留并拒绝，假 Runner 使用新收尾契约 |
| `scripts/s8/{check_baseline,import_legacy,probe_s82,reproduce}.py` | S8.2 检查入口、导入 CLI、硬退出实验、F02/F04 原始记录回归 |
| `RUNBOOK.md`、`docs/s8/{DESIGN,STATUS}.md`、`docs/s8/evidence/s82/` | 操作、边界、证据与交接 |

表中源码路径相对于 `src/kama_claude/`；完整 S8.2 patch/写回清单另保存在准备目录 `.s82-work/review/`。
Git 相对 HEAD 的总 diff 仍包含此前全部未提交 S8.1 和用户改动，不应误算为仅本轮变更。

## S8.2 验证命令与证据

在主项目根目录 PowerShell 执行：

```powershell
.venv/Scripts/python.exe scripts/s8/check_baseline.py --check s82 --output docs/s8/evidence/s82-local
.venv/Scripts/python.exe scripts/s8/check_baseline.py --check s82-crash --output docs/s8/evidence/s82-crash-local
.venv/Scripts/python.exe scripts/s8/check_baseline.py --check s82-reproduction --output docs/s8/evidence/s82-reproduction-local
.venv/Scripts/python.exe scripts/s8/check_baseline.py --output docs/s8/evidence/s82-full-local
git diff --check
```

| 实测项目 | 结果与证据 |
| --- | --- |
| 前置 S8.1 专项 | **99 passed**，`evidence/s82/preflight/s81.txt` |
| 最终全量 unit | **330 passed**，`evidence/s82/prepared-final/unit.txt` |
| 最终免 API integration | **10 passed**，`evidence/s82/prepared-final/integration.txt` |
| mypy | **91 个源码文件通过**，`evidence/s82/prepared-final/mypy.txt` |
| 协议同源 | **通过**，`evidence/s82/prepared-final/protocol.txt` |
| 全仓 Ruff | **42 项已有诊断**，`evidence/s82/prepared-final/ruff.txt`；总脚本因此返回 1 |
| 五个硬退出故障点 | **5/5 通过**，`evidence/s82/crash-final/s82-crash.txt` |
| F01–F05 固定模型复现实验 | `evidence/s82/reproduction-final/s82-reproduction.txt`，范围见下文 |

五个真实进程实验分别在意图、dispatch、result、final 事务提交前硬退出，以及工具结果提交后下一次 chat 硬退出。
前两处无 marker；结果提交前已有 marker 但库中仍为 dispatching、无结果消息；结果提交后可查询完整结果、消息与一致 checkpoint；
最终回复提交前退出不会保存虚假 succeeded。它们不是 daemon 重启恢复 E2E，也不证明断电可靠性。

F02 改为原始结果持久化回归：硬退出时有 3 条已提交消息和 checkpoint=3，未伪造 run 完成。
F03 仍只产生 1 次副作用。F04 原始记录漏存已消除：压缩开启/关闭均保存 14 条原始消息，下一轮均有最终回复；
下一轮尚不使用持久摘要，因此 F04 的完整摘要恢复验收仍属于 S8.4。
F01 新 Manager 无法索引旧会话、F05 跨 Runner 子任务查询仍能复现，保留为 S8.3 入口。

初次集成的 import 循环、取消测试宿主 Task 的 fixture 问题、取消状态分类以及接口类型错误已修复；
首次日志保留在 `first-unit/first-targeted/first-mypy/targeted/`，最终全量 unit 包含这些用例并全部通过。
未删除失败断言、放宽全局规则或跳过本阶段验收。

## S8.2 当时的限制与 S8.3 前置条件（历史）

- 本阶段存储已生效，但没有启动扫描、重建会话索引、恢复执行、业务请求去重或 TUI 恢复入口。
- storage_error 诊断不保证数据库已写入 needs_review；数据库可能保留 running/dispatching。不能自动重发其工具；
  在 S8.3 提供核查操作之前，当前会话会阻止继续输入，可创建新会话，但不会自动处置旧副作用。
- 原始消息完整保留；摘要仅有当前进程工作投影与诊断文件，checkpoint 摘要引用字段预留且未激活。
- 父 spawn 调用有记录，子 agent 内部调用及后台任务树仍沿用旧路径；持久父子关系/中断展示与 registry 生命周期留给 S8.3。
- 单 session 未完成 run 的检查仅是本阶段安全护栏，不等于多 daemon 锁、恢复所有权或客户端业务请求幂等。
- 真实模型、Linux/WSL2、Python 3.13 的本轮 S8.2、PowerShell、任意远程 MCP、断电/磁盘损坏、性能、
  工作区身份/junction 冲突检查、daemon 强杀后的完整恢复与实际 TUI 恢复均未验证。
- 下一阶段开始前核对本地 `s8` 未提交源码、SQLite schema v1 和这些证据，再实施扫描、恢复控制、审批失效及 TUI。
  **以上为 S8.2 交接时的范围；S8.3 本轮结果见下文。**

## S8.3 实施内容

本轮从 v1 主工作区完整快照继续，保留全部未提交 S8.1/S8.2 和用户文件。
三个 `luna_worker` 使用互相独立的 worktree，主线程审查具体产物后在 `.s83-work/main` 集成。
仍使用 v1 原解释器 Python 3.12.14；模型为固定假实现，本地工具和 daemon 为真实实现。

- schema v1 无损迁移到 v2；启动索引与扫描区分 interrupted、已提交结果和 unknown/needs_review。
- 同一 session 的稳定 request_id 去重；OS 数据目录锁、持久 daemon epoch 和恢复 claim 防止重复执行。
- 只通过显式恢复补齐确定未派发的调用，复用已提交结果；未知副作用保留原始事实并暂停。
- 核对工作区身份、路径重新解析及已记录的相关文件状态；跨重启一次性授权失效。
- `session.list/status/resume/review` 协议和 TUI 会话选择、状态、继续、暂停、放弃操作。
- 保留 child run 关系、结果与中断状态；新 Runner 可查询持久结果，不自动恢复后台任务树。
- 新增独立 daemon 强杀重启和真实 SocketClient/Textual Pilot 操作验证入口。

主要增量涉及 `core/session/{execution,workspace,daemon_lock,manager,store,model}.py`、
`core/{app,runner,loop}.py`、`core/tools/invocation.py`、`core/permissions/manager.py`、
`core/subagent/{tool,registry}.py`、`core/bus/{commands,events}.py`、`tui/{app,recovery}.py`；
协议生成器、验证脚本、测试与操作文档同步更新。源码路径相对于 `src/kama_claude/`。

S8.4 的摘要持久化与恢复未开始。仍不保证任意 Shell/MCP 的外部副作用 exactly-once；
未知结果必须人工核查。文件检查不能枚举任意 Shell/MCP 的全部读写集合，也不能锁住外部编辑器。
旧任务缺少历史工作区身份时保持保守阻塞；本轮不自动补造旧执行事实。

## S8.3 验收与证据

所有本轮执行均使用 Windows 原生、现有 Python 3.12.14。模型为固定假实现，真实 CoreApp
子进程、TCP JSON-RPC、SQLite、Shell/file 工具和 Textual/SocketClient 构成实际运行链路。
没有调用付费 API、提交或推送；S8.1/S8.2 与本轮源码仍为未提交状态。

| 检查 | 实测结果与证据（相对 `docs/s8/evidence/s83/`） |
| --- | --- |
| S8.2 前置复验 | **51 passed**，`preflight/s82.txt`；开工逐项核对 S8.2 原写回 79 文件摘要 |
| 最终全量 unit | **358 passed**，`prepared-final-unit-fixed/unit.txt` |
| S8.3 专项 | **28 passed**，`final-fencing/s83.txt`；含迁移、实际 Windows junction、审批取消和旧 epoch 拒写 |
| 免 API integration | **20 passed**，`prepared-final/integration.txt`；包括原有 10 项及本轮 10 项 |
| 独立真实 daemon/TUI 探针 | **10 passed**，`prepared-probe/probe.json`；8 个 daemon 场景、2 个真实 TUI 场景 |
| mypy | **94 个源码文件通过**，`final-static/mypy.txt` |
| 协议同源检查 | **通过**，`final-static/protocol.txt` |
| 全仓 Ruff | **28 项遗留诊断**，此前 42 项；`ruff-comparison.json` 按规则、相对路径与源行对照，**新增 0 项** |
| 写回与主目录复验 | `installed-final/installation.json` 保存逐文件摘要与保护校验；同目录 `checks.json` 和各项日志为主目录复验结果 |

真实恢复验收覆盖：

1. 工具结果提交后强杀，重启时两个 TCP 客户端同时恢复并重发原 request_id；仅一次认领，
   原 run/call 身份保持不变，实际 counter=1、attempt=1、tool_result=1。
2. 本地副作用产生后、提交结果前强杀；重启将 call 标为 unknown、run 标为 needs_review，
   attempt 仍为 dispatching，恢复不重放，counter 保持 1。
3. 一次性 allow 后、派发前强杀；重启重新请求审批，旧 approval_id 被拒绝，新 token 才放行。
4. workspace 不匹配、相关文件外部修改均阻止未派发写入，原文件内容和零 attempt 得到核对。
5. 子任务中断后可查父子关系且模型计数不增长；已完成子任务经 daemon 重启后，
   新用户请求启动新 Runner，通过真实 agent_result 调用取得持久结果。
6. 两个不同端口的 daemon 使用同一数据目录时，第二个进程被系统锁拒绝。
7. 真实 TUI 经 Ctrl+O、方向键/Enter 选择磁盘会话，再点击 Continue 完成恢复；
   未知结果场景经说明输入、Pause 按钮保持核查，说明写入 SQLite，counter 仍为 1。

TUI 操作由 Textual Pilot 驱动实际控件，经真实 SocketClient 与重启后的 daemon 通信；没有
伪造 RPC 响应或直接调用 App 私有恢复方法代替操作。`prepared-probe/probe.json` 汇集
32 份操作日志、19 份 daemon 日志及 6 张 SVG 截图，截图与两份 TUI 操作 JSONL 保存在
`prepared-probe/s83-artifacts-0e151pj6/`。另保留由真实 SVG 转换的 PNG，已检查长说明换行及按钮可见性。

- [成功恢复画面](evidence/s83/prepared-probe/s83-artifacts-0e151pj6/committed-after-resume.png)
- [未知副作用核查画面](evidence/s83/prepared-probe/s83-artifacts-0e151pj6/unknown-before-pause.png)

保留了准备过程的中间失败日志，未删除断言或跳过本阶段验收：修复了夹具工作区创建、
子任务查询审批、旧 metadata run_ids 保留及隔离环境中的 dotenv 专项夹具；真实探针发现的
已提交 Shell 结果误阻塞已修复。最终全量复验另暴露既有超时计时边界：工具吞掉取消时，
仅依赖计时差可能误判成功，已同时依据 timeout.expired() 判定，原 S8.1 用例保持并通过。
原失败保存在 `prepared-final-unit/unit.txt`，修复后的 358 项结果在 `prepared-final-unit-fixed/`。

写回仅使用本轮清单，安装前校验目标文件未变化，保留旧文件备份；保护 `.env`、`.env.swp`、
`test`、`.kama/context.md`、依赖锁、Python 版本、虚拟环境关键文件与 IDE 配置摘要，
HEAD 和 `stage/s7` 均保持 `cc352fe653c26ab763838899785b6bdd6575f0c0`，分支保持 `s8`。

未验证真实模型、Linux/WSL2、此轮 Python 3.13、任意远程 MCP、断电/磁盘损坏或性能。
本轮不提供整树恢复或未知副作用自动重放。此段为 S8.3 历史记录。

## S8.4 实施内容

SQLite schema 从 v2 无损迁移到 v3，新增不可变 `summaries` 表。每条摘要保存稳定 ID、
session 内版本、累计覆盖的稳定消息 `seq` 闭区间、来源主 run 与 checkpoint version、摘要文本、
生成配置版本 `s8.4-v1` 和配置 JSON。未知更高 schema 仍拒绝打开；原始消息没有更新或删除入口。

压缩先从当前主 run 捕获 checkpoint、有效摘要和原始消息前缀，再在事务外调用 provider。
提交事务重新校验当前 run、checkpoint version、旧摘要引用及覆盖前缀 digest，然后插入摘要并
CAS 更新同一 checkpoint 的有效引用和消息边界。摘要插入后、指针更新后或最终提交前发生异常
都会整体回滚。并发追加的 `seq > summary_to` 消息保留为未覆盖尾部，不会被旧候选摘要吞入。

模型上下文统一由“有效摘要 user/assistant 交接对 + `summary_to` 之后的完整原始消息”重建，
重建前仍对全量原始历史执行 tool_use/tool_result 配对校验。新主 run 在创建事务内继承上一主 run
的有效摘要；自动压缩提交成功后也从同一恢复视图替换内存上下文，不使用可变列表长度或
`prefill_len`。手动和自动入口共用 `Compactor.compact_persisted()`；默认自动阈值仍为 `0.0`。
诊断 `summary_<summary_id>.md` 只在数据库提交成功后写入，写文件失败不改变恢复权威。

`session.compact` 响应向后兼容增加 `summary_id`、`summary_version`、`summary_from`、
`summary_to`，生成的 `WIRE_PROTOCOL.md` 已同步。实现涉及：

- `src/kama_claude/core/session/{execution,store,manager}.py`
- `src/kama_claude/core/compact/compactor.py`、`core/runner.py`、`core/bus/commands.py`
- `tests/unit/test_s84_compaction.py`、`tests/integration/test_s84_compaction.py`
- `tests/helpers/s83_daemon.py`、`scripts/s8/check_baseline.py`、`WIRE_PROTOCOL.md`

## S8.4 验收与证据

全部检查使用 Windows 原生、现有 Python 3.12.14、固定假 provider 和专用临时 profile；
没有读取真实模型 Key、调用付费 API、提交、推送或发布。

| 检查 | 实测结果与证据（相对 `docs/s8/evidence/s84/`） |
| --- | --- |
| S8.4 专项 | **9 passed**，`targeted-final/s84.txt`；含 8 个 unit 和 1 个真实 CoreApp/TCP 重启用例 |
| 全量 unit | **366 passed**，`installed-final/unit.txt` |
| 免 API integration | **21 passed**，`installed-final/integration.txt` |
| mypy | **94 个源码文件通过**，`installed-final/mypy.txt` |
| 协议同源 | **通过**，`installed-final/protocol.txt` |
| 全仓 Ruff | **28 项遗留诊断，新增 0 项**，`installed-final/ruff.txt` |
| diff 格式 | `git diff --check` 通过；仅报告 Git 的既有 LF/CRLF 提示 |

专项故障注入覆盖 provider 生成失败、摘要插入后故障、checkpoint 切换后提交故障、并发尾部追加、
v2 到 v3 迁移、手动压缩跨重启继承，以及自动压缩后继续工具调用并提交最终回复。真实 daemon
用例通过 TCP 完成首轮、`session.compact`、停止、重启和第二轮；子进程假 provider 证明确实收到
摘要与新请求，不再收到已覆盖的原回复。第一次全量 unit 手工命令因未创建 `--basetemp` 父目录
产生 155 个 fixture 错误；创建专用父目录后同一源码重跑为 366 passed，未删除断言或 skip。

未验证真实摘要质量、真实模型、Linux/WSL2、Python 3.13、远程 MCP、断电/磁盘损坏、性能或
长期数据库增长。摘要采用累计前缀而非增量链；原始历史永久保留。S8.5 的前置条件是保留当前
未提交工作区并以本节证据复验 S8.1-S8.4；**本轮完成 S8.4 后停止，不开始 S8.5。**

## S8.5 实施内容

本轮只做最终验证、测量、CI 和交付文档，没有改变生产恢复状态机或 schema：

- 新增 `scripts/s8/run_s85_matrix.py`，一条命令运行 20 个确定性故障/恢复场景并输出机器可读分类。
- 新增 `scripts/s8/benchmark_s85.py`，固定原版 `cc352fe` detached worktree 与当前 S8 workload，
  保存逐样本原始值、中位数、p95、上下文、SQLite/WAL 增长和压缩成本。
- 新增 Windows/Python 3.12 免 API CI；清空 provider 凭据、禁用 dotenv、隔离 profile，排除真实付费 E2E。
- 新增 `VALIDATION.md`、`MIGRATION.md`、`KNOWN_LIMITATIONS.md`、`DEMO.md` 和待发布 release notes；
  更新 README、RUNBOOK 和 DESIGN，保留上游链接及 MIT LICENSE。
- 静态核对生产 `src/` 没有可由环境或配置启用的 S8 故障开关；私有事务回调默认 no-op。

矩阵首次统一运行是 20/20：显式 resume 后自动完成 6/6、安全暂停 10/10、终止失败 4/4。
无人值守重启自动续跑为 0 个场景且未实现，不能把 TUI Continue 计成该能力。

正式 benchmark 为 Windows 11/Python 3.12.14，预热 3 次、样本 15、每样本 200 条固定消息。
原版持久化中位数 52.9833 ms，S8 为 120.8903 ms，S8 多 67.9070 ms、为 2.282 倍；
没有性能提升。S8 从 reopen 到固定假 provider 完成并可查询的进程内控制面中位数 13.1216 ms，
不包含进程启动、TCP、Textual 绘制或真实模型。完整原始数据与限制见 `VALIDATION.md`。

最终门禁、真实 daemon/TUI 演示和当前工作区核验见 `docs/s8/evidence/s85/`。Linux/WSL2、
Python 3.13、真实模型/摘要质量、远程 MCP、断电/磁盘损坏和长期容量在 S8.5 未验证。
S8.5 验收后停止；未提交、未推送、未创建 PR/tag/release。

## S8.5 最终验收

| 检查 | 实测结果与证据（相对 `docs/s8/evidence/s85/`） |
| --- | --- |
| S8.4 前置专项 | **9 passed**，`preflight-s84/s84.txt` |
| S8.5 故障矩阵 | **20/20 场景**，`matrix-final/matrix/matrix.json` |
| 全量 unit | **366 passed**，`final-full/unit.txt` |
| 免 API integration | **21 passed**，`final-full/integration.txt` |
| 真实 daemon/TUI 探针 | **10 passed**，32 份操作日志、19 份 daemon 日志、8 个 artifact，`demo-final/probe.json` |
| mypy | **94 个源码文件通过**，`final-full/mypy.txt` |
| 协议同源 | **通过**，`final-full/protocol.txt` |
| 全仓 Ruff | **28 项遗留诊断，退出 1**，`final-full/ruff.txt` |
| S8.5 新增脚本 Ruff | **通过，新增 0 项**，`static-final/summary.json` |
| `git diff --check` | **通过**，仅既有 LF/CRLF 转换警告，`static-final/summary.json` |
| 受保护文件 | **摘要全部匹配**，`.test-tmp` 保留，`protected-files.json` |

CI workflow 尚未在 GitHub runner 上实际触发；这里只完成本地等价命令验证。CI 的无 API 保证来自
空 Key、禁用 dotenv、隔离 profile、固定 provider、显式排除真实付费 E2E 和只读仓库权限。
