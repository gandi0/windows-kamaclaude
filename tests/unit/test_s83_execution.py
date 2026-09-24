from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.config import KamaConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.execution import StorageError
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.subagent.registry import BackgroundTaskRegistry
from kama_claude.core.subagent.tool import AgentResultTool, SpawnAgentTool
from kama_claude.core.tools.builtin.write_file import WriteFileTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry


class FinalProvider:
    # 保存模型输入以验证恢复后仅发送完整配对的历史
    def __init__(self) -> None:
        self.seen: list[list[dict[str, Any]]] = []

    # 只返回固定最终回复，不调用任何外部服务
    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LlmResponse:
        self.seen.append(messages)
        return LlmResponse("end_turn", text="recovered")


# 功能：恢复部分完成批次只执行未派发调用，已提交调用结果和完整历史复用
# 设计：先真实写文件并提交一个结果，再重开数据库，核对两文件及调用计数和模型配对
async def test_resume_partial_batch_reuses_committed_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    store = SessionStore(tmp_path / "sessions")
    provider = FinalProvider()
    mgr = SessionManager(store, lambda: AgentRunner(KamaConfig(), provider=provider), EventBus())
    session = await mgr.create("chat", workspace=str(tmp_path))
    store.execution.begin_run(session.id, "run", workspace=str(tmp_path), user_content="write",
                              execution_config={"goal": "write"})
    tools = [ToolCallBlock(str(i), "write_file", {"path": f"{i}.txt", "content": str(i)})
             for i in range(2)]
    store.execution.commit_response("run", 1, [
        {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input} for tc in tools
    ], [{"call_id": f"call-{tc.id}", "tool_use_id": tc.id, "name": tc.name,
         "input": tc.input, "effect": "write", "retry_safe": False} for tc in tools])
    registry = ToolRegistry()
    registry.register(WriteFileTool(tmp_path))
    writes: list[str] = []
    original = WriteFileTool.invoke

    # 观察真实文件工具执行次数而不替代其副作用
    async def observed(self: WriteFileTool, params: dict[str, object]) -> Any:
        writes.append(str(params["path"]))
        return await original(self, params)

    monkeypatch.setattr(WriteFileTool, "invoke", observed)
    await invoke_tool(registry, tools[0], EventBus(), "run", call_id="call-0",
                      execution_store=store.execution)
    before = store.execution.get_call("call-0")
    store.close()

    reopened = SessionStore(tmp_path / "sessions")
    reopened.execution.start_daemon("second")
    mgr = SessionManager(reopened, lambda: AgentRunner(KamaConfig(), provider=provider),
                         EventBus(), epoch="second")
    result = await mgr.resume(session.id, "run", str(tmp_path))
    assert result["started"]
    await asyncio.gather(*mgr._tasks.values())
    assert writes == ["0.txt", "1.txt"]
    assert [p.read_text() for p in (tmp_path / "0.txt", tmp_path / "1.txt")] == ["0", "1"]
    assert reopened.execution.get_call("call-0")["result"] == before["result"]
    assert len(reopened.execution.get_attempts("call-0")) == 1
    assert len(reopened.execution.get_attempts("call-1")) == 1
    assert reopened.execution.get_run("run")["status"] == "succeeded"
    assert len(provider.seen) == 1
    reopened._assert_balanced(provider.seen[0])
    assert len(reopened.execution.messages(session.id)) == 5
    reopened.close()


