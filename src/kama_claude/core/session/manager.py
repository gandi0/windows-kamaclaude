from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kama_claude.core.bus.envelope import HandlerError
from kama_claude.core.bus.events import (
    RunFinishedEvent,
    SessionClosedEvent,
    SessionCreatedEvent,
    SessionMessageReceivedEvent,
    SessionResumedEvent,
    SessionWaitingForInputEvent,
    SkillInvokedEvent,
)
from kama_claude.core.events.bus import EventBus
from kama_claude.core.runs import new_run_id
from kama_claude.core.session.execution import StorageError
from kama_claude.core.session.model import Session, SessionMode
from kama_claude.core.session.store import SessionStore
from kama_claude.core.skills.loader import SkillLoader

if TYPE_CHECKING:
    from kama_claude.core.llm.base import LLMProvider
    from kama_claude.core.runner import AgentRunner

SESSION_NOT_FOUND = -32010
SESSION_CLOSED = -32011
SESSION_BUSY = -32012
SESSION_STORAGE_ERROR = -32013


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionManager:
    # 初始化会话管理器，接入文件存储、runner 工厂、事件总线和可选的 LLM provider（用于手动压缩）
    def __init__(
        self,
        store: SessionStore,
        runner_factory: Callable[[], AgentRunner],
        bus: EventBus,
        provider: LLMProvider | None = None,
        epoch: str | None = None,
    ) -> None:
        self._store = store
        self._runner_factory = runner_factory
        self._bus = bus
        self._provider = provider
        self.epoch = epoch or uuid.uuid4().hex
        self._sessions = {
            data["id"]: Session.from_dict(data) for data in store.execution.list_sessions()
        }
        self._locks = {sid: asyncio.Lock() for sid in self._sessions}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = False
        self._skill_loader = SkillLoader()

    # 创建新 session 并在发布事件前提交 SQLite 元数据
    async def create(
        self, mode: SessionMode, title: str = "", *, workspace: str | None = None,
    ) -> Session:
        sid = f"sess-{uuid.uuid4().hex[:12]}"
        ts = _now()
        session = Session(
            id=sid,
            mode=mode,
            status="active",
            title=title,
            created_at=ts,
            updated_at=ts,
            run_ids=[],
        )
        if workspace is not None and (
            not Path(workspace).is_absolute() or not Path(workspace).is_dir()
        ):
            raise HandlerError(
                SESSION_STORAGE_ERROR, "workspace must be an existing absolute directory",
            )
        self._store.write_meta(session, workspace=workspace)
        self._sessions[sid] = session
        self._locks[sid] = asyncio.Lock()
        await self._bus.publish(SessionCreatedEvent(session_id=sid, mode=mode, ts=ts))
        return session

    # 兼容同步调用方，客户端断开不撤销已经持久接受的任务
    async def send_message(
        self, sid: str, content: str, *, run_id: str | None = None,
        request_id: str | None = None,
    ) -> str:
        accepted = await self.submit_message(sid, content, run_id=run_id, request_id=request_id)
        task = self._tasks.get(accepted)
        if task is not None:
            await asyncio.shield(task)
        return accepted

    # 原子接受业务请求并调度任务，同一业务键重发始终返回原 run
    async def submit_message(
        self, sid: str, content: str, *, run_id: str | None = None,
        request_id: str | None = None,
    ) -> str:
        if self._stopping:
            raise HandlerError(SESSION_BUSY, "daemon is shutting down")
        session = self._get_session(sid)
        try:
            prior = (
                self._store.execution.get_request(sid, request_id, content) if request_id else None
            )
        except StorageError as exc:
            raise HandlerError(SESSION_STORAGE_ERROR, str(exc)) from exc
        if prior is not None:
            return str(prior["run_id"])
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        async with lock:
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")

            # Skill 解析：检测 "/" 前缀，展开为系统提示覆盖和工具白名单
            goal = content
            system_prompt_override: str | None = None
            tool_whitelist: list[str] | None = None
            if content.startswith("/") and content[1:].strip():
                parts = content[1:].split(None, 1)
                skill_name = parts[0]
                arguments = parts[1] if len(parts) > 1 else ""
                skill = self._skill_loader.resolve(skill_name)
                if skill is not None:
                    goal = self._skill_loader.render_prompt(skill, arguments)
                    system_prompt_override = skill.system_prompt_template
                    tool_whitelist = skill.allowed_tools or None
            candidate = run_id or new_run_id()
            config = {"goal": goal, "system_prompt_override": system_prompt_override,
                      "tool_whitelist": tool_whitelist}
            if (content.startswith("/") and content[1:].strip()
                    and system_prompt_override is not None):
                config["skill_name"] = skill_name
                config["skill_arguments"] = arguments
            try:
                self._store.read_messages(sid)
                accepted = self._store.execution.begin_run(
                    sid, candidate, workspace=str(self._store.workspace(sid)),
                    user_content=content, request_id=request_id, owner_epoch=self.epoch,
                    execution_config=config,
                )
            except StorageError as exc:
                raise HandlerError(SESSION_STORAGE_ERROR, str(exc)) from exc
            if accepted != candidate:
                return accepted
            session.status = "active"
            session.title = session.title or content[:40]
            session.run_ids.append(accepted)
            self._launch(session, accepted, config, content=content)
            return accepted

    # 把已接受的任务交给 daemon 所有并保存活动任务引用
    def _launch(
        self, session: Session, run_id: str, config: dict[str, Any], *,
        content: str | None = None, resume: bool = False,
    ) -> None:
        task = asyncio.create_task(self._execute(session, run_id, config, content, resume))
        self._tasks[run_id] = task
        task.add_done_callback(lambda done: self._tasks.pop(run_id, None))

    # 执行接受或恢复的任务并只在持久完成后发布可继续输入状态
    async def _execute(
        self, session: Session, run_id: str, config: dict[str, Any],
        content: str | None, resume: bool,
    ) -> None:
        sid = session.id
        try:
            if content is not None:
                await self._bus.publish(SessionMessageReceivedEvent(
                    session_id=sid, content=content, ts=_now(),
                ))
                if "skill_name" in config:
                    await self._bus.publish(SkillInvokedEvent(
                        skill_name=config["skill_name"], arguments=config["skill_arguments"],
                        run_id=run_id, ts=_now(),
                    ))
            else:
                await self._bus.publish(SessionResumedEvent(session_id=sid, ts=_now()))
            kwargs: dict[str, Any] = {
                "run_id": run_id, "session": session, "store": self._store,
                "system_prompt_override": config.get("system_prompt_override"),
                "tool_whitelist": config.get("tool_whitelist"),
            }
            if resume:
                kwargs["resume"] = True
            outcome = await self._runner_factory().run_and_capture(config["goal"], **kwargs)
            if outcome.status == "needs_review":
                return
            # S9.1: summarize skill 完成后，把人类可读摘要也持久化到 summaries 表
            if config.get("skill_name") == "summarize" and outcome.result:
                try:
                    self._store.write_skill_summary(
                        sid, run_id, summary_text=outcome.result,
                        generation_config={"source": "skill", "name": "summarize",
                                           "arguments": config.get("skill_arguments", "")},
                    )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "session: failed to persist skill summary sid=%s run=%s", sid, run_id,
                    )
            session.updated_at = _now()
            if session.mode == "one_shot":
                session.status = "closed"
                self._store.write_meta(session)
                await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))
            else:
                session.status = "waiting_for_input"
                self._store.write_meta(session)
                await self._bus.publish(SessionWaitingForInputEvent(
                    session_id=sid, last_run_id=run_id, ts=session.updated_at,
                ))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.getLogger(__name__).exception("session execution stopped run_id=%s", run_id)
            try:
                self._store.execution.block_run(run_id, f"execution_blocked: {exc}")
            except StorageError:
                pass
            await self._bus.publish(RunFinishedEvent(
                run_id=run_id, session_id=sid, status="needs_review", reason=str(exc),
                steps=0, ts=_now(),
            ))

    # 从权威数据库派生会话摘要，不把重启前 active 当成仍在执行
    def list_sessions(self) -> list[dict[str, Any]]:
        result = []
        for data in self._store.execution.list_sessions():
            run_id = data.get("current_run_id")
            run = self._store.execution.get_run(run_id) if run_id else None
            result.append({
                "session_id": data["id"], "title": data.get("title", ""),
                "status": data.get("status", "active"), "workspace": data.get("workspace"),
                "current_run_id": run_id, "run_status": run["status"] if run else None,
                "reason": run.get("reason") if run else None,
            })
        return result

    # 返回当前主任务完整调用事实、历史核查选择和子任务关系
    def status(self, sid: str) -> dict[str, Any]:
        self._get_session(sid)
        summary = next(item for item in self.list_sessions() if item["session_id"] == sid)
        runs = self._store.execution.list_runs(sid)
        current = summary["current_run_id"]
        calls = self._store.execution.list_calls(current) if current else []
        for call in calls:
            call["attempts"] = self._store.execution.get_attempts(call["call_id"])
        return {
            "session": summary, "runs": runs, "calls": calls,
            "reviews": self._store.execution.get_reviews(current) if current else [],
            "children": [run for run in runs if run.get("parent_run_id")],
        }

    # 校验任务属于会话且是当前主任务，禁止用恢复接口接管子任务
    def _main_run(self, sid: str, run_id: str) -> dict[str, Any]:
        self._get_session(sid)
        data = self._store.execution.get_session(sid)
        run = self._store.execution.get_run(run_id)
        if (run is None or run["session_id"] != sid or run.get("parent_run_id")
                or data is None or data.get("current_run_id") != run_id):
            raise HandlerError(SESSION_NOT_FOUND, "current main run not found in session")
        return run

    # 原子认领恢复权并立即返回，重复请求不调度第二个执行协程
    async def resume(self, sid: str, run_id: str, workspace: str) -> dict[str, Any]:
        if self._stopping:
            raise HandlerError(SESSION_BUSY, "daemon is shutting down")
        session = self._get_session(sid)
        self._main_run(sid, run_id)
        if session.status == "closed":
            raise HandlerError(SESSION_CLOSED, "session already closed")
        async with self._locks[sid]:
            try:
                started = self._store.execution.claim_run(run_id, self.epoch, workspace)
                run = self._main_run(sid, run_id)
                if started:
                    config = run.get("execution_config") or {}
                    if "goal" not in config:
                        self._store.execution.block_run(
                            run_id, "legacy_execution_config_unverified",
                        )
                        started = False
                        run = self._main_run(sid, run_id)
                    else:
                        self._launch(session, run_id, config, resume=True)
                return {"run_id": run_id, "status": run["status"], "started": started,
                        "reason": run.get("reason")}
            except StorageError as exc:
                raise HandlerError(SESSION_STORAGE_ERROR, str(exc)) from exc

    # 记录用户的暂停或放弃选择，放弃关闭会话但保留所有未知执行事实
    async def review(self, sid: str, run_id: str, action: str, note: str) -> dict[str, Any]:
        self._main_run(sid, run_id)
        if run_id in self._tasks:
            raise HandlerError(SESSION_BUSY, "run still active; wait for its execution to stop")
        if not note.strip():
            raise HandlerError(SESSION_STORAGE_ERROR, "review note is required")
        async with self._locks[sid]:
            try:
                self._store.execution.review_run(run_id, action, note)
                if action == "abandon":
                    session = self._get_session(sid)
                    session.status = "closed"
                    self._store.write_meta(session)
                run = self._main_run(sid, run_id)
                return {"run_id": run_id, "status": run["status"], "started": False,
                        "reason": run.get("reason")}
            except StorageError as exc:
                raise HandlerError(SESSION_STORAGE_ERROR, str(exc)) from exc

    # daemon 退出时取消并等待自己创建的任务，避免关库后继续执行
    async def shutdown(self) -> None:
        self._stopping = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # 关闭指定 session 并提交 SQLite 元数据
    async def close(self, sid: str) -> None:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            data = self.status(sid)
            if data["session"]["run_status"] in {
                "running", "created", "waiting_approval", "interrupted", "needs_review",
            }:
                raise HandlerError(
                    SESSION_BUSY, "unfinished run requires recovery or explicit review",
                )
            session.status = "closed"
            session.updated_at = _now()
            self._store.write_meta(session)
            await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))

    # 手动压缩当前持久工作视图，原子切换 SQLite 有效摘要引用
    async def compact(self, sid: str, focus: str = "") -> Any:
        self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        if self._provider is None:
            raise HandlerError(-32020, "provider not available for compaction")
        async with lock:
            if self.status(sid)["session"]["run_status"] in {
                "running", "created", "waiting_approval", "interrupted", "needs_review",
            }:
                raise HandlerError(SESSION_BUSY, "unfinished run cannot be compacted")
            from kama_claude.core.bus.commands import SessionCompactResult
            from kama_claude.core.compact.compactor import Compactor
            current_run_id = self.status(sid)["session"]["current_run_id"]
            if current_run_id is None:
                raise HandlerError(-32021, "compaction requires committed session history")
            session_dir = self._store.session_dir(sid)
            compactor = Compactor(self._bus, session_dir, sid, store=self._store)
            result = await compactor.compact_persisted(
                str(current_run_id), self._provider, focus=focus,
            )
            if result is None:
                raise HandlerError(-32021, "compaction failed or not beneficial")
            assert result.summary_id is not None
            assert result.from_seq is not None and result.to_seq is not None
            summary = self._store.execution.get_summary(result.summary_id)
            assert summary is not None
            return SessionCompactResult(
                summary_tokens=result.summary_tokens,
                saved_tokens=max(0, result.original_token_estimate - result.summary_tokens),
                summary_id=result.summary_id,
                summary_version=int(summary["version"]),
                summary_from=result.from_seq,
                summary_to=result.to_seq,
            )

    # 读取指定 session 的完整 thread 历史
    async def get_history(self, sid: str) -> list[dict[str, Any]]:
        self._get_session(sid)
        return self._store.read_raw_messages(sid)

    # 从内存索引取 session，不存在时抛 JSON-RPC 结构化错误
    def _get_session(self, sid: str) -> Session:
        data = self._store.execution.get_session(sid)
        if data is not None:
            self._sessions[sid] = Session.from_dict(data)
            self._locks.setdefault(sid, asyncio.Lock())
        session = self._sessions.get(sid)
        if session is None:
            raise HandlerError(SESSION_NOT_FOUND, "session not found")
        return session
