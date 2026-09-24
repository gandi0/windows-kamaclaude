from __future__ import annotations

import asyncio
import copy
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.compact.compactor import SUMMARY_CONFIG_VERSION, Compactor
from kama_claude.core.config import CompactionConfig, KamaConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.execution import SCHEMA_VERSION, StorageError
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore


class SequenceProvider:
    # 依次返回固定响应并保存每次模型输入
    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = iter(responses)
        self.seen: list[list[dict[str, Any]]] = []

    # 深拷贝输入以便压缩后仍能核对调用时结构
    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LlmResponse:
        self.seen.append(copy.deepcopy(messages))
        return next(self._responses)


class BlockingSummaryProvider:
    # 在摘要生成期间暴露同步屏障以确定性追加并发消息
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    # 等待测试释放屏障后返回固定摘要
    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LlmResponse:
        self.started.set()
        await self.release.wait()
        return LlmResponse("end_turn", text="stable summary", usage=UsageStats(20, 4))


# 创建带一个已完成主 run 的持久会话
def _completed_store(tmp_path: Path) -> tuple[SessionStore, Session, str]:
    store = SessionStore(tmp_path / "sessions")
    session = Session("s1", "chat", "waiting_for_input", "", "t", "t", ["r1"])
    store.write_meta(session, workspace=str(tmp_path))
    store.execution.begin_run(
        session.id, "r1", workspace=str(tmp_path), user_content="original request"
    )
    store.execution.commit_response(
        "r1", 1, [{"type": "text", "text": "original reply"}], [], final=True
    )
    return store, session, "r1"


# 使用固定 provider 对当前 run 执行一次持久压缩
async def _compact(
    store: SessionStore, tmp_path: Path, provider: Any, run_id: str = "r1"
) -> Any:
    compactor = Compactor(EventBus(), store.session_dir("s1"), "s1", store=store)
    return await compactor.compact_persisted(run_id, provider)


# 功能：验证 v2 数据库无损迁移到 v3 并新增摘要权威表
# 设计：从当前空库移除 summaries 后回标 v2，重开核对版本、旧 session 和新表
def test_v2_migration_adds_summary_table(tmp_path: Path) -> None:
    store, _, _ = _completed_store(tmp_path)
    db = store.execution.path
    store.close()
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE summaries")
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    conn.close()

    reopened = SessionStore(tmp_path / "sessions")
    assert reopened.execution._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert reopened.execution.get_session("s1") is not None
    assert reopened.execution.list_summaries("s1") == []
    reopened.close()


# 功能：验证持久摘要重开后仍以摘要加未覆盖尾部重建上下文
# 设计：压缩已完成历史后追加新消息并重开，分别核对摘要元数据、投影和原始事实
async def test_persisted_summary_reopens_with_uncovered_tail(tmp_path: Path) -> None:
    store, _, run_id = _completed_store(tmp_path)
    provider = SequenceProvider([
        LlmResponse("end_turn", text="durable summary", usage=UsageStats(40, 6))
    ])
    result = await _compact(store, tmp_path, provider, run_id)
    assert result.summary_id is not None
    store.append_message("s1", "user", "new fact")
    raw = store.read_raw_messages("s1")
    store.close()

    reopened = SessionStore(tmp_path / "sessions")
    summary = reopened.execution.current_summary("s1")
    assert summary is not None
    assert summary["summary_id"] == result.summary_id
    assert summary["from_seq"] == 1
    assert summary["to_seq"] == 2
    assert summary["source_run_id"] == run_id
    assert summary["generation_config_version"] == SUMMARY_CONFIG_VERSION
    assert reopened.read_messages("s1") == [
        {"role": "user", "content": "durable summary"},
        {"role": "assistant", "content": "Understood, I'll continue from this summary."},
        {"role": "user", "content": "new fact"},
    ]
    assert reopened.read_raw_messages("s1") == raw
    reopened.close()


# 功能：验证摘要插入后 checkpoint 切换前故障会回滚整个事务
# 设计：使用专用事务内钩子抛错，重开确认摘要和有效指针均保持旧状态
async def test_summary_insert_fault_rolls_back_pointer(tmp_path: Path) -> None:
    store, _, run_id = _completed_store(tmp_path)
    before = store.execution.get_checkpoint(run_id)

    # 在摘要行写入后、checkpoint 更新前注入失败
    def fail_after_insert() -> None:
        raise RuntimeError("summary boundary fault")

    store.execution._after_summary_insert = fail_after_insert
    provider = SequenceProvider([LlmResponse("end_turn", text="not committed")])
    with pytest.raises(StorageError, match="summary boundary fault"):
        await _compact(store, tmp_path, provider, run_id)
    store.close()

    reopened = SessionStore(tmp_path / "sessions")
    assert reopened.execution.list_summaries("s1") == []
    assert reopened.execution.get_checkpoint(run_id) == before
    assert reopened.read_messages("s1")[0]["content"] == "original request"
    reopened.close()


# 功能：验证 checkpoint 更新后的提交故障也只暴露完整旧状态
# 设计：使用通用提交前钩子在指针已更新后抛错，确认整笔摘要事务回滚
async def test_checkpoint_switch_fault_rolls_back_summary(tmp_path: Path) -> None:
    store, _, run_id = _completed_store(tmp_path)
    before = store.execution.get_checkpoint(run_id)

    # 在事务最终提交前注入失败
    def fail_before_commit() -> None:
        raise RuntimeError("checkpoint commit fault")

    store.execution._before_commit = fail_before_commit
    provider = SequenceProvider([LlmResponse("end_turn", text="rolled back")])
    with pytest.raises(StorageError, match="checkpoint commit fault"):
        await _compact(store, tmp_path, provider, run_id)
    store.execution._before_commit = lambda: None
    assert store.execution.list_summaries("s1") == []
    assert store.execution.get_checkpoint(run_id) == before
    store.close()


