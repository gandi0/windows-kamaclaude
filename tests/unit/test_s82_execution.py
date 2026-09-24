from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.config import KamaConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from kama_claude.core.loop import AgentLoop
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.execution import ExecutionStore, StorageError
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.errors import ToolCancelledError
from kama_claude.core.tools.registry import ToolRegistry


class _SequenceProvider:
    # 初始化按顺序返回的模型响应并记录请求上下文
    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = iter(responses)
        self.calls = 0
        self.messages: list[list[dict[str, object]]] = []

    # 返回下一条固定响应并保存本次模型请求的消息快照
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        self.calls += 1
        self.messages.append([dict(message) for message in messages])
        try:
            return next(self._responses)
        except StopIteration as exc:
            raise AssertionError("provider was called after the planned response sequence") from exc


class _MarkerTool(BaseTool):
    name = "marker"
    description = "append a marker to a local file"
    input_schema = {"type": "object"}
    effect = "write"
    retry_safe = False

    # 初始化可观察文件、输出和可选取消屏障
    def __init__(
        self,
        marker: Path,
        value: str,
        *,
        entered: asyncio.Event | None = None,
        block: bool = False,
    ) -> None:
        self.name = f"marker_{value.strip().replace(' ', '_')}"
        self.marker = marker
        self.value = value
        self.entered = entered
        self.block = block
        self.calls = 0

    # 记录一次真实副作用并按测试需要等待取消
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.block:
            await asyncio.Future()
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        with self.marker.open("a", encoding="utf-8") as stream:
            stream.write(self.value)
        return ToolResult(self.value)


class _TransientReadTool(BaseTool):
    name = "transient_read"
    description = "return a large read-only payload after transient failures"
    input_schema = {"type": "object"}
    effect = "read_only"
    retry_safe = True

    # 初始化暂时失败次数和最终原始输出
    def __init__(self, failures: int, output: str) -> None:
        self.failures = failures
        self.output = output
        self.calls = 0

    # 注入 EAGAIN 后返回完整大输出，模拟可安全重试的读取
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self.calls += 1
        if self.calls <= self.failures:
            raise BlockingIOError(errno.EAGAIN, "temporarily unavailable")
        return ToolResult(self.output)


class _CancellableTool(BaseTool):
    name = "cancellable"
    description = "wait until the caller cancels execution"
    input_schema = {"type": "object"}
    retry_safe = False

    # 初始化取消屏障、清理 marker 和工具效果分类
    def __init__(self, marker: Path, effect: str) -> None:
        self.marker = marker
        self.effect = effect
        self.entered = asyncio.Event()
        self.calls = 0

    # 在取消时写入清理证据并返回结构化的已知或未知结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self.calls += 1
        self.entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError as exc:
            self.marker.write_text("cleanup-confirmed", encoding="utf-8")
            outcome = "known" if self.effect == "read_only" else "unknown"
            raise ToolCancelledError(ToolResult(
                "cancelled after cleanup",
                True,
                "cancelled",
                outcome=outcome,
                cleanup_confirmed=True,
            )) from exc
        raise AssertionError("cancellable tool must be cancelled")


class _ReplacingCompactor:
    # 初始化压缩观察结果
    def __init__(self) -> None:
        self.before: list[dict[str, object]] = []

    # 保存完整运行时上下文后替换为摘要和确认消息
    async def compact(self, context: ExecutionContext, provider: object) -> None:
        self.before = [dict(message) for message in context.messages]
        context.messages = [
            {"role": "user", "content": "compacted summary"},
            {"role": "assistant", "content": [{"type": "text", "text": "summary ack"}]},
        ]


