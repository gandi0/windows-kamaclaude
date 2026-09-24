# S8：可恢复执行设计（更新至 S8.5）

本设计基于 [BASELINE.md](BASELINE.md) 的 Windows 实验。S8.1 已实现进程内工具执行语义，
S8.2 增加 SQLite 执行记录与检查点；S8.3 增加显式跨重启恢复及 TUI 操作；
S8.4 增加持久摘要与一致恢复视图；S8.5 收敛故障矩阵、测量、CI 和发布材料，
没有扩展恢复状态机。
下文先列实际实施范围，后面的完整目标设计不能视作均已实现。

2026-09-23：后续开发迁移到用户指定的 `F:\exercise\2026\kamaclaude - v1\KamaClaude`，
分支 `s8` 从本地 `stage/s7` 的 `cc352fe` 创建。沿用目标已有 Python 3.12.14；
保留原 3.13 验证记录，并补充两个解释器的超时兼容验证。详见 [STATUS.md](STATUS.md)。

## S8.5 实施记录

S8.5 不修改生产恢复语义。统一矩阵把 S8.1-S8.4 的事务、真实 daemon/TCP、Textual Pilot 和
摘要故障测试映射为稳定场景，保存注入点、预期/实际分类和分母。分类中的自动完成仅指显式
resume 后无需人工判断；启动扫描本身不无人值守调度旧任务。测试失败单列，不能塞进产品分类。

benchmark 以 detached `cc352fe` worktree 为原版，用同一解释器、固定消息和固定假 provider
对照当前 S8。原始样本与汇总分开保存；恢复延迟只测 S8，因为原版没有等价路径。数据库增长
同时记录 WAL 在线和显式 checkpoint 状态，token 只使用项目现有字符除以四估算。当前测量显示
持久化开销增加，不支持性能提升结论。

CI 在 Windows/Python 3.12 清空 Anthropic 凭据、禁用 dotenv、重定向 profile，并排除真实付费
E2E。生产源码没有故障环境开关；SQLite 的两个私有回调默认 no-op，注入只由测试进程内改写。
崩溃 helper 的类级 monkeypatch 仅存在于独立子进程，进程退出后失效。

## S8.4 实施记录

schema v3 新增不可变 `summaries` 表，摘要是从最早有效消息开始的累计前缀，记录稳定 ID、
session 版本、`from_seq/to_seq`、来源 run/checkpoint version、文本和生成配置。采用累计前缀是因为
checkpoint 只有一个有效摘要引用；不建立隐式摘要链，恢复时无需递归拼接多个版本。

快照阶段固定 checkpoint version、旧摘要引用、覆盖边界和原始消息 digest；provider 在事务外
运行。提交以 `BEGIN IMMEDIATE` 短事务校验这些条件，插入摘要并 CAS 切换 checkpoint 指针。
并发新增的尾部可以存在，但只有快照的 `to_seq` 被覆盖，checkpoint 消息边界推进到最新已提交
seq。诊断 Markdown 和事件均在数据库提交后产生，不参与恢复判断。

恢复始终先校验完整原始消息的工具配对，再构造摘要交接对和 `seq > summary_to` 的原始尾部，
最后应用既有模型投影截断。未配对、dispatching 或 unknown 调用不能被摘要隐藏。新主 run 继承
上一主 run 的有效引用；手动与自动压缩走同一持久 API。默认自动压缩仍关闭。

## S8.3 实施记录

本轮只实现恢复执行。schema v2 从 v1 增量迁移，保留既有会话、原始消息、调用、attempt、
检查点和旧导入 BLOB；未知新版本仍拒绝打开。新增业务请求、daemon epoch、工作区身份、
文件前置状态、审批、人工核查记录和 child 结果。不激活摘要指针，不实现 S8.4。

daemon 在打开执行库和启动扫描前，持有规范化数据目录的 OS 文件锁；不同端口使用同一
数据目录也不能启动第二个 daemon。锁依赖打开的文件句柄，进程退出自动释放，不按 PID
或锁文件是否存在判断。SQLite 另检查当前 epoch 和 run owner，并条件更新认领状态。
内存 Task/Lock 用于调度，不能替代这些持久检查。

