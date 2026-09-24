# S8 验证与复现

更新：2026-09-24。本文只报告当前 `s8` 未提交工作区在 Windows 11、Python 3.12.14
上的实测结果。所有核心检查使用固定假 provider、隔离用户 profile 和本地临时目录，不读取
项目 `.env`，不需要 API Key，也不调用付费模型。

## 一条命令运行故障矩阵

```powershell
.venv/Scripts/python.exe scripts/s8/check_baseline.py `
  --check s85 --output docs/s8/evidence/s85/matrix-local
```

实际矩阵有 20 个场景、19 个唯一行为断言节点。一次端到端测试同时验证“已提交结果复用”
和“双客户端单一 claim”，因此场景分母大于 pytest 节点数。首次统一运行结果：

| 观察分类 | 分母 | 通过 | 含义 |
| --- | ---: | ---: | --- |
| 显式恢复后自动完成 | 6 | 6 | 用户或客户端先请求 resume；之后无需人工判断事实 |
| 安全暂停 | 10 | 10 | 不重放未知副作用，或保留旧权威摘要/上下文 |
| 终止失败 | 4 | 4 | 当前操作或竞争 daemon 终止，且不报告成功 |
| 测试失败/不确定 | 20 | 0 | 不计入上述三类 |

daemon 重启后不会无人值守自动续跑；该分母为 **0**，不是 6。这里的“自动完成”始终带有
“显式 resume 后”的前提。逐项注入点、nodeid、观察事实和分类见
`evidence/s85/matrix-final/matrix/matrix.json`，原始 pytest 输出见同目录 `pytest.txt`。

矩阵覆盖：意图/dispatch/result/final 提交边界、已提交结果、未知副作用、并发 claim、一次性
审批、workspace 路径及文件变化、完成/中断 child、摘要生成和事务故障、并发尾部、daemon
所有权、真实 SocketClient/Textual Pilot 的 Continue 与 Pause。竞态由事件或文件屏障推进；
状态轮询只等待可观察谓词，不用固定 sleep 猜测故障窗口。

## Benchmark 方法

原版是 detached worktree 中的 `cc352fe653c26ab763838899785b6bdd6575f0c0`；S8 是相同 HEAD
上的当前未提交 S8.1-S8.5 工作树。两者使用同一个 Python 3.12.14 与同一组已安装依赖。

```powershell
git worktree add --detach F:/path/to/s85-baseline cc352fe653c26ab763838899785b6bdd6575f0c0
.venv/Scripts/python.exe scripts/s8/benchmark_s85.py `
  --baseline-root F:/path/to/s85-baseline `
  --output docs/s8/evidence/s85/benchmark-local `
  --samples 15 --warmup 3 --messages 200
```

每个样本使用新临时目录。时钟为 `time.perf_counter_ns()`；先预热 3 次，正式记录 15 次。
消息内容、长度、角色顺序和固定摘要文本不变。原始逐样本值在
`evidence/s85/benchmark-final-v2/raw.json`，汇总在 `summary.json`。

| 指标 | 原版中位数 | S8 中位数 | 口径 |
| --- | ---: | ---: | --- |
| 持久化 200 条消息 | 52.9833 ms | 120.8903 ms | 同一公开 `append_message` workload |
| 读取 200 条消息 | 0.6146 ms | 1.1874 ms | 构建可送模型消息列表 |
| 固定摘要本地提交 | 0.8653 ms | 2.2849 ms | 原版替换 JSONL；S8 原子写摘要并切 checkpoint |
| 固定假 provider 摘要生成 | 0.0492 ms | 0.0534 ms | 无网络、同一固定输出 |

S8 持久化比原版中位数多 67.9070 ms，即这个 workload 下为 2.282 倍；按 200 条消息简单
摊分约多 0.340 ms/条。两套存储提供的持久性和恢复语义不等价，这不是端到端吞吐或真实模型
延迟结论，更不是性能提升。

S8 恢复延迟只测 S8，原版没有等价恢复路径，不能填 0：