# 将测试工具注册到独立注册表，避免跨用例共享状态
def _registry(*tools: BaseTool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


# 创建真实 SQLite 执行库并写入会话和初始用户请求
def _store(tmp_path: Path, *, session_id: str = "session-s82", run_id: str = "run-s82") -> ExecutionStore:
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    store.put_session(
        {
            "id": session_id,
            "mode": "chat",
            "status": "active",
            "title": "S8.2 verification",
            "created_at": "2026-09-23T00:00:00+00:00",
            "updated_at": "2026-09-23T00:00:00+00:00",
        },
        workspace=str(tmp_path),
    )
    store.begin_run(
        session_id,
        run_id,
        workspace=str(tmp_path),
        user_content="execute the verification task",
    )
    return store


# 关闭并重新打开数据库，确保断言来自持久状态而非内存缓存
def _close_and_reopen(store: ExecutionStore, db_path: Path) -> ExecutionStore:
    store.close()
    return ExecutionStore(db_path)


# 捕获模型提交的调用意图，同时保留真实数据库写入
def _capture_plans(store: ExecutionStore, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    captured: list[dict[str, object]] = []
    original = store.commit_response

    # 记录每次模型响应中的调用意图后执行原始提交
    def commit(
        run_id: str,
        step: int,
        blocks: list[dict[str, object]],
        calls: list[dict[str, object]],
        *,
        final: bool = False,
    ) -> object:
        captured.extend(dict(call) for call in calls)
        return original(run_id, step, blocks, calls, final=final)

    monkeypatch.setattr(store, "commit_response", commit)
    return captured


# 读取执行库原始消息行并限定当前 run
def _raw_messages(store: ExecutionStore, session_id: str, run_id: str) -> list[dict[str, object]]:
    rows = store.messages(session_id)
    return [dict(row) for row in rows if row.get("run_id") == run_id]


# 直接读取公开 API 返回的原始结构内容
def _content(row: dict[str, object]) -> object:
    return row["content"]



# 在原始消息中查找指定调用的 tool_result，排除仅存在内存 context 的假结果
def _has_tool_result(rows: list[dict[str, object]], tool_use_id: str) -> bool:
    for row in rows:
        content = _content(row)
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                if block.get("tool_use_id") == tool_use_id:
                    return True
    return False


# 核对公开检查点字段给出的已提交消息边界
def _checkpoint_message_seq(checkpoint: dict[str, object]) -> int:
    value = checkpoint["message_seq"]
    assert isinstance(value, int)
    return value


# 功能：验证模型响应意图提交失败时工具完全不会启动且事务没有残留调用
# 设计：在真实 commit_response 边界抛 StorageError，观察真实 marker 与重开数据库，覆盖执行前安全边界
async def test_intent_commit_failure_prevents_tool_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    marker = tmp_path / "intent-marker.txt"
    tool = _MarkerTool(marker, "must-not-run")
    provider = _SequenceProvider([
        LlmResponse(
            stop_reason="tool_use",
            tool_calls=[ToolCallBlock("model-tool-id", tool.name, {})],
        )
    ])
    planned: list[dict[str, object]] = []

    # 模拟模型响应事务故障并保留已生成的调用 UUID
    def fail_commit(
        run_id: str,
        step: int,
        blocks: list[dict[str, object]],
        calls: list[dict[str, object]],
        *,
        final: bool = False,
    ) -> None:
        planned.extend(dict(call) for call in calls)
        raise StorageError("intent commit failed")

    monkeypatch.setattr(store, "commit_response", fail_commit)
    loop = AgentLoop(provider, _registry(tool), EventBus(), execution_store=store)
    context = ExecutionContext("run-s82", "execute", 3)

    with pytest.raises(StorageError, match="intent commit failed"):
        await loop.run(context)

    assert provider.calls == 1
    assert tool.calls == 0
    assert not marker.exists()
    reopened = _close_and_reopen(store, tmp_path / "execution.sqlite3")
    try:
        assert planned
        assert all(reopened.get_call(str(call["call_id"])) is None for call in planned)
    finally:
        reopened.close()


# 功能：验证派发 attempt 提交失败时真实写工具没有产生 marker 且模型不再继续
# 设计：意图先真实提交，再在 start_attempt 边界故障，重开数据库确认调用仍未跨过执行边界
async def test_dispatch_commit_failure_prevents_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    marker = tmp_path / "dispatch-marker.txt"
    tool = _MarkerTool(marker, "must-not-run")
    provider = _SequenceProvider([
        LlmResponse(
            stop_reason="tool_use",
            tool_calls=[ToolCallBlock("model-dispatch-id", tool.name, {})],
        )
    ])
    planned = _capture_plans(store, monkeypatch)

    # 模拟派发边界事务故障，禁止工具本体启动
    def fail_attempt(call_id: str, attempt_id: str, number: int) -> None:
        raise StorageError("dispatch commit failed")

    monkeypatch.setattr(store, "start_attempt", fail_attempt)
    loop = AgentLoop(provider, _registry(tool), EventBus(), execution_store=store)
    with pytest.raises(StorageError, match="dispatch commit failed"):
        await loop.run(ExecutionContext("run-s82", "execute", 3))

    assert provider.calls == 1
    assert tool.calls == 0
    assert not marker.exists()
    assert len(planned) == 1
    reopened = _close_and_reopen(store, tmp_path / "execution.sqlite3")
    try:
        call = reopened.get_call(str(planned[0]["call_id"]))
        assert call is not None
        assert call["status"] in {"planned", "ready"}
        assert call["result"] is None
        assert reopened.get_attempts(str(planned[0]["call_id"])) == []
    finally:
        reopened.close()


# 功能：验证工具已写入后结果提交失败只执行一次并保留 dispatching 未决状态
# 设计：同批安排两个真实 marker 并阻断 commit_result，检查第二工具、下一模型、结果消息和检查点均未推进
async def test_result_commit_failure_stops_after_one_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    first_marker = tmp_path / "first-marker.txt"
    second_marker = tmp_path / "second-marker.txt"
    first = _MarkerTool(first_marker, "first")
    second = _MarkerTool(second_marker, "second")
    provider = _SequenceProvider([
        LlmResponse(
            stop_reason="tool_use",
            tool_calls=[
                ToolCallBlock("model-first-id", first.name, {}),
                ToolCallBlock("model-second-id", second.name, {}),
            ],
        ),
        LlmResponse(stop_reason="end_turn", text="must-not-be-requested"),
    ])
    planned = _capture_plans(store, monkeypatch)

    # 模拟结果事务故障，阻断同批后续动作
    def fail_result(
        call_id: str,
        result: ToolResult,
        *,
        retry: bool = False,
    ) -> None:
        raise StorageError("result commit failed")

    monkeypatch.setattr(store, "commit_result", fail_result)
    loop = AgentLoop(provider, _registry(first, second), EventBus(), execution_store=store)
    with pytest.raises(StorageError, match="result commit failed"):
        await loop.run(ExecutionContext("run-s82", "execute", 3))

    assert provider.calls == 1
    assert first.calls == 1
    assert second.calls == 0
    assert first_marker.read_text(encoding="utf-8") == "first"
    assert not second_marker.exists()
    assert [str(call["tool_use_id"]) for call in planned] == [
        "model-first-id", "model-second-id"
    ]

    reopened = _close_and_reopen(store, tmp_path / "execution.sqlite3")
    try:
        first_call = reopened.get_call(str(planned[0]["call_id"]))
        assert first_call is not None
        assert first_call["status"] == "dispatching"
        assert first_call["result"] is None
        attempts = reopened.get_attempts(str(planned[0]["call_id"]))
        assert len(attempts) == 1
        assert attempts[0]["phase"] == "dispatching"
        rows = _raw_messages(reopened, "session-s82", "run-s82")
        assert rows
        assert not _has_tool_result(rows, "model-first-id")
        assert not _has_tool_result(rows, "model-second-id")
        checkpoint = reopened.get_checkpoint("run-s82")
        assert checkpoint is not None
        assert _checkpoint_message_seq(checkpoint) == max(int(row["seq"]) for row in rows)
    finally:
        reopened.close()


# 功能：验证成功调用按 call_id 保存完整大输出、UUID 独立于模型 ID 且安全重试每次派发
# 设计：真实只读工具先抛两次 EAGAIN 再返回原文，重开数据库同时核对 call、attempt、消息和下一模型请求
async def test_success_persists_full_result_and_retry_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    output = "large-output:" + ("x" * 12_000)
    tool = _TransientReadTool(2, output)
    provider = _SequenceProvider([
        LlmResponse(
            stop_reason="tool_use",
            tool_calls=[ToolCallBlock("model-read-id", tool.name, {})],
        ),
        LlmResponse(stop_reason="end_turn", text="read complete"),
    ])
    planned = _capture_plans(store, monkeypatch)
    monkeypatch.setattr("kama_claude.core.tools.invocation._RETRY_BASE_S", 0)
    loop = AgentLoop(provider, _registry(tool), EventBus(), execution_store=store)
    context = ExecutionContext("run-s82", "execute", 4)
    await loop.run(context)

    assert context.status == "success"
    assert provider.calls == 2
    assert tool.calls == 3
    assert len(planned) == 1
    call_id = str(planned[0]["call_id"])
    assert call_id != "model-read-id"

    reopened = _close_and_reopen(store, tmp_path / "execution.sqlite3")
    try:
        call = reopened.get_call(call_id)
        assert call is not None
        assert call["status"] == "succeeded"
        assert call["tool_use_id"] == "model-read-id"
        assert call["result"] is not None
        assert call["result"]["content"] == output
        attempts = reopened.get_attempts(call_id)
        assert [int(attempt["number"]) for attempt in attempts] == [1, 2, 3]
        assert len({str(attempt["attempt_id"]) for attempt in attempts}) == 3
        assert all(attempt["phase"] == "result_committed" for attempt in attempts)
        rows = _raw_messages(reopened, "session-s82", "run-s82")
        assert _has_tool_result(rows, "model-read-id")
        assert any(output in json.dumps(_content(row), ensure_ascii=False) for row in rows)
    finally:
        reopened.close()


# 功能：验证可安全重试的读取在退避期间取消不会派发第二个 attempt
# 设计：首个暂时错误触发真实事件订阅取消当前任务，观察工具次数、attempt 序号和重开数据库状态
async def test_cancel_during_retry_backoff_does_not_dispatch_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    tool = _TransientReadTool(1, "eventual output")
    provider = _SequenceProvider([
        LlmResponse(
            stop_reason="tool_use",
            tool_calls=[ToolCallBlock("model-backoff-id", tool.name, {})],
        )
    ])
    planned = _capture_plans(store, monkeypatch)
    monkeypatch.setattr("kama_claude.core.tools.invocation._RETRY_BASE_S", 0.05)
    bus = EventBus()

    # 首次失败的诊断事件到达后取消正在退避的调用任务
    async def cancel_after_first_failure(event: Any) -> None:
        if (event.type == "tool.call_failed" and event.error_class == "runtime_error"
                and event.call_id == str(planned[0]["call_id"])):
            current = asyncio.current_task()
            assert current is not None
            current.cancel()

    bus.subscribe(cancel_after_first_failure)
    loop = AgentLoop(provider, _registry(tool), bus, execution_store=store)
    task = asyncio.create_task(loop.run(ExecutionContext("run-s82", "execute", 3)))
    with pytest.raises(ToolCancelledError):
        await task

    assert provider.calls == 1
    assert tool.calls == 1
    assert len(planned) == 1
    call_id = str(planned[0]["call_id"])
    reopened = _close_and_reopen(store, tmp_path / "execution.sqlite3")
    try:
        call = reopened.get_call(call_id)
        assert call is not None
        assert call["status"] == "cancelled"
        assert call["result"] is not None
        attempts = reopened.get_attempts(call_id)
        assert [int(attempt["number"]) for attempt in attempts] == [1]
        assert len(attempts) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("effect", "expected_outcome"),
    [("read_only", "known"), ("write", "unknown")],
)
# 功能：验证取消完成清理后提交对应 known/unknown 结果且不安排后续工具或模型
# 设计：用真实 asyncio 取消屏障和磁盘清理 marker，覆盖只读可知与写操作不可知两种保守结论
async def test_cancellation_commits_outcome_without_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    effect: str,
    expected_outcome: str,
) -> None:
    store = _store(tmp_path)
    tool = _CancellableTool(tmp_path / f"{effect}-cleanup.txt", effect)
    provider = _SequenceProvider([
        LlmResponse(
            stop_reason="tool_use",
            tool_calls=[ToolCallBlock(f"model-{effect}-id", tool.name, {})],
        ),
        LlmResponse(stop_reason="end_turn", text="must-not-be-requested"),
    ])
    planned = _capture_plans(store, monkeypatch)
    loop = AgentLoop(provider, _registry(tool), EventBus(), execution_store=store)
    task = asyncio.create_task(loop.run(ExecutionContext("run-s82", "execute", 3)))
    await asyncio.wait_for(tool.entered.wait(), 2)
    task.cancel()
    with pytest.raises(ToolCancelledError):
        await task

    assert provider.calls == 1
    assert tool.calls == 1
    assert tool.marker.read_text(encoding="utf-8") == "cleanup-confirmed"
    assert len(planned) == 1
    call_id = str(planned[0]["call_id"])
    reopened = _close_and_reopen(store, tmp_path / "execution.sqlite3")
    try:
        call = reopened.get_call(call_id)
        assert call is not None
        assert call["status"] in {"cancelled", "failed", "unknown"}
        assert call["result"] is not None
        assert call["result"]["outcome"] == expected_outcome
    finally:
        reopened.close()