启动扫描只重建索引和分类，不执行模型或工具。未完成任务显示 interrupted；有未提交结果
的 dispatching 或 unknown 调用显示 needs_review，原 attempt 事实保留。已提交结果仍按
call_id 查询，恢复上下文复用其原始 tool_result，不再次调用工具。planned 调用重新检查
权限和文件状态后派发；ready 的安全重试保留已有 attempt 编号和总预算。模型输出截断的
意图保存 dispatch_allowed=false，恢复也只追加“未执行”的错误结果。

`session.send_message` 接受独立业务 request_id，`(session_id, request_id)` 唯一；
相同键和相同原文返回原 run，键相同而内容不同则报冲突。RPC 在可靠接受后返回，任务属于
daemon，不依赖客户端连接寿命。旧客户端省略 request_id 仍可使用，但不具备重发去重。
模型 tool_use_id、JSON-RPC id 和参数 hash 都不是业务去重键。

新增 `session.list/status/resume/review` 协议。resume 条件认领当前主任务，一次成功认领
才创建一个协程。后台 child 不通过该入口恢复。恢复保留原 run/step/goal/技能提示及白名单；
不会重新追加用户输入或已提交的模型响应。工作历史只有最后一批明确 planned/ready 的调用
允许暂时未配对，补齐这些已提交意图后才允许请求模型；新输入仍要求完整配对。

工作区校验包括规范路径、目录 dev/ino 身份以及相关文件路径、存在性与内容摘要；重启前
没有身份记录的旧未完成任务保持核查，不能用重启时观察到的目录伪造旧身份。文件工具重新
解析原始入口，防止 junction/symlink 换向后写入另一目标。已确认工具结果更新已知文件预期，
恢复比较累计状态。任意 Shell/MCP 的读写集合无法完整推断；文件检查也不是锁住外部编辑器，
不能消除检查与外部操作之间所有竞态。

ASK 审批先记录 call、完整 scope、随机 approval_id 和 daemon epoch，再通知 TUI。
批准/拒绝由条件更新接收，工具消费审批后才进入 dispatch 边界。重启前待决或尚未完成的
一次性授权不会直接放行；恢复必须得到新请求 token。持久 policy.toml 的既有授权范围继续
按原规则评估；不把一次性授权升级成持久授权。

TUI 可选择磁盘旧会话、查看原始历史和完整状态、恢复 interrupted 任务。未知操作显示完整
参数、workspace、call/attempt、可确认事实与原因。首版核查选择是 pause 或 abandon，均记录
用户说明；abandon 同事务关闭 session 并停止安排主任务，保留 unknown call/attempt，不补造
成功 tool_result，不撤销外部副作用。重新尝试未知操作或人工导入成功结果不在本轮接口中。

spawn 前提交 child 与父 run 的关系，完成结果可被新 Runner 查询。daemon 共享活跃 registry，
退出时取消其持有的后台协程。重启后的未完成 child 显示 interrupted/needs_review；child 内部
仍沿用既有执行路径，未新增逐工具恢复或任务树接管，不能把持久父子关系解释为整树恢复。

故障入口仅位于测试 helper，使用真实 CoreApp 子进程、TCP JSON-RPC、SQLite 和本地工具，
固定 Provider 不调用付费 API。明确屏障覆盖提交后的恢复与副作用未知窗口；Textual Pilot
通过真实 socket 操作 TUI。具体实测结果和限制以 [STATUS.md](STATUS.md) 及证据为准。

## S8.2 实施记录（历史）

SQLite 为新会话元数据、run 状态与执行记录的唯一权威。`SessionStore` 使用
`sessions/execution.sqlite3`；独立 `AgentRunner` 使用其 runs 目录下的 `execution.sqlite3`。
`meta.json/thread.jsonl` 不再参与新格式双写。事件 JSONL、notes 与 summary 文件仍是诊断/附件，
不能证明某个工具结果已可靠提交。存储使用现有 Python 标准库 sqlite3，未新增依赖。

