from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.bus.envelope import HandlerError
from kama_claude.core.config import KamaConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.execution import StorageError
from kama_claude.core.session.manager import SESSION_STORAGE_ERROR, SessionManager
from kama_claude.core.session.store import SessionStore


class FixedProvider:
    # 记录输入并返回有限假响应，额外模型请求将导致测试失败
    def __init__(self, responses: list[LlmResponse]) -> None:
        self.responses = iter(responses)
        self.seen: list[list[dict[str, Any]]] = []

    # 深拷贝模型输入，避免工作上下文之后修改污染观察结果
    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LlmResponse:
        self.seen.append(copy.deepcopy(messages))
        return next(self.responses)


# 创建真实管理器和 Runner，整个路径仅使用传入的假模型
def manager(store: SessionStore, provider: FixedProvider) -> SessionManager:
    return SessionManager(
        store, lambda: AgentRunner(KamaConfig(), provider=provider), EventBus(),
    )


# 功能：验证连续两轮的 SQLite 原始消息稳定有序，且不再双写旧 JSONL
# 设计：走真实 manager/runner，再关闭重开存储，检查状态、序号和磁盘派生文件
async def test_two_turns_use_only_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    store = SessionStore(tmp_path / "sessions")
    provider = FixedProvider([LlmResponse("end_turn", text="one"), LlmResponse("end_turn", text="two")])
    mgr = manager(store, provider)
    session = await mgr.create("chat")
    first = await mgr.send_message(session.id, "first")
    second = await mgr.send_message(session.id, "second")
    assert not (store.session_dir(session.id) / "thread.jsonl").exists()
    assert not (store.session_dir(session.id) / "meta.json").exists()
    store.close()
    reopened = SessionStore(tmp_path / "sessions")
    rows = reopened.execution.messages(session.id)
    assert [r["seq"] for r in rows] == [1, 2, 3, 4]
    assert len({r["id"] for r in rows}) == 4
    assert [r["role"] for r in rows] == ["user", "assistant", "user", "assistant"]
    assert reopened.execution.get_run(first)["status"] == "succeeded"
    assert reopened.execution.get_run(second)["status"] == "succeeded"
    assert reopened.read_meta(session.id).run_ids == [first, second]
    assert len(provider.seen[1]) == 3
    reopened.close()


# 功能：验证会话创建后 cwd 改变不会让真实文件工具写到另一个目录
# 设计：两个目录提供可观察对照，核对文件位置与数据库绑定路径一致
async def test_session_binds_tool_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original, other = tmp_path / "original", tmp_path / "other"
    original.mkdir()
    other.mkdir()
    monkeypatch.chdir(original)
    store = SessionStore(tmp_path / "sessions")
    provider = FixedProvider([
        LlmResponse("tool_use", tool_calls=[ToolCallBlock(
            "model-id", "write_file", {"path": "marker.txt", "content": "effect"},
        )]),
        LlmResponse("end_turn", text="done"),
    ])
    mgr = manager(store, provider)
    session = await mgr.create("chat")
    monkeypatch.chdir(other)
    run_id = await mgr.send_message(session.id, "write marker")
    assert (original / "marker.txt").read_text() == "effect"
    assert not (other / "marker.txt").exists()
    assert Path(store.execution.get_run(run_id)["workspace"]) == original.resolve()
    store.close()


# 功能：验证已执行过的 run_id 不能通过直接 Runner 调用再次发模型请求
# 设计：复用同一 run 标识并检查假模型调用数，防止误用底层接口绕过持久记录
async def test_runner_rejects_completed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    store = SessionStore(tmp_path / "sessions")
    provider = FixedProvider([LlmResponse("end_turn", text="done")])
    mgr = manager(store, provider)
    session = await mgr.create("chat")
    run_id = await mgr.send_message(session.id, "once")
    with pytest.raises(StorageError, match="already started"):
        await AgentRunner(KamaConfig(), provider=provider).run_and_capture(
            "again", run_id=run_id, session=session, store=store,
        )
    assert len(provider.seen) == 1
    store.close()


# 功能：验证不完整工具批次阻止新消息且不会裁掉已确认结果
# 设计：保存合法部分结果与孤儿调用，再观察 manager 拒绝前后的原始消息数
async def test_unbalanced_batch_blocks_new_input(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    provider = FixedProvider([])
    mgr = manager(store, provider)
    session = await mgr.create("chat")
    store.append_message(session.id, "assistant", [
        {"type": "tool_use", "id": "one", "name": "write_file", "input": {}},
        {"type": "tool_use", "id": "two", "name": "write_file", "input": {}},
    ])
    store.append_message(session.id, "user", [
        {"type": "tool_result", "tool_use_id": "one", "content": "saved"},
    ])
    before = store.execution.messages(session.id)
    with pytest.raises(HandlerError) as error:
        await mgr.send_message(session.id, "continue")
    assert error.value.code == SESSION_STORAGE_ERROR
    assert store.execution.messages(session.id) == before
    assert not provider.seen
    store.close()


# 功能：验证手动摘要只影响当前工作投影，完整原始内容重开后仍可读取
# 设计：同时检查原始字节内容、投影视图与重开状态，明确 S8.4 前无持久摘要声明
def test_compaction_preserves_raw_history(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.append_message("sess", "user", "original")
    store.append_message("sess", "assistant", "reply")
    raw = store.execution.messages("sess")
    view = [{"role": "user", "content": "summary"}, {"role": "assistant", "content": "ack"}]
    store.write_compacted("sess", view)
    store.append_message("sess", "user", "after")
    assert store.read_messages("sess") == view + [{"role": "user", "content": "after"}]
    assert store.execution.messages("sess")[:2] == raw
    store.close()
    reopened = SessionStore(tmp_path / "sessions")
    assert reopened.read_messages("sess")[0]["content"] == "original"
    assert reopened.read_messages("sess")[-1]["content"] == "after"
    reopened.close()


# 功能：验证文档提供的离线导入命令实际可用且重复执行不改变原文件
# 设计：使用独立 Python 进程执行真实 CLI 两次，再通过 SessionStore 查询同一数据库
def test_import_cli_and_session_adapter(tmp_path: Path) -> None:
    old = tmp_path / "old"
    old.mkdir()
    meta = b'{"id":"legacy","mode":"chat","status":"active","created_at":"t","updated_at":"t"}'
    raw = b'{"role":"user","content":"same"}\r\n' * 2
    (old / "meta.json").write_bytes(meta)
    (old / "thread.jsonl").write_bytes(raw)
    script = Path(__file__).resolve().parents[2] / "scripts" / "s8" / "import_legacy.py"
    root = tmp_path / "sessions"
    command = [sys.executable, str(script), str(old), "--database", str(root / "execution.sqlite3")]
    first = subprocess.run(command, check=True, capture_output=True, text=True, timeout=15)
    second = subprocess.run(command, check=True, capture_output=True, text=True, timeout=15)
    assert json.loads(first.stdout)["import_id"] == json.loads(second.stdout)["import_id"]
    store = SessionStore(root)
    assert store.import_legacy(old)["status"] == "already_imported"
    assert len(store.execution.messages("legacy")) == 2
    assert store.read_meta("legacy").id == "legacy"
    with pytest.raises(StorageError, match="unbound"):
        store.workspace("legacy")
    assert (old / "meta.json").read_bytes() == meta
    assert (old / "thread.jsonl").read_bytes() == raw
    store.close()