# 功能：截断模型输出留下的调用意图在恢复时仍然不会执行
# 设计：构造已提交且明确不可派发的真实写文件意图，重启后核对零副作用与可解释错误
async def test_resume_never_dispatches_incomplete_model_output(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    provider = FinalProvider()
    mgr = SessionManager(store, lambda: AgentRunner(KamaConfig(), provider=provider), EventBus())
    session = await mgr.create("chat", workspace=str(tmp_path))
    store.execution.begin_run(session.id, "run", workspace=str(tmp_path), user_content="write",
                              execution_config={"goal": "write"})
    params = {"path": "must-not-exist", "content": "partial"}
    store.execution.commit_response("run", 1, [
        {"type": "tool_use", "id": "model", "name": "write_file", "input": params},
    ], [{"call_id": "call", "tool_use_id": "model", "name": "write_file", "input": params,
         "effect": "write", "retry_safe": False, "dispatch_allowed": False}])
    store.execution.start_daemon("second")
    mgr = SessionManager(store, lambda: AgentRunner(KamaConfig(), provider=provider),
                         EventBus(), epoch="second")
    assert (await mgr.resume(session.id, "run", str(tmp_path)))["started"]
    await asyncio.gather(*mgr._tasks.values())
    assert not (tmp_path / "must-not-exist").exists()
    assert not store.execution.get_attempts("call")
    assert store.execution.get_call("call")["result"]["error_type"] == "incomplete_model_output"
    assert store.execution.get_run("run")["status"] == "succeeded"
    store.close()


# 功能：相同模型工具 ID 在新 daemon 中不能被旧审批 token 授权
# 设计：使用独立权限管理器重建生命周期，先拒绝旧 token 再确认新 token，观察 Future 未被提前解决
async def test_old_approval_token_cannot_authorize_new_epoch() -> None:
    old = PermissionManager(daemon_epoch="old")
    new = PermissionManager(daemon_epoch="new")
    emitted: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # 用事件队列确定审批已挂起，避免固定等待推测时序
    async def emit(event: dict[str, Any]) -> None:
        await emitted.put(event)

    kwargs = {"tool_use_id": "same", "tool_name": "bash", "params": {"command": "x"},
              "session_id": "session", "event_emitter": emit}
    first = asyncio.create_task(old.check_and_wait(**kwargs))
    token = (await emitted.get())["approval_id"]
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    second = asyncio.create_task(new.check_and_wait(**kwargs))
    fresh = (await emitted.get())["approval_id"]
    assert fresh != token
    assert not new.respond("same", "allow_once", token)
    assert not new.respond("same", "allow_once")
    assert not second.done()
    assert new.respond("same", "allow_once", fresh)
    assert await second == (True, "allow_once")


# 功能：子任务结果可通过新 Runner 的空内存注册表按持久关系查询
# 设计：执行真实 spawn 与固定模型完成子任务，再构造全新结果工具，排除旧 registry 的帮助
async def test_child_result_survives_new_registry(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    provider = FinalProvider()
    mgr = SessionManager(store, lambda: AgentRunner(KamaConfig(), provider=provider), EventBus())
    session = await mgr.create("chat", workspace=str(tmp_path))
    store.execution.begin_run(session.id, "parent", workspace=str(tmp_path), user_content="parent")
    registry = BackgroundTaskRegistry()
    spawn = SpawnAgentTool(provider, EventBus(), "parent", None, 4, registry,
                           store.runs_dir(session.id), session.id,
                           execution_store=store.execution, workspace=tmp_path)
    await spawn.invoke({"description": "child", "prompt": "child", "run_in_background": True})
    await asyncio.gather(*(task for task, _ in registry.all()))
    children = [run for run in store.execution.list_runs(session.id) if run["parent_run_id"]]
    assert len(children) == 1 and children[0]["parent_run_id"] == "parent"
    new_result = AgentResultTool(BackgroundTaskRegistry(), store.execution, session_id=session.id)
    result = await new_result.invoke({"run_id": children[0]["run_id"]})
    assert result.content == "recovered" and not result.is_error
    assert store.read_meta(session.id).run_ids == ["parent"]
    store.close()


# 功能：缺失旧元数据的已导入会话仍可列出历史但不能隐式绑定工作区执行
# 设计：导入仅 thread 的旧目录后新建管理器，验证启动索引不因可保留的残缺数据崩溃
async def test_index_keeps_incomplete_legacy_metadata_visible(tmp_path: Path) -> None:
    old = tmp_path / "legacy"
    old.mkdir()
    (old / "thread.jsonl").write_text('{"role":"user","content":"history"}\n')
    store = SessionStore(tmp_path / "sessions")
    store.import_legacy(old)
    provider = FinalProvider()
    mgr = SessionManager(store, lambda: AgentRunner(KamaConfig(), provider=provider), EventBus())
    assert mgr.list_sessions()[0]["session_id"] == "legacy"
    assert (await mgr.get_history("legacy"))[0]["content"] == "history"
    from kama_claude.core.bus.envelope import HandlerError
    with pytest.raises(HandlerError, match="unbound"):
        await mgr.send_message("legacy", "must not execute")
    assert not provider.seen
    store.close()


# 功能：审批等待取消后保留审计决定并原样传播取消，不进入工具派发
# 设计：真实 SQLite 记录待决审批，用事件屏障取消等待，核对 cancelled 决策和零 attempts
async def test_durable_approval_cancellation_keeps_cancellation(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.execution.start_daemon("old")
    manager = SessionManager(store, lambda: AgentRunner(KamaConfig()), EventBus(), epoch="old")
    session = await manager.create("chat", workspace=str(tmp_path))
    store.execution.begin_run(session.id, "run", workspace=str(tmp_path), user_content="goal")
    params = {"command": "echo test"}
    store.execution.commit_response("run", 1, [
        {"type": "tool_use", "id": "model", "name": "unknown_tool", "input": params},
    ], [{"call_id": "call", "tool_use_id": "model", "name": "unknown_tool", "input": params,
         "effect": "unknown", "retry_safe": False}])
    permissions = PermissionManager(daemon_epoch="old")
    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # 用审批事件确认等待已经建立，再触发取消
    async def emit(event: dict[str, Any]) -> None:
        await events.put(event)

    pending = asyncio.create_task(permissions.check_and_wait(
        "model", "unknown_tool", params, session.id, emit,
        call_id="call", execution_store=store.execution,
    ))
    await asyncio.wait_for(events.get(), timeout=3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert store.execution.get_approvals("run")[0]["decision"] == "cancelled"
    assert not store.execution.get_attempts("call")
    store.close()


# 功能：新 daemon 扫描之后、认领之前，旧连接不能继续提交或创建子任务
# 设计：同时保留两个真实 SQLite 连接，切换持久 epoch 后检查旧写入被拒绝且事实不变
async def test_restart_fences_stale_writer_before_claim(tmp_path: Path) -> None:
    old = SessionStore(tmp_path / "sessions")
    old.execution.start_daemon("old")
    manager = SessionManager(old, lambda: AgentRunner(KamaConfig()), EventBus(), epoch="old")
    session = await manager.create("chat", workspace=str(tmp_path))
    old.execution.begin_run(session.id, "run", workspace=str(tmp_path), user_content="goal")
    fresh = SessionStore(tmp_path / "sessions")
    fresh.execution.start_daemon("new")
    with pytest.raises(StorageError, match="epoch"):
        old.execution.commit_response("run", 1, [{"type": "text", "text": "stale"}], [])
    with pytest.raises(StorageError, match="epoch"):
        old.execution.create_child("run", "child", "goal", "", True)
    with pytest.raises(StorageError, match="epoch"):
        old.execution.review_run("run", "abandon", "stale decision")
    assert not fresh.execution.get_reviews("run")
    assert not old.execution.claim_run("run", "old", str(tmp_path))
    assert len(fresh.execution.messages(session.id)) == 1
    assert len(fresh.execution.list_runs(session.id)) == 1
    assert fresh.execution.get_run("run")["status"] == "interrupted"
    assert fresh.execution.claim_run("run", "new", str(tmp_path))
    old.close()
    fresh.close()