schema `PRAGMA user_version=1` 实际包含 `sessions/runs/messages/tool_calls/attempts/checkpoints`
以及 `legacy_imports/legacy_rows`；新于支持范围的版本在 WAL 设置前拒绝打开。
WAL、FULL、foreign_keys、busy_timeout=5000；单次写入使用 `BEGIN IMMEDIATE`，同一连接短事务串行。
仅接受绝对本地数据库路径，拒绝 UNC；映射网络盘和同步软件不能完备识别，使用者须选本地专用目录。
进程硬退出验证不等于断电、磁盘损坏或文件系统持久性验证。

实际提交次序如下：

1. 接受消息：run + user 原文 + 初始 checkpoint + session run_ids 一次提交。
2. 模型响应：assistant 原文 + 全部工具意图（内部 UUID、模型 ID、完整参数及 hash、效果类别、retry_safe、workspace）一次提交。
3. 每次派发：审批/参数校验完成后，独立 attempt UUID 和递增序号先提交，才进入工具本体。
4. 可确认结果：attempt 完整结果、call 状态、原始 tool_result 消息、checkpoint 同一事务提交后才允许推进。
   安全重试的中间暂时失败只提交 attempt、call=ready 与 checkpoint，不生成重复的模型 tool_result；
   最终结论才生成该调用唯一的 tool_result。退避取消保留已提交的暂时失败 attempt。
5. 最终回复：assistant + run=succeeded + checkpoint 一次提交，成功后才发 run.finished success。

事务中不等待模型、Shell、MCP、审批或 retry sleep。执行前写入失败不进入工具；
执行后失败不重试、不执行下个工具、不发下一模型请求，Runner 报 `needs_review/storage_error`。
该诊断不代表 needs_review 标志写入成功；数据库可能仍是 `running + dispatching`，这是保留未知窗口的证据。
下一阶段必须先扫描并解释这些窗口，不能按 running/dispatching 自动重放。SQLite 不提供外部副作用 exactly-once。

已计划参数在调用入口深拷贝并与持久意图核对；不同 call_id 的相同参数仍是不同操作。
同一个已开始的 call_id 或已有响应的 run_id 在当前进程被拒绝重新执行，成功结果可通过 `get_call(call_id)` 查询。
本阶段没有实现从查询结果恢复循环。单 session 未完成主 run 的事务检查仅用于阻止继续覆盖不明状态；
业务请求 ID、跨进程拥有权、启动扫描、一次性审批 epoch、恢复 UI 均尚未实现。

原始消息使用 UUID 与 session 内稳定 seq，追加后无更新/删除接口，与 `context.messages` 完全分离。
每个工具结果一条原始 user 消息；送模型前合并相邻工具结果，并作既有输出截断，库内结果不截断。
未配对批次保留完整原文并阻止新请求，不再 trim 掉已执行结果。自动压缩更换工作列表不会丢失原文。
手动压缩保存的是当前 SessionStore 的内存工作投影，原 JSONL 不再覆盖；重启后投影消失，
checkpoint 只预留 `summary_ref/summary_from/summary_to`，没有有效摘要指针或持久摘要恢复。
正式摘要版本、覆盖范围提交与恢复行为仍属于 S8.4，自动压缩默认值仍为 0。

会话创建时绑定绝对 workspace；主 run 文件工具和 Shell 显式使用该目录，避免 daemon cwd 后续变化。
Shell 仍是 Windows cmd。尚未实现恢复时目录身份、junction/文件冲突复核。
父 run 的 `spawn_agent` 调用被持久记录为 unknown 效果；子 agent 内部仍为原 S7/S8.1 执行路径，
子任务详细记录、后台 registry 生命周期与中断可见性留给 S8.3，不宣称任务树恢复。

旧格式通过 `scripts/s8/import_legacy.py <旧会话目录> --database <绝对SQLite路径>` 显式导入。
不加载模型、真实用户配置，不覆盖/重命名/截断源文件。来源规范化路径与 meta/thread SHA-256 标识快照；
保存两份源 BLOB、逐行原始 bytes、行号、解析分类与消息 UUID/seq 映射。合法重复内容按行保留，
坏 JSON、未知 role、孤儿调用留作取证；没有创建可自动恢复的 call/attempt。
完全相同快照返回原 import_id；前缀及完整行边界证明为追加时只增加后缀消息；
重写或元数据变化保守隔离为新 session 并报告 conflict。缺失 workspace 保持未绑定，不能直接执行。