# 功能：验证摘要生成期间新增消息不会被错误纳入已捕获覆盖范围
# 设计：用事件屏障停在 provider 内，追加一组完整消息后再提交并检查 seq 尾部
async def test_concurrent_messages_remain_after_snapshot(tmp_path: Path) -> None:
    store, _, run_id = _completed_store(tmp_path)
    provider = BlockingSummaryProvider()
    task = asyncio.create_task(_compact(store, tmp_path, provider, run_id))
    await provider.started.wait()
    store.append_message("s1", "user", "concurrent user")
    store.append_message("s1", "assistant", "concurrent reply")
    provider.release.set()
    result = await task

    assert result.to_seq == 2
    checkpoint = store.execution.get_checkpoint(run_id)
    assert checkpoint is not None and checkpoint["message_seq"] == 4
    assert [message["content"] for message in store.read_messages("s1")] == [
        "stable summary",
        "Understood, I'll continue from this summary.",
        "concurrent user",
        "concurrent reply",
    ]
    assert len(store.read_raw_messages("s1")) == 4
    store.close()


# 功能：验证自动压缩后工具结果、最终回复和摘要引用均完整提交
# 设计：真实 Runner 依次走工具、摘要、最终回复，检查模型输入及 SQLite 原始顺序
async def test_auto_compaction_continues_and_preserves_f04(tmp_path: Path) -> None:
    provider = SequenceProvider([
        LlmResponse(
            "tool_use",
            tool_calls=[ToolCallBlock("tool-1", "list_dir", {"path": ".", "max_depth": 1})],
            usage=UsageStats(100, 10, context_pct=0.9),
        ),
        LlmResponse("end_turn", text="auto summary", usage=UsageStats(80, 8)),
        LlmResponse("end_turn", text="final answer", usage=UsageStats(30, 5)),
    ])
    config = KamaConfig(compaction=CompactionConfig(auto_threshold=0.5))
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(
        store,
        lambda: AgentRunner(config, provider=provider),
        EventBus(),
        provider=provider,
    )
    session = await manager.create("chat", workspace=str(tmp_path))
    run_id = await manager.send_message(session.id, "inspect files")

    rows = store.execution.messages(session.id)
    assert [row["role"] for row in rows] == ["user", "assistant", "user", "assistant"]
    assert rows[1]["content"][0]["type"] == "tool_use"
    assert rows[2]["content"][0]["type"] == "tool_result"
    assert rows[3]["content"][0]["text"] == "final answer"
    checkpoint = store.execution.get_checkpoint(run_id)
    assert checkpoint is not None and checkpoint["summary_ref"] is not None
    assert checkpoint["summary_to"] == 3
    assert provider.seen[2][0]["content"] == "auto summary"
    assert provider.seen[2][-1]["content"] == "Understood, I'll continue from this summary."
    assert store.execution.get_run(run_id)["status"] == "succeeded"
    store.close()


# 功能：验证手动压缩与下一 run 使用同一持久恢复视图
# 设计：经 SessionManager 手动压缩后重开 store，再发消息观察新 run 的首次模型输入
async def test_manual_compaction_is_inherited_by_next_run(tmp_path: Path) -> None:
    first_provider = SequenceProvider([
        LlmResponse("end_turn", text="first answer"),
        LlmResponse("end_turn", text="manual summary", usage=UsageStats(40, 5)),
    ])
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(
        store,
        lambda: AgentRunner(KamaConfig(), provider=first_provider),
        EventBus(),
        provider=first_provider,
    )
    session = await manager.create("chat", workspace=str(tmp_path))
    await manager.send_message(session.id, "first request")
    await manager.compact(session.id)
    first_summary = store.execution.current_summary(session.id)
    assert first_summary is not None
    store.close()

    second_provider = SequenceProvider([LlmResponse("end_turn", text="second answer")])
    reopened = SessionStore(tmp_path / "sessions")
    resumed = SessionManager(
        reopened,
        lambda: AgentRunner(KamaConfig(), provider=second_provider),
        EventBus(),
        provider=second_provider,
    )
    await resumed.send_message(session.id, "second request")
    assert [message["content"] for message in second_provider.seen[0]] == [
        "manual summary",
        "Understood, I'll continue from this summary.",
        "second request",
    ]
    current = reopened.execution.current_summary(session.id)
    assert current is not None and current["summary_id"] == first_summary["summary_id"]
    reopened.close()


# 功能：验证摘要 provider 失败不会改变旧 checkpoint 或工作上下文
# 设计：抛出生成异常后比较完整 checkpoint、摘要列表和模型投影
async def test_generation_failure_preserves_effective_state(tmp_path: Path) -> None:
    store, _, run_id = _completed_store(tmp_path)
    before_checkpoint = store.execution.get_checkpoint(run_id)
    before_messages = store.read_messages("s1")

    class FailingProvider:
        # 固定在摘要生成阶段失败
        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LlmResponse:
            raise RuntimeError("provider failed")

    assert await _compact(store, tmp_path, FailingProvider(), run_id) is None
    assert store.execution.get_checkpoint(run_id) == before_checkpoint
    assert store.execution.list_summaries("s1") == []
    assert store.read_messages("s1") == before_messages
    store.close()
