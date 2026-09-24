from __future__ import annotations

import asyncio
import datetime
import fnmatch
import json
import logging
import signal
import sys
import time
import uuid
from datetime import UTC
from pathlib import Path
from types import FrameType
from typing import Any

from pydantic import BaseModel

import kama_claude
from kama_claude.core.bus.commands import (
    AgentRunCommand,
    AgentRunResult,
    EventSubscribeCommand,
    EventSubscribeResult,
    PermissionRespondCommand,
    PermissionRespondResult,
    PongResult,
    SessionCloseCommand,
    SessionCloseResult,
    SessionCompactCommand,
    SessionCompactResult,
    SessionCreateCommand,
    SessionCreateResult,
    SessionGetHistoryCommand,
    SessionGetHistoryResult,
    SessionListResult,
    SessionResumeCommand,
    SessionResumeResult,
    SessionReviewCommand,
    SessionSendMessageCommand,
    SessionSendMessageResult,
    SessionStatusCommand,
    SessionStatusResult,
)
from kama_claude.core.bus.envelope import EventPushEnvelope
from kama_claude.core.config import KamaConfig, get_config
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.lazy import LazyAnthropicProvider
from kama_claude.core.logging_setup import setup_logging
from kama_claude.core.mcp.server import McpServerManager
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.storage import load_policy_file
from kama_claude.core.runner import AgentRunner
from kama_claude.core.runs import events_file, new_run_id
from kama_claude.core.session import SessionManager, SessionStore
from kama_claude.core.session.daemon_lock import DaemonLock
from kama_claude.core.subagent.registry import BackgroundTaskRegistry
from kama_claude.core.trace.record import TraceRecord
from kama_claude.core.trace.writer import TraceWriter
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from kama_claude.core.transport.socket_server import SocketServer, get_connection_writer

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


