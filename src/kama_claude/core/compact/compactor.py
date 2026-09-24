from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kama_claude.core.bus.events import ContextCompactedEvent
from kama_claude.core.events.bus import EventBus

if TYPE_CHECKING:
    from kama_claude.core.context import ExecutionContext
    from kama_claude.core.llm.base import LLMProvider
    from kama_claude.core.session.store import SessionStore

logger = logging.getLogger(__name__)

SUMMARY_CONFIG_VERSION = "s8.4-v1"

_COMPACT_PROMPT = """\
You are compressing an agent conversation into a handoff summary.
Another LLM instance will continue this task from your summary alone — make it complete.

Structure your response with exactly these six sections:

## 1. Original Goal
One sentence describing what the user asked the agent to accomplish.

## 2. Completed Steps
Bullet list of what has been done. Be specific (file paths, commands run, decisions made).

## 3. Key Constraints & Discoveries
Facts learned during the run that affect future decisions \
(e.g., API limitations, file formats, user preferences stated mid-conversation).

## 4. Current File State
For each file that was created or modified: path, a one-line description of its current state.

## 5. Remaining TODOs
Ordered list of what still needs to be done to complete the original goal.

## 6. Critical Data
Any values the next LLM needs verbatim: IDs, tokens, exact error messages, config values \
discovered during the run.

Be concise. Omit reasoning steps and intermediate attempts. Keep conclusions.\
"""


# 返回当前 UTC 时间的简短时间戳字符串（用于文件名）
def _ts_compact() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class CompactionResult:
    summary_text: str
    original_token_estimate: int
    summary_tokens: int
    summary_id: str | None = None
    from_seq: int | None = None
    to_seq: int | None = None


class Compactor:
    # 初始化压缩器，绑定事件总线、session 目录和 session ID
    def __init__(
        self,
        bus: EventBus,
        session_dir: Path,
        session_id: str,
        store: SessionStore | None = None,
    ) -> None:
        self._bus = bus
        self._session_dir = session_dir
        self._session_id = session_id
        self._store = store

    # 压缩 ExecutionContext.messages，就地替换消息列表并写 summary 文件
    async def compact(
        self,
        context: ExecutionContext,
        provider: LLMProvider,
        focus: str = "",
    ) -> CompactionResult | None:
        if self._store is not None:
            return await self.compact_persisted(context.run_id, provider, focus, context=context)
        result = await self.compact_messages(context.messages, provider, focus=focus)
        if result is None:
            return None

        context.messages = [
            {"role": "user", "content": result.summary_text},
            {"role": "assistant", "content": "Understood, I'll continue from this summary."},
        ]
        self._write_summary(result.summary_text)
        await self._bus.publish(
            ContextCompactedEvent(
                session_id=self._session_id,
                run_id=context.run_id,
                original_tokens=result.original_token_estimate,
                summary_tokens=result.summary_tokens,
                ts=_now(),
            )
        )
        logger.info(
            "context compacted session=%s run=%s original≈%d summary=%d tokens",
            self._session_id, context.run_id,
            result.original_token_estimate, result.summary_tokens,
        )
        return result

    # 从稳定 SQLite 快照生成摘要，提交成功后才替换模型工作上下文
    async def compact_persisted(
        self,
        run_id: str,
        provider: LLMProvider,
        focus: str = "",
        *,
        context: ExecutionContext | None = None,
    ) -> CompactionResult | None:
        if self._store is None:
            raise RuntimeError("persistent compaction requires a session store")
        snapshot = self._store.capture_compaction(self._session_id, run_id)
        result = await self.compact_messages(snapshot["messages"], provider, focus=focus)
        if result is None:
            return None
        summary = self._store.commit_compaction(
            self._session_id,
            run_id,
            snapshot,
            result.summary_text,
            generation_config_version=SUMMARY_CONFIG_VERSION,
            generation_config={"prompt_version": SUMMARY_CONFIG_VERSION, "focus": focus},
        )
        result.summary_id = summary["summary_id"]
        result.from_seq = summary["from_seq"]
        result.to_seq = summary["to_seq"]
        if context is not None:
            context.messages = self._store.read_messages(self._session_id)
        self._write_summary(result.summary_text, result.summary_id)
        await self._publish_result(run_id, result)
        return result

    # 发布一次已生效压缩的事件和诊断日志
    async def _publish_result(self, run_id: str, result: CompactionResult) -> None:
        await self._bus.publish(
            ContextCompactedEvent(
                session_id=self._session_id,
                run_id=run_id,
                original_tokens=result.original_token_estimate,
                summary_tokens=result.summary_tokens,
                ts=_now(),
            )
        )
        logger.info(
            "context compacted session=%s run=%s summary=%s original≈%d summary_tokens=%d",
            self._session_id,
            run_id,
            result.summary_id,
            result.original_token_estimate,
            result.summary_tokens,
        )

    # 纯函数式压缩：接收消息列表，返回 CompactionResult；失败时返回 None
    async def compact_messages(
        self,
        messages: list[dict[str, Any]],
        provider: LLMProvider,
        focus: str = "",
    ) -> CompactionResult | None:
        from kama_claude.core.events.bus import EventBus as _Bus

        original_estimate = sum(
            len(str(m.get("content", ""))) for m in messages
        ) // 4  # 粗略 token 估算（字符数 / 4）

        history_text = _messages_to_text(messages)
        prompt = _COMPACT_PROMPT
        if focus.strip():
            prompt += f"\n\nIMPORTANT: Pay special attention to: {focus.strip()}"

        compress_request: list[dict[str, object]] = [
            {"role": "user", "content": f"{prompt}\n\n---\n\n{history_text}"}
        ]

        try:
            silent_bus = _Bus()
            response = await provider.chat(
                messages=compress_request,
                tool_schemas=[],
                bus=silent_bus,
                run_id="compact",
                step=0,
                system="You are a helpful assistant that summarizes conversations.",
            )
        except Exception:
            logger.exception("compactor: LLM call failed, skipping compaction")
            return None

        summary_text = response.text.strip()
        if not summary_text:
            logger.warning("compactor: LLM returned empty summary, skipping compaction")
            return None

        summary_tokens = response.usage.output_tokens if response.usage else len(summary_text) // 4

        return CompactionResult(
            summary_text=summary_text,
            original_token_estimate=original_estimate,
            summary_tokens=summary_tokens,
        )

    # 将摘要文本写入 session 目录的 summary_<ts>.md
    def _write_summary(self, text: str, summary_id: str | None = None) -> None:
        try:
            self._session_dir.mkdir(parents=True, exist_ok=True)
            suffix = summary_id or _ts_compact()
            path = self._session_dir / f"summary_{suffix}.md"
            path.write_text(text, encoding="utf-8")
        except Exception:
            logger.exception("compactor: failed to write summary file")


# 将消息列表序列化为可供 LLM 阅读的纯文本
def _messages_to_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(f"[{role}]\n{content}")
        elif isinstance(content, list):
            blocks: list[str] = []
            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    blocks.append(block.get("text", ""))
                elif btype == "tool_use":
                    blocks.append(
                        f"<tool_call name={block.get('name')} id={block.get('id')}>\n"
                        f"{block.get('input', {})}\n</tool_call>"
                    )
                elif btype == "tool_result":
                    blocks.append(
                        f"<tool_result id={block.get('tool_use_id')}>\n"
                        f"{block.get('content', '')}\n</tool_result>"
                    )
            parts.append(f"[{role}]\n" + "\n".join(blocks))
    return "\n\n".join(parts)
