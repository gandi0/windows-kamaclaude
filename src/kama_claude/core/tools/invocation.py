from __future__ import annotations

import asyncio
import copy
import errno
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from kama_claude.core.bus.events import (
    PermissionDeniedEvent,
    PermissionGrantedEvent,
    PermissionRequestedEvent,
    ToolCallFailedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)
from kama_claude.core.tools.base import Outcome, ToolAttempt, ToolResult
from kama_claude.core.tools.errors import RateLimitedError, ToolCancelledError

if TYPE_CHECKING:
    from kama_claude.core.events.bus import EventBus
    from kama_claude.core.llm.types import ToolCallBlock
    from kama_claude.core.permissions.manager import PermissionManager
    from kama_claude.core.session.execution import ExecutionStore
    from kama_claude.core.tools.registry import ToolRegistry

_DEFAULT_TIMEOUT = 120.0
_MAX_RETRIES = 2
_RETRY_BASE_S = 2.0
_TRANSIENT_ERRNOS = {errno.EAGAIN, errno.EINTR, errno.EBUSY}


# 返回事件时间戳
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 防止依赖吞掉取消后继续产生新的工具或模型请求
def check_cancelled() -> None:
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError()


# 校验和审批后按工具契约有限重试，记录调用和每次尝试，取消始终向上传播
async def invoke_tool(
    registry: ToolRegistry,
    tool_call: ToolCallBlock,
    bus: EventBus,
    run_id: str,
    timeout: float = _DEFAULT_TIMEOUT,
    *,
    permission_manager: PermissionManager | None = None,
    session_id: str = "",
    call_id: str | None = None,
    execution_store: ExecutionStore | None = None,
    recovery: bool = False,
) -> ToolResult:
    from kama_claude.core.session.execution import StorageError

    if execution_store is not None and call_id is None:
        raise StorageError("durable invocation requires a committed call_id")
    # 固定已计划参数，审批回调或工具内部修改不能改变后续实际派发内容
    from kama_claude.core.llm.types import ToolCallBlock
    tool_call = ToolCallBlock(tool_call.id, tool_call.name, copy.deepcopy(tool_call.input))
    call_id = call_id or uuid.uuid4().hex
    started = time.monotonic()
    attempts: list[ToolAttempt] = []
    attempt_id = ""
    attempt = 0
    active = False
    last_result: ToolResult | None = None
    committed = False
    tool = registry.get(tool_call.name)
    if execution_store is not None:
        intent = execution_store.get_call(call_id)
        if (intent is None or intent["run_id"] != run_id
                or intent["tool_use_id"] != tool_call.id or intent["name"] != tool_call.name
                or intent["input"] != tool_call.input
                or intent["effect"] != (tool.effect if tool else "unknown")
                or intent["retry_safe"] != (tool.retry_safe if tool else False)):
            raise StorageError("invocation does not match committed call intent")
        if intent["status"] != "planned" and not (recovery and intent["status"] == "ready"):
            raise StorageError("call already started; automatic replay is not available")
        if recovery and intent["status"] == "ready":
            previous = execution_store.get_attempts(call_id)
            if (not intent["retry_safe"] or not previous or len(previous) > _MAX_RETRIES
                    or any(row["phase"] != "result_committed" or row["outcome"] != "known"
                           for row in previous)
                    or not intent["result"] or not intent["result"].get("transient")):
                raise StorageError("safe retry history cannot be verified")
            attempts = [ToolAttempt(
                row["attempt_id"], row["number"], cast(Outcome, row["outcome"]),
                row["error_type"], row["cleanup_confirmed"],
            ) for row in previous]

    # 把完整结果绑定到当前调用，不通过参数相同推断为同一次执行
    def attach(result: ToolResult) -> ToolResult:
        result.call_id = call_id
        result.attempts = list(attempts)
        return result

    # 保存本次真实尝试的结构化结论；前置校验失败不伪造 attempt
    def record(result: ToolResult) -> None:
        if attempt_id and not any(item.attempt_id == attempt_id for item in attempts):
            attempts.append(ToolAttempt(
                attempt_id, attempt, result.outcome, result.error_type, result.cleanup_confirmed
            ))

    # 在诊断事件和下一次执行前提交结果；持久化失败直接终止调用链
    def persist(result: ToolResult, *, retry: bool = False) -> None:
        nonlocal committed
        attach(result)
        if execution_store is not None and not committed:
            execution_store.commit_result(call_id, result, retry=retry)
        committed = not retry

    # 发布带调用与尝试标识的失败诊断事件，不把事件日志当作持久执行记录
    async def failed(result: ToolResult) -> ToolResult:
        attach(result)
        await bus.publish(ToolCallFailedEvent(
            run_id=run_id, tool_use_id=tool_call.id, tool_name=tool_call.name,
            call_id=call_id, attempt_id=attempt_id, attempt=attempt,
            error_class=result.error_type or "runtime_error", error_message=result.content,
            outcome=result.outcome, cleanup_confirmed=result.cleanup_confirmed,
            elapsed_ms=int((time.monotonic() - started) * 1000), ts=_now(),
        ))
        return result

    # 保存未启动工具的已知结论，不生成虚假的派发记录
    async def rejected(result: ToolResult) -> ToolResult:
        persist(result)
        return await failed(result)

    try:
        check_cancelled()
        await bus.publish(ToolCallStartedEvent(
            run_id=run_id, tool_use_id=tool_call.id, tool_name=tool_call.name,
            params=copy.deepcopy(tool_call.input), call_id=call_id, ts=_now(),
        ))
        if tool is None:
            return await rejected(ToolResult(
                f"unknown tool: {tool_call.name}", True, "runtime_error", outcome="not_started"
            ))
        if tool.params_model is not None:
            try:
                tool.params_model.model_validate(dict(tool_call.input))
            except ValidationError as exc:
                return await rejected(ToolResult(
                    str(exc), True, "schema_error", outcome="not_started"
                ))
        if permission_manager is not None:
            approval: dict[str, Any] = {}
            # 桥接权限事件，保留现有审批协议
            async def emit_permission(raw: dict[str, Any]) -> None:
                approval.update(raw)
                await bus.publish(PermissionRequestedEvent(**raw, run_id=run_id))

            allowed, decision = await permission_manager.check_and_wait(
                tool_use_id=tool_call.id, tool_name=tool_call.name,
                params=copy.deepcopy(tool_call.input),
                session_id=session_id, event_emitter=emit_permission,
                call_id=call_id, execution_store=execution_store,
            )
            check_cancelled()
            if not allowed:
                if decision != "auto_deny":
                    await bus.publish(PermissionDeniedEvent(
                        run_id=run_id, tool_use_id=tool_call.id, decision=decision, ts=_now(),
                        approval_id=approval.get("approval_id"),
                        daemon_epoch=approval.get("daemon_epoch"),
                    ))
                return await rejected(ToolResult(
                    "Permission approval timed out." if decision == "timeout" else
                    "Permission denied by user. You may not execute this command.",
                    True, "approval_timeout" if decision == "timeout" else "permission_denied",
                    outcome="not_started",
                ))
            if decision != "auto_allow":
                await bus.publish(PermissionGrantedEvent(
                    run_id=run_id, tool_use_id=tool_call.id, decision=decision, ts=_now(),
                    approval_id=approval.get("approval_id"),
                    daemon_epoch=approval.get("daemon_epoch"),
                ))

        if timeout <= 0:
            return await rejected(ToolResult(
                "Tool deadline expired before execution.", True, "timeout", outcome="not_started"
            ))

        for attempt in range(len(attempts) + 1, _MAX_RETRIES + 2):
            check_cancelled()
            attempt_id = uuid.uuid4().hex
            if execution_store is not None:
                execution_store.start_attempt(call_id, attempt_id, attempt)
            active = True
            attempt_started = time.monotonic()
            deadline = asyncio.timeout(timeout)
            try:
                async with deadline:
                    result = await tool.invoke(copy.deepcopy(tool_call.input))
                if deadline.expired() or time.monotonic() - attempt_started >= timeout:
                    result = ToolResult(
                        f"tool exceeded its {timeout}s deadline", True, "timeout",
                        outcome="known" if tool.effect == "read_only" else "unknown",
                        cleanup_confirmed=result.cleanup_confirmed,
                    )
            except ToolCancelledError as exc:
                # Python 3.12 不把 CancelledError 子类转换为 TimeoutError，依据本层截止状态分类。
                task = asyncio.current_task()
                if not deadline.expired() or (task is not None and task.cancelling()):
                    raise
                result = exc.result
                if result.error_type != "cleanup_failed":
                    result.error_type = "timeout"
                    result.content = f"tool timed out after {timeout}s"
            except RateLimitedError as exc:
                result = ToolResult(str(exc), True, "rate_limited", transient=True,
                                    outcome="known" if tool.effect == "read_only" else "unknown")
            except TimeoutError as exc:
                cause = exc.__cause__
                if isinstance(cause, ToolCancelledError):
                    result = cause.result
                    if result.error_type != "cleanup_failed":
                        result.error_type = "timeout"
                        result.content = f"tool timed out after {timeout}s"
                else:
                    result = ToolResult(
                        f"tool timed out after {timeout}s", True, "timeout",
                        outcome="known" if tool.effect == "read_only" else "unknown",
                    )
            except PermissionError as exc:
                result = ToolResult(str(exc), True, "permission_denied",
                                    outcome="known" if tool.effect == "read_only" else "unknown")
            except Exception as exc:
                transient = isinstance(exc, OSError) and exc.errno in _TRANSIENT_ERRNOS
                result = ToolResult(str(exc), True, "runtime_error", transient=transient,
                                    outcome="known" if tool.effect == "read_only" else "unknown")
            if result.outcome == "unknown":
                result.is_error = True
                result.error_type = result.error_type or "outcome_unknown"
            record(result)
            active = False
            last_result = result
            attach(result)
            task = asyncio.current_task()
            retry = bool(
                result.is_error and tool.retry_safe and result.transient
                and result.outcome != "unknown"
                and result.error_type in {"runtime_error", "rate_limited"}
                and attempt <= _MAX_RETRIES
                and not (task is not None and task.cancelling())
            )
            persist(result, retry=retry)
            check_cancelled()
            if not result.is_error:
                await bus.publish(ToolCallFinishedEvent(
                    run_id=run_id, tool_use_id=tool_call.id, tool_name=tool_call.name,
                    call_id=call_id, attempt_id=attempt_id, attempt=attempt,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    outcome=result.outcome, cleanup_confirmed=result.cleanup_confirmed,
                    output=result.content, ts=_now(),
                ))
                check_cancelled()
                return result
            await failed(result)
            if not retry:
                return result
            check_cancelled()
            attempt_id = ""
            await asyncio.sleep(_RETRY_BASE_S * (2 ** (attempt - 1)))
    except asyncio.CancelledError as exc:
        result = exc.result if isinstance(exc, ToolCancelledError) else ToolResult(
            "Tool execution cancelled; external effects may require review.", True, "cancelled",
            outcome=(last_result.outcome if not active and last_result is not None else
                     "not_started" if not active else
                     "known" if tool is not None and tool.effect == "read_only" else "unknown"),
            cleanup_confirmed=(last_result.cleanup_confirmed if not active and
                               last_result is not None else None),
        )
        if active:
            record(result)
        persist(result)
        await failed(result)
        raise ToolCancelledError(attach(result)) from exc
    raise AssertionError("retry loop exhausted without returning")