# 功能：验证成功结果已提交后末尾诊断事件取消不会覆盖持久成功结果
# 设计：在真实 tool.call_finished 订阅回调中取消任务，重开数据库核对 call/result/attempt 仍为成功且不续跑
async def test_finished_event_cancellation_preserves_committed_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    marker = tmp_path / "finished-marker.txt"
    tool = _MarkerTool(marker, "finished")
    provider = _SequenceProvider([
        LlmResponse(
            stop_reason="tool_use",
            tool_calls=[ToolCallBlock("model-finished-id", tool.name, {})],
        ),
        LlmResponse(stop_reason="end_turn", text="must-not-be-requested"),
    ])
    planned = _capture_plans(store, monkeypatch)
    bus = EventBus()

    # 成功诊断已经发布时取消当前任务，模拟观察者取消竞争窗口
    async def cancel_after_finished(event: Any) -> None:
        if event.type == "tool.call_finished":
            current = asyncio.current_task()
            assert current is not None
            current.cancel()

    bus.subscribe(cancel_after_finished)
    loop = AgentLoop(provider, _registry(tool), bus, execution_store=store)
    task = asyncio.create_task(loop.run(ExecutionContext("run-s82", "execute", 3)))
    with pytest.raises(ToolCancelledError):
        await task

    assert provider.calls == 1
    assert tool.calls == 1
    assert marker.read_text(encoding="utf-8") == "finished"
    assert len(planned) == 1
    call_id = str(planned[0]["call_id"])
    reopened = _close_and_reopen(store, tmp_path / "execution.sqlite3")
    try:
        call = reopened.get_call(call_id)
        assert call is not None
        assert call["status"] == "succeeded"
        assert call["result"] is not None
        assert call["result"]["content"] == "finished"
        attempts = reopened.get_attempts(call_id)
        assert len(attempts) == 1
        assert attempts[0]["phase"] == "result_committed"
    finally:
        reopened.close()


