# 【windows适配】更好的KamaClaude

本项目是在kamaclaude基础上进行二次开发，修复了原有项目在学习过程中，因调用windows不支持指令而无法运行的错误
帮助大家更好地学习开发
同时提供了S8和S9的功能进阶实现

本地 AI Agent 系统。`kama-core` 作为常驻守护进程处理所有任务，`kama`（CLI）和 `kama-tui`（TUI）通过 TCP loopback 与之通信。

## 环境要求

| 依赖 | 版本 |
|------|------|
| 操作系统 | windows x64/macOS / Linux |
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