验收脚本 `check_baseline.py --check s82` 覆盖实际文件副作用、DB 重开、事务回滚、重试与取消、
顺序稳定及无损导入；`--check s82-crash` 在 5 个明确屏障执行真实子进程硬退出并重开取证。
故障钩子仅在测试/探测进程 monkeypatch，没有正常 daemon 的环境变量或配置故障开关。
具体命令与实测结果见 [STATUS.md](STATUS.md)。

## S8.1 实施记录（历史）

- `BaseTool.effect` 分为 `read_only/write/unknown`，默认 unknown；`retry_safe` 默认 false。
  读取文件/目录和任务查询明确允许安全重试；任意 Shell、写工具、spawn 和 MCP 不自动重试。
  暂时性来自受控工具的 `transient=True`、安全读取的 EAGAIN/EINTR/EBUSY 或限流错误，
  仍需同时通过安全契约、已知结果、剩余次数和取消状态检查。上限为首次 + 2 次重试。
- 一个调用生成一个 UUID `call_id`，每次实际执行有独立 UUID `attempt_id` 和递增序号。
  执行层可接收未来存储层预分配的 call_id；当前不按该 ID 去重或查询持久结果。
  `ToolResult.attempts` 保存本次调用的结构化结果，诊断事件新增可选 ID 字段，旧载荷仍能解析。
  校验/拒绝/审批超时属于 `not_started`，attempt 列表为空；诊断事件 attempt=0。
- `outcome=known/not_started/unknown` 与工具效果类别分开。Shell exit 7 是已知失败，
  但不是重试许可；超时、执行中取消、MCP 失败/失联、清理未确认按未知处理。
  MCP 的协议错误与结果中的 `isError=true` 都进入此路径，不根据工具名猜测无副作用。
  `cleanup_confirmed=True` 只证明本地受控进程树已退出，不证明副作用回滚或远端操作停止。
- 参数错误、权限拒绝、审批超时、执行超时、取消分别记录为 schema_error、permission_denied、
  approval_timeout、timeout、cancelled；清理失败为 cleanup_failed。取消继续向上抛出，
  审批 pending 在取消时移除；退避及同批工具循环不会在取消后继续。
  内置工具用 Pydantic 前置校验；task_create/get/update 补充严格参数模型，不再把非法值
  强转后执行。MCP 远端 JSON Schema 尚无本地验证器，其远端参数错误仍保守归入未知。
- Windows 使用父进程持有的 `KILL_ON_JOB_CLOSE` Job Object。受控 Python 引导进程先加入
  Job 并握手，随后才从管道接收用户命令；加入失败或父进程提前断开不会执行命令。
  终止作用于 Job，不按 PID 杀进程。取消/超时等待管道回收、直接进程退出、Job 活跃计数归零，
  并等待终止前快照中仍属该 Job 的成员进程句柄置位，避免只看活跃计数的提前确认竞态。
  重复取消会延后传播，直到清理结论产生。正常命令结束也回收遗留后台进程。
  Job 约束不覆盖通过服务管理器、任务计划程序等外部机制启动的工作。
- Windows 明确使用系统 cmd；PowerShell 配置尚未实现。非 Windows 使用独立 POSIX 进程组，
  不能约束主动 setsid 逃逸的后代，且本轮未实测该路径。
- Loop 遇到结果未知时终止当前 run，使用 `needs_review` 与相应 reason；TUI 显示需要核查。
  本轮未实现持久状态机/恢复 UI；SessionManager 下一次用户消息仍是新的 run，不能把它当恢复。