# 功能：验证同批多工具按模型顺序执行，压缩替换 context 后原始消息和完整结果仍在库中
# 设计：两个真实文件追加器配合会替换消息的压缩器，比较文件顺序、模型输入和重开数据库原始消息
async def test_multi_tool_order_and_compaction_preserve_raw_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    marker = tmp_path / "ordered-marker.txt"
    first = _MarkerTool(marker, "one\n")
    second = _MarkerTool(marker, "two\n")
    provider = _SequenceProvider([
        LlmResponse(
            stop_reason="tool_use",
            tool_calls=[
                ToolCallBlock("model-one-id", first.name, {}),
                ToolCallBlock("model-two-id", second.name, {}),
            ],
            usage=UsageStats(100, 10, context_pct=0.9),
        ),
        LlmResponse(stop_reason="end_turn", text="ordered complete"),
    ])
    compactor = _ReplacingCompactor()
    planned = _capture_plans(store, monkeypatch)
    loop = AgentLoop(
        provider,
        _registry(first, second),
        EventBus(),
        compactor=compactor,  # type: ignore[arg-type]
        compact_threshold=0.5,
        execution_store=store,
    )
    context = ExecutionContext("run-s82", "execute", 4)
    await loop.run(context)

    assert context.status == "success"
    assert marker.read_text(encoding="utf-8") == "one\ntwo\n"
    assert provider.calls == 2
    assert provider.messages[1][0]["content"] == "compacted summary"
    assert len(compactor.before) >= 3
    assert [str(call["tool_use_id"]) for call in planned] == [
        "model-one-id", "model-two-id"
    ]
    assert [first.calls, second.calls] == [1, 1]

    reopened = _close_and_reopen(store, tmp_path / "execution.sqlite3")
    try:
        rows = _raw_messages(reopened, "session-s82", "run-s82")
        assert rows
        assert _has_tool_result(rows, "model-one-id")
        assert _has_tool_result(rows, "model-two-id")
        serialized = json.dumps([_content(row) for row in rows], ensure_ascii=False)
        assert "one\\n" in serialized
        assert "two\\n" in serialized
        assert any(
            isinstance(_content(row), list)
            and any(
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("id") == "model-one-id"
                for block in _content(row)
            )
            for row in rows
        )
    finally:
        reopened.close()


