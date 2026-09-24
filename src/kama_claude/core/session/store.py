from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kama_claude.core.session.execution import ExecutionStore, StorageError
from kama_claude.core.session.model import Session

MessageContent = str | list[dict[str, Any]]


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionStore:
    # SQLite 为会话与执行状态唯一权威，原目录仅存诊断和笔记附件
    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self.execution = ExecutionStore(self._root / "execution.sqlite3")
        self._working_views: dict[str, tuple[int, list[dict[str, Any]]]] = {}

    # 返回指定 session 的目录路径
    def session_dir(self, sid: str) -> Path:
        if not sid or Path(sid).name != sid or sid in {".", ".."}:
            raise ValueError("invalid session id")
        return self._root / sid

    # 返回指定 session 下的诊断 runs 目录路径
    def runs_dir(self, sid: str) -> Path:
        return self.session_dir(sid) / "runs"

    # 提交元数据并保留已绑定工作区，不再写入旧 meta.json
    def write_meta(self, session: Session, *, workspace: str | None = None) -> None:
        old = self.execution.get_session(session.id)
        workspace = old["workspace"] if old is not None else workspace or str(Path.cwd().resolve())
        self.execution.put_session(session.to_dict(), workspace=workspace)

    # 读取 SQLite 元数据；旧会话需先显式执行无损导入
    def read_meta(self, sid: str) -> Session:
        data = self.execution.get_session(sid)
        if data is None:
            raise FileNotFoundError(sid)
        return Session.from_dict(data)

    # 读取持久绑定的工作区；未绑定的旧会话禁止直接执行
    def workspace(self, sid: str) -> Path:
        data = self.execution.get_session(sid)
        if data is None or not data.get("workspace"):
            raise StorageError("session workspace is unbound")
        workspace = Path(data["workspace"])
        if not workspace.is_absolute() or not workspace.is_dir():
            raise StorageError("session workspace is unavailable")
        return workspace

    # 兼容调用方追加原始消息，旧 run 标识仅作兼容来源而不虚构执行状态
    def append_message(
        self, sid: str, role: str, content: MessageContent, run_id: str | None = None,
    ) -> None:
        if self.execution.get_session(sid) is None:
            self.write_meta(Session(sid, "chat", "active", "", _now(), _now()))
        linked_run = run_id if run_id and self.execution.get_run(run_id) is not None else None
        self.execution.append_message(sid, role, content, run_id=linked_run, source="compat")

    # 兼容旧测试与导出调用方；运行链路使用专门的原子提交方法
    def append_messages(self, sid: str, messages: list[dict[str, Any]], run_id: str) -> None:
        for msg in messages:
            self.append_message(sid, str(msg["role"]), msg["content"], run_id)

    # 返回不可变原始历史的完整内容，不裁剪孤儿调用或工具输出
    def read_raw_messages(self, sid: str) -> list[dict[str, Any]]:
        return [{"role": row["role"], "content": row["content"]}
                for row in self.execution.messages(sid)]

    # 构建可压缩模型投影；未配对调用必须阻止推进而不能隐藏
    def read_messages(
        self, sid: str, *, pending_tool_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.execution.messages(sid)
        messages = [{"role": row["role"], "content": row["content"]} for row in rows]
        self._assert_balanced(messages, pending_tool_ids=pending_tool_ids)
        summary = self.execution.current_summary(sid)
        if summary is not None:
            messages = self._summary_messages(summary["summary_text"]) + [
                {"role": row["role"], "content": row["content"]}
                for row in rows if row["seq"] > summary["to_seq"]
            ]
            self._assert_balanced(messages, pending_tool_ids=pending_tool_ids)
        elif sid in self._working_views and pending_tool_ids is None:
            boundary, view = self._working_views[sid]
            messages = copy.deepcopy(view) + [
                {"role": row["role"], "content": row["content"]}
                for row in rows if row["seq"] > boundary
            ]
        merged: list[dict[str, Any]] = []
        for msg in messages:
            content = msg["content"]
            if (merged and msg["role"] == merged[-1]["role"] == "user"
                    and isinstance(content, list) and isinstance(merged[-1]["content"], list)
                    and all(b.get("type") == "tool_result"
                            for b in content + merged[-1]["content"])):
                merged[-1]["content"].extend(content)
            else:
                merged.append(copy.deepcopy(msg))
        from kama_claude.core.compact.budget import truncate_tool_results
        return truncate_tool_results(merged)

    # 将持久摘要还原为模型可接受的 user/assistant 交接消息对
    def _summary_messages(self, summary_text: str) -> list[dict[str, Any]]:
        return [
            {"role": "user", "content": summary_text},
            {"role": "assistant", "content": "Understood, I'll continue from this summary."},
        ]

    # 捕获压缩所需的稳定消息边界和来源 checkpoint 版本
    def capture_compaction(self, sid: str, run_id: str) -> dict[str, Any]:
        snapshot = self.execution.capture_compaction(sid, run_id)
        rows = snapshot.pop("rows")
        raw = [{"role": row["role"], "content": row["content"]} for row in rows]
        self._assert_balanced(raw)
        summary = snapshot.pop("summary")
        messages = raw
        if summary is not None:
            messages = self._summary_messages(summary["summary_text"]) + [
                {"role": row["role"], "content": row["content"]}
                for row in rows if row["seq"] > summary["to_seq"]
            ]
            self._assert_balanced(messages)
        snapshot["messages"] = messages
        return snapshot

    # 通过执行存储原子提交摘要并清除旧进程内兼容投影
    def commit_compaction(
        self,
        sid: str,
        run_id: str,
        snapshot: dict[str, Any],
        summary_text: str,
        *,
        generation_config_version: str,
        generation_config: dict[str, Any],
        summary_kind: str = "handoff",
    ) -> dict[str, Any]:
        summary = self.execution.commit_summary(
            sid,
            run_id,
            source_checkpoint_version=int(snapshot["source_checkpoint_version"]),
            source_summary_ref=snapshot["source_summary_ref"],
            message_digest=str(snapshot["message_digest"]),
            from_seq=int(snapshot["from_seq"]),
            to_seq=int(snapshot["to_seq"]),
            summary_text=summary_text,
            generation_config_version=generation_config_version,
            generation_config=generation_config,
            summary_kind=summary_kind,
        )
        self._working_views.pop(sid, None)
        return summary

    # S9.1: 轻量写入人类可读摘要（summarize skill 完成后 hook）
    def write_skill_summary(
        self,
        sid: str,
        run_id: str,
        *,
        summary_text: str,
        generation_config: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return self.execution.write_skill_summary(
            sid, run_id, summary_text=summary_text, generation_config=generation_config,
        )

    # 校验调用与结果的顺序配对，保留中断历史给后续核查阶段
    def _assert_balanced(
        self, messages: list[dict[str, Any]], *, pending_tool_ids: set[str] | None = None,
    ) -> None:
        pending: set[str] = set()
        for msg in messages:
            content = msg["content"]
            if (msg["role"] not in {"assistant", "user"}
                    or not isinstance(content, (str, list))
                    or (isinstance(content, list)
                        and any(not isinstance(block, dict) for block in content))):
                raise StorageError("invalid historical message requires review")
            blocks = content if isinstance(content, list) else []
            if msg["role"] == "assistant":
                if pending:
                    raise StorageError("unresolved tool calls require review")
                for block in blocks:
                    if block.get("type") == "tool_use":
                        tool_id = str(block.get("id", ""))
                        if not tool_id or tool_id in pending:
                            raise StorageError("invalid or duplicate tool_use id")
                        pending.add(tool_id)
            else:
                if pending and (not blocks or any(
                    block.get("type") != "tool_result" for block in blocks
                )):
                    raise StorageError("unresolved tool calls require review")
                for block in blocks:
                    if block.get("type") == "tool_result":
                        tool_id = str(block.get("tool_use_id", ""))
                        if tool_id not in pending:
                            raise StorageError("orphan tool result requires review")
                        pending.remove(tool_id)
        if pending != (pending_tool_ids or set()):
            raise StorageError("unresolved tool calls require review")

    # 保留旧直接调用方的进程内投影；产品压缩入口使用持久 commit_compaction
    def write_compacted(self, sid: str, messages: list[dict[str, Any]]) -> None:
        rows = self.execution.messages(sid)
        self._assert_balanced(self.read_raw_messages(sid))
        self._working_views[sid] = (rows[-1]["seq"] if rows else 0, copy.deepcopy(messages))

    # 显式无损导入旧会话，保持旧目录和 JSONL 原文件不变
    def import_legacy(self, directory: Path) -> dict[str, Any]:
        return self.execution.import_legacy(directory)

    # 关闭 SQLite 连接供 daemon 清理或离线检查使用
    def close(self) -> None:
        self.execution.close()

    # 读取 notes.md 全文，文件不存在时返回空字符串
    def read_notes(self, sid: str) -> str:
        path = self.session_dir(sid) / "notes.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    # 将一条主动笔记追加到 notes.md
    def append_note(self, sid: str, content: str, run_id: str) -> None:
        path = self.session_dir(sid)
        path.mkdir(parents=True, exist_ok=True)
        with (path / "notes.md").open("a", encoding="utf-8") as f:
            f.write(f"## Note ({_now()}, {run_id})\n{content}\n\n")
