# S8 数据迁移与兼容性

S8 使用 SQLite `PRAGMA user_version` 逐级迁移。升级前先停止旧 daemon，并用 SQLite backup API
备份主库及 WAL/SHM；不要在运行中只复制 `execution.sqlite3`。

| 来源 | 目标 | 行为 |
| --- | --- | --- |
| 无数据库 | v3 | 新建完整 schema |
| v1 | v2，再到 v3 | 保留 session/run/message/call/attempt/checkpoint，增加恢复字段和摘要表 |
| v2 | v3 | 保留全部执行事实，新增不可变 `summaries` 与有效摘要引用 |
| 旧 `meta.json/thread.jsonl` | v3 | 仅通过 `scripts/s8/import_legacy.py` 显式无损导入 |
| 高于 v3 | 拒绝 | 不降级写入未知 schema |

旧 JSONL 导入保存源 bytes 和逐行映射；同一快照重复导入不重复消息，源文件重写会隔离为冲突，
不会覆盖原文件。旧历史没有执行前意图、workspace 身份或原子结果证据时，不会补造成可自动恢复
的任务。

v3 数据库不能由旧版程序安全打开，协议新增字段保持可选或向后兼容，但旧客户端不具备稳定
`request_id` 去重、恢复、核查和摘要范围展示能力。迁移不会删除原始消息；摘要只是恢复视图。

升级后运行：

```powershell
.venv/Scripts/python.exe scripts/s8/check_baseline.py --check s84 --output docs/s8/evidence/s85/migration-check
.venv/Scripts/python.exe scripts/gen_protocol_doc.py --check
```

回退需要恢复升级前备份；不要手工降低 `user_version` 或删除摘要/检查点行。