# 功能：验证最终 assistant 回复提交失败时 loop 不标记 success 且数据库不留下最终消息
# 设计：只在 final=True 的真实响应事务边界注入 StorageError，重开数据库检查 run、原始消息和 checkpoint
async def test_final_response_commit_failure_does_not_report_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    provider = _SequenceProvider([LlmResponse(stop_reason="end_turn", text="must-not-commit")])
    original = store.commit_response

    # 仅阻断最终 assistant 事务，保留非最终响应的真实提交语义
    def fail_final(
        run_id: str,
        step: int,
        blocks: list[dict[str, object]],
        calls: list[dict[str, object]],
        *,
        final: bool = False,
    ) -> object:
        if final:
            raise StorageError("final response commit failed")
        return original(run_id, step, blocks, calls, final=final)

    monkeypatch.setattr(store, "commit_response", fail_final)
    loop = AgentLoop(provider, _registry(), EventBus(), execution_store=store)
    context = ExecutionContext("run-s82", "execute", 2)
    with pytest.raises(StorageError, match="final response commit failed"):
        await loop.run(context)
    assert context.status != "success"
    assert provider.calls == 1

    reopened = _close_and_reopen(store, tmp_path / "execution.sqlite3")
    try:
        run = reopened.get_run("run-s82")
        assert run is not None
        assert run["status"] == "running"
        rows = _raw_messages(reopened, "session-s82", "run-s82")
        assert len(rows) == 1
        assert rows[0]["role"] == "user"
        checkpoint = reopened.get_checkpoint("run-s82")
        assert checkpoint is not None
        assert _checkpoint_message_seq(checkpoint) == int(rows[0]["seq"])
    finally:
        reopened.close()


