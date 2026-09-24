from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from tests.helpers.s83_daemon import DaemonProcess, RpcClient, read_jsonl

_STATUS_TIMEOUT = 30.0


# 轮询会话直到当前 run 进入预期终态
async def _wait_succeeded(client: RpcClient, session_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + _STATUS_TIMEOUT
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = await client.call("session.status", {"session_id": session_id})
        if latest.get("session", {}).get("run_status") == "succeeded":
            return latest
        await asyncio.sleep(0.05)
    raise AssertionError(f"session did not succeed: {latest}")


# 功能：验证真实 daemon 手动压缩后重启仍向模型提供持久摘要和新消息
# 设计：经 TCP RPC 完成首轮与压缩，停止重启同一 profile 后由子进程 provider 记录上下文
async def test_daemon_restart_reuses_persisted_manual_summary(tmp_path: Path) -> None:
    daemon = DaemonProcess(
        profile=tmp_path / "profile",
        workspace=tmp_path / "workspace",
        scenario="s84-compaction",
    )
    await daemon.start()
    first = await daemon.connect("s84-first")
    try:
        created = await first.call(
            "session.create",
            {"mode": "chat", "title": "s84", "workspace": str(daemon.workspace)},
        )
        session_id = str(created["session_id"])
        await first.call(
            "session.send_message",
            {"session_id": session_id, "content": "first request", "request_id": "s84-1"},
        )
        await _wait_succeeded(first, session_id)
        compacted = await first.call(
            "session.compact", {"session_id": session_id, "focus": "durability"}
        )
        assert compacted["summary_tokens"] >= 0
        assert compacted["summary_id"]
        assert compacted["summary_version"] == 1
        assert compacted["summary_from"] == 1
        assert compacted["summary_to"] == 2
    finally:
        await daemon.stop()

    await daemon.start()
    second = await daemon.connect("s84-second")
    try:
        await second.call(
            "session.send_message",
            {"session_id": session_id, "content": "second request", "request_id": "s84-2"},
        )
        await _wait_succeeded(second, session_id)
    finally:
        await daemon.stop()

    contexts = [
        row for row in read_jsonl(daemon.profile / "child-operations.jsonl")
        if row.get("operation") == "s84.context"
    ]
    assert len(contexts) == 2
    assert contexts[0]["summary_seen"] is False
    assert contexts[1]["summary_seen"] is True
    assert contexts[1]["original_seen"] is False
    assert contexts[1]["message_count"] == 3
