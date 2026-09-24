# KamaClaude

本地 AI Agent 系统。`kama-core` 作为常驻守护进程处理所有任务，`kama`（CLI）和 `kama-tui`（TUI）通过 TCP loopback 与之通信。
本项目基于上游 [youngyangyang04/KamaClaude](https://github.com/youngyangyang04/KamaClaude)，
保留 MIT 许可证和原版权声明。

## 环境要求

| 依赖 | 版本 |
|------|------|
| 操作系统 | macOS / Linux |
| Python | 3.12.x |
| [uv](https://docs.astral.sh/uv/) | ≥ 0.4 |

安装 uv（若尚未安装）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Python 3.12 由 uv 自动管理，无需手动安装。

## 快速开始

```bash
git clone <repo> && cd KamaClaude
uv sync
cp .env.example .env        # 按需修改

uv run kama-core &          # 启动守护进程（后台）
uv run kama ping            # 验证连通：应返回 pong
uv run kama --version       # 应输出 0.0.1
```

## 文档

- **[RUNBOOK.md](./RUNBOOK.md)** — 完整操作参考：配置、开发命令、故障排查
- **[WIRE_PROTOCOL.md](./WIRE_PROTOCOL.md)** — IPC 协议定义（由代码生成，勿手动编辑）

## S8 可恢复执行贡献

当前 S8 工作树增加 SQLite 执行记录、显式跨重启恢复、未知副作用安全暂停、TUI 核查操作和
持久摘要。已提交工具结果可在显式 resume 后复用；结果不明的 Shell/写/MCP 不会自动重放。
这不等于任意外部操作 exactly-once，也不包含无人值守自动续跑或整棵后台任务树恢复。

- [验证、故障矩阵与 benchmark](./docs/s8/VALIDATION.md)
- [恢复演示](./docs/s8/DEMO.md)
- [迁移说明](./docs/s8/MIGRATION.md)
- [已知限制](./docs/s8/KNOWN_LIMITATIONS.md)
- [待发布变更说明](./docs/s8/RELEASE_NOTES.md)

核心验证不需要 API Key；CI 显式清空 provider 凭据并排除真实付费 E2E。固定 workload 的实测
结果显示 S8 持久化开销高于原版，因此本项目不声称 S8 带来性能提升。
