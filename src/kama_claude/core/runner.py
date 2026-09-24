from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kama_claude.core.bus.events import RunFinishedEvent, RunStartedEvent
from kama_claude.core.compact.compactor import Compactor
from kama_claude.core.config import KamaConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus, EventHandler
from kama_claude.core.events.writer import EventWriter
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.lazy import LazyAnthropicProvider
from kama_claude.core.loop import AgentLoop
from kama_claude.core.mcp.server import McpServerManager
from kama_claude.core.memory.loader import load_context_file
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.runs import RUNS_DIR, new_run_id
from kama_claude.core.session.execution import ExecutionStore, StorageError
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore
from kama_claude.core.subagent.registry import BackgroundTaskRegistry
from kama_claude.core.subagent.tool import AgentResultTool, SpawnAgentTool
from kama_claude.core.task.manager import TaskManager
from kama_claude.core.tools.builtin import (
    BashTool,
    ListDirTool,
    NoteSaveTool,
    ReadFileTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    WriteFileTool,
)
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.trace.provider import TracingProvider
from kama_claude.core.trace.writer import TraceWriter


# 返回运行事件的 UTC 时间
def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunOutcome:
    status: str
    result: str
    reason: str | None


class AgentRunner:
    # 组装所有运行时依赖，准备执行一次完整的 agent run
    def __init__(
        self,
        config: KamaConfig,
        *,
        bus: EventBus | None = None,
        provider: LLMProvider | None = None,
        extra_handlers: list[EventHandler] | None = None,
        runs_dir: Path | None = None,
        trace: TraceWriter | None = None,
        permission_manager: PermissionManager | None = None,
        mcp_manager: McpServerManager | None = None,
        task_registry: BackgroundTaskRegistry | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._provider = provider
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._runs_dir = runs_dir or RUNS_DIR
        self._trace = trace
        self._permission_manager = permission_manager
        self._mcp_manager = mcp_manager
        # 跨 run 共享的后台 subagent 任务注册表
        self._task_registry = task_registry or BackgroundTaskRegistry()

    # 构建工具注册表，注入 TaskManager（任务工具共享同一实例）；可选注入 SpawnAgentTool
    def _build_registry(
        self,
        task_manager: TaskManager,
        *,
        session: Session | None = None,
        store: SessionStore | None = None,
        run_id: str | None = None,
        provider: LLMProvider | None = None,
        bus: EventBus | None = None,
        child_runs_dir: Path | None = None,
        session_id: str = "",
        tool_whitelist: list[str] | None = None,
        workspace: Path | None = None,
    ) -> ToolRegistry:
        allowed: set[str] | None = set(tool_whitelist) if tool_whitelist else None

        # 按本次技能允许列表选择工具
        def _ok(name: str) -> bool:
            return allowed is None or name in allowed

        registry = ToolRegistry()
        for t in [ReadFileTool(workspace), BashTool(workspace),
                  WriteFileTool(workspace), ListDirTool(workspace)]:
            if _ok(t.name):
                registry.register(t)
        for t in [
            TaskCreateTool(task_manager),
            TaskUpdateTool(task_manager),
            TaskListTool(task_manager),
            TaskGetTool(task_manager),
        ]:
            if _ok(t.name):
                registry.register(t)
        if session is not None and store is not None and run_id is not None:
            note_tool = NoteSaveTool(store, session.id, run_id)
            if _ok(note_tool.name):
                registry.register(note_tool)
        if provider is not None and bus is not None and run_id is not None:
            runs_dir = child_runs_dir or self._runs_dir
            if _ok("spawn_agent"):
                registry.register(
                    SpawnAgentTool(
                        provider=provider,
                        parent_bus=bus,
                        parent_run_id=run_id,
                        permission_manager=self._permission_manager,
                        max_steps=self._config.agent.max_steps,
                        task_registry=self._task_registry,
                        runs_dir=runs_dir,
                        session_id=session_id,
                        depth=0,
                        execution_store=store.execution if store is not None else None,
                        workspace=workspace,
                    )
                )
            if _ok("agent_result"):
                registry.register(AgentResultTool(
                    self._task_registry, store.execution if store is not None else None,
                    session_id=session_id,
                ))
        if self._mcp_manager is not None:
            for mcp_tool in self._mcp_manager.get_tools():
                if _ok(mcp_tool.name):
                    registry.register(mcp_tool)
        return registry

    # 执行一次完整的 agent run（委托给 run_and_capture，忽略返回值）
    async def run(self, goal: str, *, run_id: str | None = None) -> None:
        await self.run_and_capture(goal, run_id=run_id)

    # 执行 agent run 并返回 RunOutcome（含最终文字结果）
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
        resume: bool = False,
    ) -> RunOutcome:
        run_id = run_id or new_run_id()
        owns_execution = session is None or store is None
        if session is not None and store is not None:
            store.write_meta(session)
            execution = store.execution
            workspace = store.workspace(session.id)
            run_path = store.runs_dir(session.id) / run_id
            pending = None
            if resume:
                pending = {
                    call["tool_use_id"] for call in execution.list_calls(run_id)
                    if call["status"] in {"planned", "ready"}
                }
            history = store.read_messages(session.id, pending_tool_ids=pending)
            notes = store.read_notes(session.id)
            if execution.get_run(run_id) is None:
                execution.begin_run(session.id, run_id, workspace=str(workspace))
            elif (execution.get_run(run_id)["status"] != "running"  # type: ignore[index]
                  or (not resume and any(row["run_id"] == run_id and row["role"] == "assistant"
                                         for row in execution.messages(session.id)))):
                raise StorageError("run already started; use the explicit recovery entry")
        else:
            workspace = Path.cwd().resolve()
            execution = ExecutionStore(self._runs_dir.resolve() / "execution.sqlite3")
            sid = f"standalone-{run_id}"
            execution.put_session(Session(
                sid, "one_shot", "active", goal[:40], _now(), _now(), [run_id],
            ).to_dict(), workspace=str(workspace))
            execution.begin_run(sid, run_id, workspace=str(workspace), user_content=goal)
            run_path = self._runs_dir / run_id
            history = [{"role": "user", "content": goal}]
            notes = ""
        run_path.mkdir(parents=True, exist_ok=True)

        global_ctx = load_context_file(Path("~/.kama/context.md").expanduser())
        project_ctx = load_context_file(workspace / ".kama/context.md")

        task_manager = TaskManager(run_path / ".tasks")

        bus = self._bus if self._bus is not None else EventBus()
        for h in self._extra_handlers:
            bus.subscribe(h)

        context = ExecutionContext(
            run_id=run_id,
            goal=goal,
            max_steps=self._config.agent.max_steps,
            prefill_messages=history,
            session_notes=notes,
            global_context=global_ctx,
            project_context=project_ctx,
            system_prompt_override=system_prompt_override,
        )
        if resume:
            durable = execution.get_run(run_id)
            assert durable is not None
            context.step = durable["step"]

        async with EventWriter(run_path / "events.jsonl") as writer:
            writer.subscribe(bus)
            await bus.publish(RunStartedEvent(
                run_id=run_id, session_id=session.id if session is not None else None,
                goal=goal, ts=_now(),
            ))

            cancelled = False
            storage_failed = False
            try:
                provider: LLMProvider = self._provider or LazyAnthropicProvider(
                    self._config.llm.default_model,
                    base_url=self._config.llm.base_url or None,
                )
                if self._trace is not None:
                    provider = TracingProvider(
                        provider,
                        self._trace,
                        include_payload=self._config.trace.include_llm_payload,
                    )
                session_id_str = session.id if session is not None else ""
                child_runs_dir = (
                    store.runs_dir(session.id)
                    if session is not None and store is not None
                    else self._runs_dir
                )
                registry = self._build_registry(
                    task_manager,
                    session=session,
                    store=store,
                    run_id=run_id,
                    provider=provider,
                    bus=bus,
                    child_runs_dir=child_runs_dir,
                    session_id=session_id_str,
                    tool_whitelist=tool_whitelist,
                    workspace=workspace,
                )
                session_dir = (
                    store.session_dir(session.id)
                    if session is not None and store is not None
                    else run_path
                )
                compactor = Compactor(bus, session_dir, session_id_str, store=store)
                loop = AgentLoop(
                    provider, registry, bus,
                    permission_manager=self._permission_manager,
                    compactor=compactor,
                    compact_threshold=self._config.compaction.auto_threshold,
                    session_id=session_id_str,
                    execution_store=execution,
                    tool_result_limit=self._config.compaction.tool_result_limit,
                    tool_result_keep=self._config.compaction.tool_result_keep,
                )
                if resume:
                    await loop.recover_pending(context)
                await loop.run(context)
            except StorageError:
                storage_failed = True
                context.status, context.reason = "needs_review", "storage_error"
                logging.getLogger(__name__).exception(
                    "execution state was not committed run_id=%s", run_id,
                )
            except asyncio.CancelledError:
                cancelled = True
                if not context.is_done():
                    context.mark_failed("cancelled")
            except Exception:
                logging.getLogger(__name__).exception(
                    "agent run failed run_id=%s step=%d", run_id, context.step
                )
                if not context.is_done():
                    context.mark_failed("llm_error")

            if not storage_failed:
                try:
                    status = "succeeded" if context.status == "success" else context.status
                    if cancelled and status != "needs_review":
                        status = "cancelled"
                    durable_run = execution.get_run(run_id)
                    if durable_run is None or durable_run["status"] != "succeeded":
                        execution.finish_run(run_id, status, context.reason)
                except StorageError:
                    storage_failed = True
                    context.status, context.reason = "needs_review", "storage_error"
                    logging.getLogger(__name__).exception(
                        "run completion was not committed run_id=%s", run_id,
                    )

            await bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    session_id=session.id if session is not None else None,
                    status=context.status,
                    reason=context.reason,
                    steps=context.step,
                    ts=_now(),
                )
            )

        if owns_execution:
            execution.close()

        if cancelled:
            raise asyncio.CancelledError()

        return RunOutcome(
            status=context.status,
            result=context.result,
            reason=context.reason,
        )
