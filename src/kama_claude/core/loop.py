from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from kama_claude.core.bus.events import StepFinishedEvent, StepStartedEvent
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.session.execution import StorageError
from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.errors import ToolCancelledError
from kama_claude.core.tools.invocation import check_cancelled, invoke_tool
from kama_claude.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from kama_claude.core.compact.compactor import Compactor
    from kama_claude.core.permissions.manager import PermissionManager
    from kama_claude.core.session.execution import ExecutionStore


log = logging.getLogger(__name__)

# 返回当前事件的 UTC 时间
def _now() -> str:
    return datetime.now(UTC).isoformat()


class AgentLoop:
    # 初始化模型、工具、事件、权限及可选的压缩依赖
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        bus: EventBus,
        *,
        permission_manager: PermissionManager | None = None,
        compactor: Compactor | None = None,
        compact_threshold: float = 0.80,
        session_id: str = "",
        execution_store: ExecutionStore | None = None,
        tool_result_limit: int = 8_000,
        tool_result_keep: int = 4_000,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._bus = bus
        self._permission_manager = permission_manager
        self._compactor = compactor
        self._compact_threshold = compact_threshold
        self._session_id = session_id
        self._execution_store = execution_store
        # S9.2: tool_result 截断配置（limit=触发截断的字符数，keep=截断后保留前缀）
        self._tool_result_limit = tool_result_limit
        self._tool_result_keep = tool_result_keep

    # S9.2: 长 tool_result 截断——避免爆 context window
    def _truncate_tool_result(self, content: str) -> str:
        if (self._tool_result_limit <= 0
                or len(content) <= self._tool_result_limit):
            return content
        kept = content[:self._tool_result_keep]
        tail = content[-self._tool_result_keep // 2:] if self._tool_result_keep > 0 else ""
        return (
            f"{kept}\n... (truncated, full length {len(content)} chars) ...\n{tail}"
        )

    # 只补齐持久化的未派发调用，已提交结果从历史复用，未知副作用始终暂停
    async def recover_pending(self, context: ExecutionContext) -> None:
        assert self._execution_store is not None
        for call in self._execution_store.list_calls(context.run_id):
            if call["status"] in {"succeeded", "failed", "cancelled"}:
                if call["result"] is None:
                    raise StorageError("terminal call has no committed result")
                continue
            if call["status"] not in {"planned", "ready"}:
                context.status, context.reason = "needs_review", "tool_outcome_unknown"
                return
            tc = ToolCallBlock(call["tool_use_id"], call["name"], call["input"])
            if not call.get("dispatch_allowed", True):
                result = ToolResult(
                    "Incomplete model output: this call was never dispatched.",
                    True, "incomplete_model_output", outcome="not_started", call_id=call["call_id"],
                )
                self._execution_store.commit_result(call["call_id"], result)
            else:
                result = await invoke_tool(
                    self._registry, tc, self._bus, context.run_id,
                    permission_manager=self._permission_manager, session_id=self._session_id,
                    call_id=call["call_id"], execution_store=self._execution_store, recovery=True,
                )
            # S9.2: 长 tool_result 截断
            context.add_tool_result(tc.id, self._truncate_tool_result(result.content), is_error=result.is_error)
            if result.outcome == "unknown":
                context.status, context.reason = "needs_review", "tool_outcome_unknown"
                return
        if context.step >= context.max_steps:
            context.mark_failed("exceeded_max_steps")

    # 驱动 plan→act→observe 循环直到上下文终止；CancelledError 向上传播
    async def run(self, context: ExecutionContext) -> None:
        while not context.is_done():
            check_cancelled()
            context.step += 1
            await self._bus.publish(
                StepStartedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

            # [plan] call LLM — API errors terminate the run
            try:
                response = await self._provider.chat(
                    messages=context.messages,
                    tool_schemas=self._registry.tool_schemas(),
                    bus=self._bus,
                    run_id=context.run_id,
                    step=context.step,
                    system=context.system_prompt(
                        "You are a helpful AI assistant. "
                        "Use the available tools to complete the user's goal. "
                        "When the goal is fully achieved, respond with a final answer "
                        "and do not call any more tools."
                    ),
                )
                check_cancelled()
            except asyncio.CancelledError:
                context.mark_failed("cancelled")
                raise
            except Exception:
                logging.getLogger(__name__).exception(
                    "LLM call failed run_id=%s step=%d", context.run_id, context.step
                )
                context.mark_failed("llm_error")
                break

            # [observe] append assistant content blocks to context
            # thinking blocks must come first and be preserved verbatim for extended thinking mode
            blocks: list[dict[str, object]] = list(response.thinking_blocks)
            if response.text:
                blocks.append({"type": "text", "text": response.text})
            for tc in response.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
                )
            call_ids = [uuid.uuid4().hex for _ in response.tool_calls]
            if self._execution_store is not None:
                calls = []
                for call_id, tc in zip(call_ids, response.tool_calls, strict=True):
                    tool = self._registry.get(tc.name)
                    calls.append({
                        "call_id": call_id, "tool_use_id": tc.id, "name": tc.name,
                        "input": tc.input, "effect": tool.effect if tool else "unknown",
                        "retry_safe": tool.retry_safe if tool else False,
                        "dispatch_allowed": response.stop_reason == "tool_use",
                    })
                self._execution_store.commit_response(
                    context.run_id, context.step, blocks, calls,
                    final=response.stop_reason == "end_turn" and not response.tool_calls,
                )
            context.add_assistant_message(blocks)

            # [act] execute each requested tool; errors become tool results so loop continues
            if response.stop_reason == "tool_use":
                for call_id, tc in zip(call_ids, response.tool_calls, strict=True):
                    try:
                        check_cancelled()
                        result = await invoke_tool(
                            self._registry, tc, self._bus, context.run_id,
                            permission_manager=self._permission_manager,
                            session_id=self._session_id,
                            call_id=call_id,
                            execution_store=self._execution_store,
                        )
                    except asyncio.CancelledError as exc:
                        if isinstance(exc, ToolCancelledError) and exc.result.outcome == "unknown":
                            context.status = "needs_review"
                            context.reason = "cancelled_tool_unknown"
                        else:
                            context.mark_failed("cancelled")
                        raise
                    # S9.2: 长 tool_result 截断
                    context.add_tool_result(tc.id, self._truncate_tool_result(result.content), is_error=result.is_error)
                    if result.outcome == "unknown":
                        context.status, context.reason = "needs_review", "tool_outcome_unknown"
                        return
            elif response.tool_calls:
                # Output token limit hit mid-tool-call; input is incomplete.
                # Add synthetic error results so the conversation stays balanced.
                for call_id, tc in zip(call_ids, response.tool_calls, strict=True):
                    content = (
                        "Error: output token limit or invalid stop reason prevented this tool "
                        "call from being executed. Please break the task into smaller steps."
                    )
                    if self._execution_store is not None:
                        self._execution_store.commit_result(call_id, ToolResult(
                            content, True, "incomplete_model_output", outcome="not_started",
                            call_id=call_id,
                        ))
                    context.add_tool_result(
                        tc.id, content,
                        is_error=True,
                    )

            # Termination check — end_turn wins over max_steps if both hit on same step
            if response.stop_reason == "end_turn":
                context.result = response.text or ""
                context.mark_success()
            elif context.step >= context.max_steps:
                context.mark_failed("exceeded_max_steps")

            # 工具结果追加完毕（messages 末尾为 user）后检查压缩，仅在 run 继续时触发
            # 此时压缩结果 [user_summary, assistant_ack] 对下一次 LLM 调用是合法输入
            if (
                not context.is_done()
                and response.stop_reason == "tool_use"
                and self._compactor is not None
                and self._compact_threshold > 0
                and response.usage is not None
                and response.usage.context_pct >= self._compact_threshold
            ):
                await self._compactor.compact(context, self._provider)

            await self._bus.publish(
                StepFinishedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )
