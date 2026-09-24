# S8 待发布变更说明

状态：仅准备，未创建 tag、release、提交或推送。

## 新增

- SQLite v1-v3 执行记录、稳定消息/call/attempt/checkpoint 和不可变摘要。
- 重启扫描、稳定 request ID、单一恢复 claim、daemon 数据目录锁和 workspace 冲突检查。
- 已提交结果复用；未知副作用进入 needs_review，不自动重放。
- 一次性审批跨 daemon epoch 失效，child 关系与已提交结果可查询。
- TUI session 选择、状态、Continue、Pause 和 Abandon 操作。
- 手动/自动压缩共用持久摘要事务，恢复使用摘要加未覆盖原始尾部。
- 免 API 故障矩阵、CI workflow、固定 workload benchmark 和恢复演示。

## 兼容性

- Python 声明为 `>=3.12,<3.14`；本次发布准备只实测 Windows/Python 3.12.14。
- schema 自动从 v1/v2 迁移到 v3；旧程序不能安全打开 v3。
- 协议新增 session 恢复/核查方法和摘要字段；生成的 `WIRE_PROTOCOL.md` 已同步。
- 旧 JSONL 会话需显式无损导入，不会自动改写源文件。

## 行为与限制

- Shell/写/MCP 默认不因通用错误自动重试；外部 exactly-once 不受保证。
- daemon 重启后不会无人值守继续，需显式 resume；needs_review 不算成功。
- 自动压缩默认仍为 0.0（关闭）。原始消息不会因压缩删除。
- 固定 workload 中 S8 持久化慢于原版；没有性能提升声明。完整数据见 `VALIDATION.md`。
- 真实模型、摘要质量、远程 MCP、断电、Linux/WSL2、Python 3.13 未在 S8.5 验证。

上游项目：[youngyangyang04/KamaClaude](https://github.com/youngyangyang04/KamaClaude)。
MIT `LICENSE` 和原版权声明保持不变。
