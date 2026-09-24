# S8 已知限制

- daemon 重启只扫描和分类任务；不会无人值守自动续跑。确定场景仍需用户或客户端显式 resume。
- 任意 Shell、MCP 或远程系统与 SQLite 不能组成原子事务，不承诺外部副作用 exactly-once。
- `needs_review` 是安全暂停，不是恢复成功；未知副作用不会自动重放，也不能由界面伪造成功。
- 后台 child 保留关系、状态和已提交结果，但未完成的整棵任务树不会自动接管或恢复。
- workspace 身份与相关文件摘要只能覆盖已知路径，无法推断任意 Shell/MCP 的完整读写集合，也不能锁住外部编辑器。
- 原始消息永久保留；摘要采用累计前缀。当前没有数据库清理、归档或长期容量策略。
- token 统计是字符数除以四的项目估算，不是供应商 tokenizer；固定假摘要不证明真实摘要质量或费用下降。
- benchmark 是 Windows 单机微基准。原版 JSONL 与 S8 WAL/FULL SQLite 的功能不等价，不能外推真实 agent 吞吐。
- 断电、磁盘损坏、网络文件系统、同步盘、远程 MCP、Windows Job 嵌套/逃逸进程尚未验证。
- S8.5 只验证 Windows 11/Python 3.12.14。WSL2 发行版存在但虚拟化服务不可用；独立
  Python 3.13.0 没有 pytest，未另建依赖环境，因此两者都未验证。
- CI 没有跟踪的冻结依赖锁可用，安装按 `pyproject.toml` 下限解析；workflow 尚未在 GitHub
  runner 实际触发，本地只验证了等价命令。
- 全仓 Ruff 仍有 28 项 S8 之前的诊断；S8.5 新增脚本定向 Ruff 通过，但没有借本阶段清理无关历史问题。