- 为满足 Windows 和现有 Python 主目标，加入 Windows 信号桥接、无 Key 的惰性 Provider，
  支持范围为 `>=3.12,<3.14`。新主工作区保留 S7 的 `.python-version=3.12` 和原虚拟环境；
  原检出的 3.13 安装与测试作为历史证据保留。旧 compactor 单测用各自的 asyncio.run。
  Python 3.12 不自动将 CancelledError 子类转为 TimeoutError，执行层改为持有明确的
  asyncio.timeout 截止状态，同时检查外部取消计数，保留清理结果与 cleanup_failed 分类。

S8.1 只完成上述部分。下文中的数据库、业务请求去重、工作区绑定、迁移、摘要指针、
恢复核查等要求仍是 S8.2–S8.4 的设计约束，不是当前能力。

## 1. 目标平台与范围

按用户最新要求，以 **Windows 原生 + 目标仓库现有 Python 3.12.14** 为主环境，现有 3.13.0 为额外回归环境。TUI 为主要入口，CLI 用于诊断。任务书原来的 Linux/WSL2 优先被覆盖；后者为后续可选验证。

首先覆盖单用户、单 daemon、主任务、内置工具。后台子代理首版持久化父子关系及可见中断状态，不自动恢复整个任务树。MCP 默认副作用未知。恢复重建状态和下一步，不复活 Python 协程或接管身份不明的外部进程。

任意 Shell、远程调用与 SQLite 不能原子提交，因此不承诺外部操作 exactly-once。恢复目标是“已确认结果复用；能证明尚未开始的操作在重新校验后执行；结果不明时暂停核查”。

## 2. 唯一权威存储

采用 Python 标准库 SQLite，无额外数据库服务。S8.2 起以数据库作为执行与会话状态的唯一权威，JSONL 只作旧数据输入、导出和诊断，禁止双向双写后任意选择一个版本。events/trace 为尽力写入的观察数据，不能决定恢复路径。notes/context 为有来源的上下文附件，快照/hash 随 checkpoint 记录；不以它们证明工具已执行。

默认数据目录为本机用户目录的 `.kama`，可显式配置绝对路径；Windows 首版支持本地 NTFS。数据库不得默默落在相对 cwd、UNC 网络共享或跨主机同步目录中。实现时识别不了同步目录就明确要求使用本地专用目录，不能承诺任意盘符上都安全。

启用 `foreign_keys=ON`、`busy_timeout`、`journal_mode=WAL`、`synchronous=FULL`；连接和写入由单 daemon 管理，事务短小。数据库及 WAL/SHM 属于同一备份单元，使用 SQLite backup API，不能运行时只复制主文件。断电持久性仍依赖 OS/磁盘行为，须单列验证。表有明确 schema_version 和逐步迁移；发现较新未知版本时拒绝写入。

| 逻辑表 | 最小内容与约束 |
| --- | --- |
| sessions | UUID、title、open/closed、workspace 绝对路径及身份、版本、当前主 run 引用 |
| runs | UUID、session、parent_run、kind、业务 request_id、状态、owner_epoch、版本、错误、创建/更新时间；`(session_id, request_id)` 唯一 |
| messages | UUID、session、单调且唯一 seq、run、role、原始 content、来源；原始消息追加保存 |
| tool_calls | 内部稳定 call_id、模型 tool_use_id、run/step/序号、参数及 hash、效果类别、状态、workspace、权限范围 |
| attempts | 独立 attempt_id、call_id、attempt_no、phase、开始结束、进程身份、结构化结果/错误；`(call_id, attempt_no)` 唯一 |
| checkpoints | run、版本、已提交消息边界、下一步、有效摘要引用、待处理调用和相关文件前置状态 |
| summaries | UUID、版本、覆盖的稳定 seq 区间、源 checkpoint/version、摘要文本、生成配置版本 |
| approvals | 请求 ID、call_id、参数/权限 scope、决策、daemon epoch、是否已消费；一次性授权不可跨 epoch 使用 |
| legacy_imports / legacy_rows | 源路径、文件快照 hash、行号/原始 bytes/解析状态、映射关系、导入进度 |

child run 复用 runs 与 calls/attempts，避免另造第二套任务状态机。原始消息和工具完整结果保存为权威数据，模型截断仅是上下文投影；敏感参数/输出继承本地访问保护，不额外上传。

## 3. 状态机