class CoreApp:
    # 初始化 daemon，可注入本地测试依赖而无需生产故障开关
    def __init__(
        self, *, config: KamaConfig | None = None, provider: LLMProvider | None = None,
        sessions_root: Path | None = None, permission_manager: PermissionManager | None = None,
    ) -> None:
        self._start_time = time.monotonic()
        self._bus = EventBus()
        self._broadcaster: IpcEventBroadcaster | None = None
        self._trace: TraceWriter | None = None
        self._config: KamaConfig | None = None
        self._running_runs: set[asyncio.Task[Any]] = set()
        self._sessions: SessionManager | None = None
        self._permission_manager: PermissionManager | None = None
        self._mcp_manager: McpServerManager | None = None
        self._injected_config = config
        self._provider = provider
        self._sessions_root = sessions_root
        self._injected_permissions = permission_manager
        self._epoch = uuid.uuid4().hex
        self._task_registry = BackgroundTaskRegistry()
        self._store: SessionStore | None = None
        self._server: SocketServer | None = None

    # 处理 core.ping 请求，返回服务版本、运行时长和接收时间
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        client = params.get("client", "unknown")
        logger.debug("ping from %s", client)
        return PongResult(
            server_version=kama_claude.__version__,
            uptime_ms=int((time.monotonic() - self._start_time) * 1000),
            received_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )

    # 将 EventBus 事件写入 trace（作为 EventBus 订阅者）
    async def _trace_event_handler(self, event: BaseModel) -> None:
        assert self._trace is not None
        event_dict = event.model_dump()
        self._trace.emit(
            TraceRecord(
                ts=_now(),
                direction="CORE",
                layer="event",
                kind="event",
                run_id=event_dict.get("run_id"),
                data=event_dict,
            )
        )

    # 启动一次 agent run：异步创建 AgentRunner 并立即返回 run_id
    async def _agent_run_handler(self, params: dict[str, Any]) -> AgentRunResult:
        assert self._sessions is not None
        cmd = AgentRunCommand.model_validate(params)
        session = await self._sessions.create(mode="one_shot", title=cmd.goal[:40])
        run_id = new_run_id()
        run_task = asyncio.create_task(
            self._sessions.send_message(session.id, cmd.goal, run_id=run_id)
        )
        self._running_runs.add(run_task)
        run_task.add_done_callback(self._running_runs.discard)
        return AgentRunResult(run_id=run_id)

    # 创建 chat 或 one_shot session，并返回 session_id
    async def _session_create_handler(self, params: dict[str, Any]) -> SessionCreateResult:
        assert self._sessions is not None
        cmd = SessionCreateCommand.model_validate(params)
        session = await self._sessions.create(
            mode=cmd.mode, title=cmd.title, workspace=cmd.workspace,
        )
        return SessionCreateResult(session_id=session.id, status=session.status)

    # 持久接受业务消息后立即返回任务身份，执行不依赖客户端连接
    async def _session_send_handler(self, params: dict[str, Any]) -> SessionSendMessageResult:
        assert self._sessions is not None
        cmd = SessionSendMessageCommand.model_validate(params)
        run_id = await self._sessions.submit_message(
            cmd.session_id, cmd.content, request_id=cmd.request_id,
        )
        return SessionSendMessageResult(run_id=run_id)

    # 列出已持久化会话及当前主任务状态
    async def _session_list_handler(self, params: dict[str, Any]) -> SessionListResult:
        assert self._sessions is not None
        return SessionListResult(sessions=self._sessions.list_sessions())

    # 返回恢复所需的完整事实与核查记录
    async def _session_status_handler(self, params: dict[str, Any]) -> SessionStatusResult:
        assert self._sessions is not None
        cmd = SessionStatusCommand.model_validate(params)
        return SessionStatusResult(**self._sessions.status(cmd.session_id))

    # 认领中断任务并异步继续，重复请求返回同一任务状态
    async def _session_resume_handler(self, params: dict[str, Any]) -> SessionResumeResult:
        assert self._sessions is not None
        cmd = SessionResumeCommand.model_validate(params)
        return SessionResumeResult(**await self._sessions.resume(
            cmd.session_id, cmd.run_id, cmd.workspace,
        ))

    # 保存用户对未知执行的明确处理选择
    async def _session_review_handler(self, params: dict[str, Any]) -> SessionResumeResult:
        assert self._sessions is not None
        cmd = SessionReviewCommand.model_validate(params)
        return SessionResumeResult(**await self._sessions.review(
            cmd.session_id, cmd.run_id, cmd.action, cmd.note,
        ))

    # 返回 session 的完整 Anthropic messages 历史
    async def _session_history_handler(self, params: dict[str, Any]) -> SessionGetHistoryResult:
        assert self._sessions is not None
        cmd = SessionGetHistoryCommand.model_validate(params)
        messages = await self._sessions.get_history(cmd.session_id)
        return SessionGetHistoryResult(messages=messages)

    # 接收客户端权限审批响应，resolve 对应挂起的 Future
    async def _permission_respond_handler(self, params: dict[str, Any]) -> PermissionRespondResult:
        cmd = PermissionRespondCommand.model_validate(params)
        logger.info(
            "permission.respond received tool_use_id=%s decision=%s",
            cmd.tool_use_id, cmd.decision,
        )
        if self._permission_manager is None:
            logger.error("permission.respond: PermissionManager not initialized")
            return PermissionRespondResult()
        accepted = self._permission_manager.respond(
            cmd.tool_use_id, cmd.decision, cmd.approval_id,
        )
        return PermissionRespondResult(ok=accepted)

    # 手动压缩 session thread，将摘要持久化写入 thread.jsonl
    async def _session_compact_handler(self, params: dict[str, Any]) -> SessionCompactResult:
        assert self._sessions is not None
        cmd = SessionCompactCommand.model_validate(params)
        result = await self._sessions.compact(cmd.session_id, cmd.focus)
        return result  # type: ignore[no-any-return]

    # 关闭 session 并返回 closed 状态
    async def _session_close_handler(self, params: dict[str, Any]) -> SessionCloseResult:
        assert self._sessions is not None
        cmd = SessionCloseCommand.model_validate(params)
        await self._sessions.close(cmd.session_id)
        return SessionCloseResult(status="closed")

    # 注册客户端事件订阅，可选先回放 events.jsonl 历史再接收实时流
    async def _subscribe_handler(self, params: dict[str, Any]) -> EventSubscribeResult:
        cmd = EventSubscribeCommand.model_validate(params)
        writer = get_connection_writer()

        replayed_count = 0
        if cmd.replay_from_run is not None:
            replayed_count = await self._replay_events(
                cmd.replay_from_run, writer, cmd.topics
            )

        assert self._broadcaster is not None
        sub_id = self._broadcaster.subscribe(writer, cmd.topics, cmd.scope)
        return EventSubscribeResult(subscription_id=sub_id, replayed_count=replayed_count)

    # 从 events.jsonl 向 writer 回放匹配 topic 的历史事件，返回已回放条数
    async def _replay_events(
        self,
        run_id: str,
        writer: asyncio.StreamWriter,
        topics: list[str],
    ) -> int:
        path = events_file(run_id)
        if not path.exists():
            for candidate in Path("~/.kama/sessions").expanduser().glob(
                f"*/runs/{run_id}/events.jsonl"
            ):
                path = candidate
                break
        if not path.exists():
            return 0

        count = 0
        for line in path.read_text().splitlines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type: str = event.get("type", "")
            if not any(fnmatch.fnmatch(event_type, p) for p in topics):
                continue
            envelope = EventPushEnvelope(event=event)
            writer.write(envelope.model_dump_json().encode() + b"\n")
            count += 1

        if count:
            await writer.drain()
        return count

    # 持有数据目录的系统锁覆盖整个生命周期，启动失败和取消时也完整释放资源
    async def run(self) -> None:
        sessions_root = (self._sessions_root or Path("~/.kama/sessions")).expanduser().resolve()
        lock = DaemonLock(sessions_root / "daemon.lock")
        lock.acquire()
        try:
            await self._run(sessions_root)
        finally:
            try:
                if self._server is not None:
                    await self._server.stop()
                for run_task in list(self._running_runs):
                    run_task.cancel()
                if self._running_runs:
                    await asyncio.gather(*self._running_runs, return_exceptions=True)
                if self._sessions is not None:
                    await self._sessions.shutdown()
                await self._task_registry.cancel_all()
                if self._mcp_manager is not None:
                    await self._mcp_manager.stop_all()
                if self._store is not None:
                    self._store.close()
                if self._trace is not None:
                    await self._trace.stop()
            finally:
                lock.close()

    # 初始化配置、索引和协议处理器后监听本地端口
    async def _run(self, sessions_root: Path) -> None:
        self._start_time = time.monotonic()
        self._config = self._injected_config or get_config()
        setup_logging(self._config)

        if self._config.trace.enabled:
            trace_path = Path(self._config.trace.file).expanduser()
            self._trace = TraceWriter(trace_path)
            await self._trace.start()
            self._bus.subscribe(self._trace_event_handler)

        policy_file = Path("~/.kama/policy.toml").expanduser()
        self._permission_manager = self._injected_permissions or PermissionManager(
            policy_file=policy_file,
            timeout_s=self._config.permission.timeout_s,
            daemon_epoch=self._epoch,
        )
        self._permission_manager.begin_epoch(self._epoch)
        logger.info(
            "permission manager: timeout_s=%.1f  persistent=%d entries",
            self._config.permission.timeout_s,
            len(load_policy_file(policy_file)),
        )

        self._broadcaster = IpcEventBroadcaster(trace=self._trace)
        self._bus.subscribe(self._broadcaster.handle)
        store = SessionStore(sessions_root)
        self._store = store
        store.execution.start_daemon(self._epoch)
        assert self._config is not None
        compact_provider = self._provider or LazyAnthropicProvider(
            self._config.llm.default_model,
            base_url=self._config.llm.base_url or None,
        )

        self._mcp_manager = McpServerManager()
        if self._config.mcp.servers:
            logger.info("mcp: starting %d server(s)", len(self._config.mcp.servers))
            await self._mcp_manager.start_all(self._config.mcp.servers)

        self._sessions = SessionManager(
            store,
            runner_factory=lambda: AgentRunner(
                self._config,  # type: ignore[arg-type]
                bus=self._bus,
                trace=self._trace,
                permission_manager=self._permission_manager,
                mcp_manager=self._mcp_manager,
                provider=self._provider,
                task_registry=self._task_registry,
            ),
            bus=self._bus,
            provider=compact_provider,
            epoch=self._epoch,
        )

        server = SocketServer(
            self._config.host,
            self._config.port,
            self._broadcaster,
            trace=self._trace,
        )
        self._server = server
        server.register("core.ping", self._ping_handler)
        server.register("agent.run", self._agent_run_handler)
        server.register("event.subscribe", self._subscribe_handler)
        server.register("session.create", self._session_create_handler)
        server.register("session.send_message", self._session_send_handler)
        server.register("session.get_history", self._session_history_handler)
        server.register("session.close", self._session_close_handler)
        server.register("permission.respond", self._permission_respond_handler)
        server.register("session.compact", self._session_compact_handler)
        server.register("session.list", self._session_list_handler)
        server.register("session.status", self._session_status_handler)
        server.register("session.resume", self._session_resume_handler)
        server.register("session.review", self._session_review_handler)

        addr = await server.start()
        logger.info("kama-core %s listening addr=%s", kama_claude.__version__, addr)
        logger.info("config: %s", self._config)

        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

        # Windows 主线程信号桥接到 asyncio，保留原生 Proactor 子进程支持
        def request_shutdown(signum: int, frame: FrameType | None) -> None:
            loop.call_soon_threadsafe(shutdown.set)

        try:
            for sig in previous:
                if sys.platform == "win32":
                    signal.signal(sig, request_shutdown)
                else:
                    loop.add_signal_handler(sig, shutdown.set)
            await shutdown.wait()
        finally:
            for sig, handler in previous.items():
                if sys.platform != "win32":
                    loop.remove_signal_handler(sig)
                signal.signal(sig, handler)
            logger.info("shutting down")


# 同步入口：启动 CoreApp 事件循环
def run() -> None:
    try:
        asyncio.run(CoreApp().run())
    except KeyboardInterrupt:
        return