# 功能：验证 Runner 遇到最终回复存储异常返回 needs_review/storage_error 且不发布 success
# 设计：真实 Runner 和假模型在最终提交前故障，重开数据库排除未持久化回复被报告为成功
async def test_runner_maps_storage_error_without_success_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ExecutionStore.commit_response

    # 在真实最终回复事务入口注入失败，其余 Runner 路径全部实际执行
    def fail_final(self: ExecutionStore, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("final"):
            raise StorageError("final response commit failed")
        original(self, *args, **kwargs)

    monkeypatch.setattr(ExecutionStore, "commit_response", fail_final)
    events: list[Any] = []

    # 收集实际运行事件以排除提交失败后的虚假成功通知
    async def collect(event: Any) -> None:
        events.append(event)

    config = KamaConfig()
    runner = AgentRunner(
        config,
        provider=_SequenceProvider([LlmResponse(stop_reason="end_turn", text="unsaved")]),  # type: ignore[arg-type]
        extra_handlers=[collect],
        runs_dir=tmp_path,
    )
    outcome = await runner.run_and_capture("execute", run_id="run-s82")

    assert outcome.status == "needs_review"
    assert outcome.reason == "storage_error"
    finished = [event for event in events if event.type == "run.finished"]
    assert len(finished) == 1
    assert finished[0].status == "needs_review"
    assert finished[0].reason == "storage_error"
    assert all(event.status != "success" for event in finished)

    reopened = ExecutionStore(tmp_path / "execution.sqlite3")
    try:
        run = reopened.get_run("run-s82")
        assert run is not None and run["status"] == "running"
        assert len(reopened.messages(run["session_id"])) == 1
        assert reopened.get_checkpoint("run-s82")["message_seq"] == 1
    finally:
        reopened.close()