session 仅表示 open/closed；“忙/待审批/可恢复”等 UI 状态从持久 run 派生，避免两个字段矛盾。run 使用以下状态：

```text
created -> running
running -> waiting_approval | interrupted | needs_review | succeeded | failed | cancelled
waiting_approval -> running（当前有效授权通过） | interrupted | cancelled
interrupted -> running（用户请求恢复、重新校验并占有执行权）
interrupted -> needs_review（发现无法确认的副作用）
needs_review -> running（明确核查结论已提交） | cancelled（未知历史仍保留）
```

`succeeded/failed/cancelled` 为终态；另起业务任务创建新 run。`cancelled` 表示不再安排新动作，不隐含外部副作用回滚。无法确认进程是否停止或结果是否提交时，先保持 needs_review，不能把取消请求直接当作已清理。

call 使用 `planned / waiting_approval / ready / dispatching / succeeded / failed / unknown / cancelled`。每次 attempt 独立记录 `dispatching -> result_committed`，并有 success/known_failure/unknown/cancelled 分类与 error_class。调用失败不必使 run 失败：已提交的已知错误结果可以交模型处理；未知调用必须阻止后续模型请求。

重启扫描：旧 epoch 的 running/waiting_approval 不能继续显示正常运行。有 dispatching 且无已提交结果的写操作、Shell 或 MCP，call 标 unknown，run 标 needs_review；其余未完成 run 标 interrupted。没有在 scan 时自动重放用户任务。子 run 同理显示 interrupted 或 needs_review，首版无自动接管。

## 4. 提交边界与不变量

1. **接受请求事务**：查业务 request_id；新请求写 user message、run、workspace、初始 checkpoint。客户端重试返回原 run_id。临时 JSON-RPC id 不参与业务去重。
2. **模型响应事务**：写 assistant 原始消息及全部 tool_use 的调用意图、稳定 call_id、完整参数、效果类别、权限和工作区资料。提交失败不启动任何工具。同一步多个工具也各有 call/attempt，不因批处理扩大重放范围。
3. **审批提交**：请求持久化后才发 TUI 事件；审批响应条件更新并绑定 call 参数、scope、epoch。已失效、重复响应不扩大授权。
4. **派发提交**：权限和冲突检查通过后，先提交 attempt=dispatching，然后调用工具。只有仍在 planned/ready 且无 dispatching attempt，才能证明执行边界尚未跨过。
5. **结果事务**：工具返回后同时提交 attempt 结果、call 状态、对应 tool_result 原始消息及 checkpoint；成功后才能推进下一工具/模型步骤。只有临时性错误、明确安全重试、未取消且预算充足时，另建 attempt 重试。
6. **最终回复事务**：提交最终 assistant 消息、检查点、run=succeeded 后，才对外发布成功。诊断事件发出早晚不改变数据库事实。

事务中不等待 LLM、Shell、MCP 或审批。执行前数据库失败：工具不得启动。执行后提交失败：立即停止安排新动作；尽力通知“状态未提交”，不可声称 interrupted 已持久化，因为数据库本身可能不可写。重启以后根据仍然存在的 dispatching 意图保守进入 needs_review。不得仅依靠同一失败数据库再写一条“失败标志”来实现安全。

| 崩溃位置 | 恢复判断 |
| --- | --- |
| 意图尚未提交 | 不允许已有外部副作用；重新取得模型决策需按请求状态判断 |
| 意图已提交、没有 dispatching attempt | 确认未跨执行边界，重新校验权限及工作区后可执行 |
| dispatching 已提交、实际启动前或执行后但结果未提交 | 两个窗口无法可靠区分；Shell/写/MCP 都需要核查，不能因“可能没启动”自动重放 |
| result + message + checkpoint 已提交 | 按 call_id 复用结果，不再产生该调用的副作用 |

参数 hash 仅核对完整性，不合并不同 run 中合法的相同命令。模型声称“幂等”不是安全契约。模型请求本身也不保证重启前后不重复计费；可选真实模型评测必须另行授权。

## 5. Windows 执行与取消