| S8 恢复阶段 | 中位数 | p95 | 定义 |
| --- | ---: | ---: | --- |
| 打开 SQLite | 1.7418 ms | 3.2178 ms | 构造 `SessionStore` |
| 启动扫描 | 1.9745 ms | 2.5501 ms | `start_daemon` 分类旧 epoch |
| 内存索引 | 0.1959 ms | 0.6009 ms | 构造 `SessionManager` |
| claim | 1.7649 ms | 3.5811 ms | 显式 resume 到返回 started |
| 继续到终态 | 6.4212 ms | 8.9611 ms | 固定假 provider 最终回复提交 |
| 状态查询 | 0.2931 ms | 0.5201 ms | 构造 UI 可查询 status |
| reopen 到可查询终态 | 13.1216 ms | 17.2861 ms | 上述本地控制面总边界 |

该恢复计时是进程内控制面，不包含 OS 拉起 daemon、TCP 建连、Textual 绘制、真实模型或工具
时间。真实 daemon/TUI 的正确性由演示和矩阵验证，不能把这里的 13.1216 ms 当作用户可见恢复
延迟。

## 上下文、数据库和压缩

固定 200 条历史在 S8 中从 200 条、51,800 内容字符、58,101 canonical JSON UTF-8 bytes、
12,950 个项目粗略 token，变为 2 条、71 字符、135 bytes、17 个粗略 token。token 口径严格为
`sum(len(str(content))) // 4`，不是 Anthropic tokenizer；固定摘要不代表真实摘要质量或真实成本。

数据库增长 workload 是 42 条消息、20 个 tool call、20 个 attempt、1 个摘要。显式
`wal_checkpoint(TRUNCATE)` 后，空 schema 的主库+WAL+SHM 为 204,800 bytes，最终为 241,664
bytes，增长 36,864 bytes。WAL 在线时摘要前后分别为 2,170,072 和 2,190,672 bytes；因此长期
容量判断必须同时说明 checkpoint 状态。本次只有一个短 workload，不能外推长期增长可接受。

## 完整门禁

```powershell
.venv/Scripts/python.exe scripts/s8/check_baseline.py --output docs/s8/evidence/s85/final-full
.venv/Scripts/python.exe scripts/s8/probe_s83.py --output docs/s8/evidence/s85/demo-final/probe.json
git diff --check
```

`check_baseline.py` 的 integration 明确忽略真实付费 `test_run_e2e.py`。隔离环境清空
`ANTHROPIC_API_KEY` 和 `ANTHROPIC_AUTH_TOKEN`、禁用 dotenv、重定向用户 profile。CI 文件
`.github/workflows/s8-no-api.yml` 使用同一入口。全仓 Ruff 仍有 28 项 S8 前已存在的诊断，
其非零退出必须单独报告；S8.5 新增脚本使用定向 Ruff 检查。

## 证据与限制

- `evidence/s85/preflight-s84/`：S8.4 开工复验，9 passed。
- `evidence/s85/preflight-full/`：366 unit、21 integration、mypy、protocol 与遗留 Ruff。
- `evidence/s85/matrix-first/`：统一 20 场景矩阵。
- `evidence/s85/matrix-final/`：通过一键入口复跑的最终 20 场景矩阵。
- `evidence/s85/benchmark-final-v2/`：正式原始样本和汇总。
- `evidence/s85/benchmark-final/`：首版正式测量；数据库初末 WAL 状态不可直接相减，保留作方法迭代历史。
- `evidence/s85/benchmark-smoke/`：2 样本脚本自检，不用于结论。
- `evidence/s85/demo-final/`：10/10 真实 daemon/TCP/TUI 探针、操作日志和截图。
- `evidence/s85/final-full/`：366 unit、21 no-API integration、mypy、protocol 和全仓 Ruff。
- `evidence/s85/static-final/`：S8.5 定向 Ruff、协议、mypy 和 diff-check 结果。
- `evidence/s85/protected-files.json`：受保护文件摘要复核。

真实模型质量和费用、远程 MCP、断电/磁盘损坏、长期容量、Linux/WSL2、Python 3.13 均未在
S8.5 验证。WSL2 发行版存在但虚拟化服务不可用；独立 Python 3.13.0 存在但没有 pytest，
未另建或下载环境。探测见 `evidence/s85/platforms.json`，完整限制见
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)。
