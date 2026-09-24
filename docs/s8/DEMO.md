# S8 恢复演示

演示使用真实 CoreApp 子进程、TCP SocketClient 和 Textual Pilot，但 provider 固定、profile 与
workspace 隔离，不读取 `.env` 或真实会话。

```powershell
.venv/Scripts/python.exe scripts/s8/probe_s83.py `
  --output docs/s8/evidence/s85/demo-local/probe.json
```

成功路径在工具结果已提交后强制结束 daemon。重启后 TUI 通过 `Ctrl+O` 选择磁盘 session，点击
Continue；同一调用不会再次产生副作用，run 到达 succeeded。未知路径在副作用已经发生、结果
提交前结束 daemon；重启后 TUI 展示完整调用事实，Pause 保存人工说明并保持 needs_review，
counter 不增加。

探针保存 JSON 操作记录、daemon 日志和 SVG 截图。它只结束自己创建的子进程，不连接默认端口，
不使用真实用户 profile。成功路径仍是“显式 Continue 后完成”，不是无人值守自动续跑。