S8.0 已复现无 Key 启动耦合及 `add_signal_handler` 不支持。S8.1 已实现惰性 Provider、
Windows 信号桥接和 Job 进程控制；以下保留完整设计约束，未完成项单独列明：

- Core 启动应能创建/查看会话而无需模型 Key，Provider 延迟到实际模型调用或通过测试依赖注入构造。缺 Key 时返回明确配置错误。
- 关闭机制提供平台适配：Windows 主线程 signal 回调桥接 asyncio shutdown；无需 Unix loop 信号接口。强杀和控制台异常关闭可能无法写收尾，依赖下次启动扫描，不能把优雅退出测试替代硬退出测试。
- 保留现有工具名 `bash` 以兼容历史，记录实际 shell。首版 Windows 默认明确为系统 `cmd.exe /d /s /c`；PowerShell 作为显式配置的 executable + 参数列表，不隐式混用 POSIX 语法。路径参数构造和 shell 字符串分别处理。S8.0 已确认现有 BashTool 在 Windows 调用 cmd；尚未实施上述显式配置。
- 任意 Shell 默认 `unknown_effect`、不自动重试。read_file/list_dir 等可声明 read_only；write_file、notes、task 修改依工具契约分类。只有工具自身实现或受控服务契约保证的操作才标 retry_safe。
- 进程控制器区分“取消请求”“父进程退出”“进程树确认清理”。Windows 使用 Job Object 管理 Shell 子进程树，并在派发命令前完成加入；`CREATE_NEW_PROCESS_GROUP` 单独不能保证后代清理。清理等待使用已确认 Job 成员的内核句柄，避免 PID 复用误判。Job 创建/加入失败且未派发命令为 not_started；派发后无法确认清理则为 unknown。
- 取消后不再重试/发模型请求；等待可确认的清理及结果提交。超时、外部退出非零、权限拒绝、参数错误、取消分别编码。MCP 远端操作不因本地连接断开而推断取消成功；stdio 后台进程生命周期以后复用同一控制接口，不自动重放 MCP。

S8.1 已实测 Windows 子/孙进程取消与超时、重复取消、Job 建立失败不执行、
清理未确认返回 unknown、无 Key 启动及 SIGINT 桥接、本地 editable 包安装。
仍未验证任意并发派生/特殊 Job 嵌套、父进程硬退出时的 Job 回收、GUI 控制台关闭、
PowerShell 或远端 MCP 取消；不能把普通取消通过外推为这些场景已通过。

## 6. 工作区、权限与单一执行权

session 创建时持久化规范化绝对 workspace，run 复制其身份和版本。文件工具显式接收该工作区；Shell 显式传 cwd；不得用 daemon 全局 `os.chdir()` 在会话之间切换。

Windows 路径解析处理盘符、大小写、UNC、junction/symlink；解析后的目标必须满足既有授权范围，不能只检查字符串 `..`。首版使用解析路径与相关文件存在性/hash 校验；不能可靠验证 junction/目录身份变化时暂停核查。文件句柄 ID 是否纳入严格身份判定留给实现验证，不把路径字符串当永久身份。

恢复前验证 workspace 存在且身份匹配。继续写文件前比较预记录的相关文件 hash/预期状态；文件变化、目标路径解析变化或权限变化进入 needs_review。任意 Shell 的读写集合无法完备推断，不能宣称已做全面冲突检测。

同一 session 的非终态主 run 最多一个：数据库唯一约束/条件更新占有执行权，owner_epoch 区分 daemon 生命周期；内存锁只减少竞争。interrupted/needs_review 同样占用主任务入口，用户必须恢复或明确终止它。单 daemon 通过系统级实例锁和数据库检查保证，端口探测不单独充当数据锁。重复恢复请求只返回已存在的执行状态。

一次性 allow/待决审批跨 epoch 失效；恢复创建新请求并按当前规则检查。原持久 policy.toml 授权按原有工具范围读取兼容，不升级为更宽授权，不把 session 内存授权迁移为全局授权。核查未知调用的 TUI 必须展示命令/参数、目录、调用 ID、attempt、可确认事实与未知部分；允许继续暂停、终止任务、附证据确认结果或明确授权新的尝试。用户选择跳过时保存明确的人工处理结果，不能虚构工具成功。

## 7. 原始记录、摘要与模型上下文

原始消息使用稳定 seq，与运行时可变 `context.messages` 分离。摘要是覆盖完整消息组的派生视图，保存覆盖范围、源 checkpoint 版本、prompt/config 版本及生成文本。摘要不覆盖/删除原始记录，压缩也不承担工具提交。

生成时取得快照 `(from_seq, to_seq, checkpoint_version)`，事务外调用固定或真实摘要模型；提交摘要与 active_summary 指针使用短事务和版本比较。失败保持原指针/检查点；源版本改变则重新校验或丢弃候选，不能丢弃同时新增的消息。

重建规则：已提交且适用的摘要对 + `seq > covered_to_seq` 的完整近期消息组。一个 assistant 消息中的每个 tool_use 必须恰有匹配 tool_result；只有全部配对才允许把组压入摘要。结果未知时暂停，不删除孤儿调用或伪造成功来绕过模型格式要求。截断仅影响送模型的工具输出，完整执行记录仍可查。

手动 compact 和自动 compact 共用同一提交/重建流程，不再以 `prefill_len` 切片判断持久化增量。自动压缩默认仍关闭（0.0）；若以后扩大启用范围需独立行为测试。中断点覆盖生成前、生成后未提交、指针提交后；固定摘要只验证结构/提交语义，真实摘要质量单列。

## 8. 旧数据与后台子代理

旧 session 导入不覆盖、重命名或截断任何原文件。源规范化路径 + 文件内容 hash 标识一次快照；完全相同重复导入返回原批次。逐行保存行号、原始 bytes、解析状态；非法 JSON、未知 role、孤儿调用保留取证，不通过现有 `read_messages()` 的裁剪视图导入。

追加型源用已导入前缀 hash + 行号验证只增加后缀；中间重写/旧 compact 造成变化时作为新快照隔离并报告冲突，不能盲目拼接。消息 UUID 映射绑定来源行，不能按内容 hash 合并重复但合法的用户输入。导入记录与映射在同一事务中提交，可重复执行。

旧 run/事件仅标 legacy provenance；没有执行前意图与原子结果证明，不能构造为可自动恢复 attempt。缺失 workspace 的旧会话保留未绑定状态，执行前由用户绑定。新格式导出为派生文件，不能再回写成第二权威。

后台 spawn 前提交 child run 与父子关系；查询从持久结果读取，内存 registry 仅管理活跃 Task，生命周期属于 daemon/session 而非单次 Runner。重启将旧未完成子任务显示 interrupted/needs_review，保留可查信息，不自动续跑。父 run 状态不覆盖子 run 状态，也不因父任务成功就推断所有子任务成功。

## 9. 分阶段入口与验证门槛

| 阶段 | 工作与必需证据 |
| --- | --- |
| S8.1 | 将 F03 改成正常回归；效果类别、重试约束、attempt 标识、取消/Windows 子进程清理；安全读取暂时失败仍能有限重试；处理正式 Python 3.13 运行声明及必要平台适配，不声称恢复已完成 |
| S8.2 | SQLite schema/迁移、意图与结果事务、稳定消息 ID/seq、workspace、旧数据无损幂等导入；注入执行前提交失败须无 marker，执行后失败不得推进 |
| S8.3 | 启动扫描、请求去重、审批失效、执行权/冲突检查；Windows 真实 daemon 启动—硬中断—重启—恢复及 TUI 演示；F01/F02/F05 扩为持久状态回归 |
| S8.4 | 摘要事务与重建；F04 转为正向回归；完整配对和并发新增不丢失；默认启用范围不静默改变 |
| S8.5 | **已完成** Windows 故障矩阵、文档、演示和固定 workload 测量；分别统计安全暂停、显式恢复后完成与终止失败；未自动发布 |

后续仍需验证 Job 嵌套限制、硬退出清理、SQLite 本地盘断电行为及旧持久授权兼容。
S8 止于 S8.5。Linux/WSL2、Python 3.13、真实模型和长期容量仍未验证；不自动开始其他阶段。
