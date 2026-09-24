from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kama_claude.core.session.execution import (
    ExecutionStore,
    StorageError,
    _dump,
    _new_id,
    _now,
)


@dataclass
class _LegacyLine:
    line_no: int
    raw: bytes
    parse_status: str
    error: str | None
    role: str | None
    content: Any = None
    message_id: str | None = None
    seq: int | None = None


# 读取旧 session 的原始元数据和 thread 字节快照
def _read_snapshot(directory: Path) -> tuple[bytes, bytes]:
    meta_path = directory / "meta.json"
    thread_path = directory / "thread.jsonl"
    if not directory.exists() or not directory.is_dir():
        raise StorageError(f"legacy directory does not exist: {directory}")
    if not meta_path.exists() and not thread_path.exists():
        raise StorageError("legacy directory has neither meta.json nor thread.jsonl")
    try:
        meta = meta_path.read_bytes() if meta_path.exists() else b""
        thread = thread_path.read_bytes() if thread_path.exists() else b""
    except OSError as exc:
        raise StorageError(f"cannot read legacy snapshot: {exc}") from exc
    return meta, thread


# 解析 meta.json 并保留无法解析时的目录身份
def _parse_meta(
    meta_bytes: bytes, directory: Path,
) -> tuple[dict[str, Any], str | None, str | None]:
    if not meta_bytes:
        return {"id": directory.name}, None, None
    try:
        value = json.loads(meta_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"id": directory.name}, None, f"invalid_meta: {exc}"
    if not isinstance(value, dict):
        return {"id": directory.name}, None, "invalid_meta: root is not an object"
    raw_sid = value.get("id", value.get("session_id", value.get("sid", directory.name)))
    sid = str(raw_sid) if raw_sid is not None and str(raw_sid) else directory.name
    payload = dict(value)
    payload["id"] = sid
    workspace = value.get("workspace")
    if workspace is None and "workspace_path" in value:
        workspace = value.get("workspace_path")
    if workspace is not None:
        workspace = str(workspace)
    return payload, workspace, None


# 将 thread 原始行解析为可映射记录而不丢弃坏行
def _parse_lines(thread_bytes: bytes) -> list[_LegacyLine]:
    records: list[_LegacyLine] = []
    for line_no, raw in enumerate(thread_bytes.splitlines(keepends=True), start=1):
        value = raw.rstrip(b"\r\n")
        if not value:
            records.append(_LegacyLine(line_no, raw, "blank", None, None))
            continue
        try:
            parsed = json.loads(value)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            records.append(_LegacyLine(line_no, raw, "invalid_json", str(exc), None))
            continue
        if not isinstance(parsed, dict):
            records.append(_LegacyLine(line_no, raw, "invalid_row", "row is not an object", None))
            continue
        role = parsed.get("role")
        if not isinstance(role, str) or role not in {"user", "assistant"}:
            records.append(
                _LegacyLine(line_no, raw, "unknown_role", f"unknown role: {role!r}", None)
            )
            continue
        records.append(
            _LegacyLine(line_no, raw, "mapped", None, str(role), parsed.get("content", ""))
        )
    return records


# 标出合法但未配对的 tool_use/tool_result 取证行
def _mark_orphans(records: list[_LegacyLine]) -> None:
    uses: dict[str, int] = {}
    results: dict[str, int] = {}
    for record in records:
        if record.parse_status != "mapped" or not isinstance(record.content, list):
            continue
        for block in record.content:
            if not isinstance(block, dict):
                continue
            if record.role == "assistant" and block.get("type") == "tool_use":
                key = str(block.get("id", ""))
                uses[key] = uses.get(key, 0) + 1
            if record.role == "user" and block.get("type") == "tool_result":
                key = str(block.get("tool_use_id", ""))
                results[key] = results.get(key, 0) + 1
    for record in records:
        if record.parse_status != "mapped" or not isinstance(record.content, list):
            continue
        ids: set[str] = set()
        if record.role == "assistant":
            ids = {
                str(block.get("id", ""))
                for block in record.content
                if isinstance(block, dict) and block.get("type") == "tool_use"
            }
            if any(not item or uses.get(item, 0) != results.get(item, 0) for item in ids):
                record.parse_status = "orphan"
        elif record.role == "user":
            ids = {
                str(block.get("tool_use_id", ""))
                for block in record.content
                if isinstance(block, dict) and block.get("type") == "tool_result"
            }
            if any(not item or results.get(item, 0) != uses.get(item, 0) for item in ids):
                record.parse_status = "orphan"


# 计算快照中包含的完整行数量
def _line_count(raw: bytes) -> int:
    return len(raw.splitlines(keepends=True))


# 生成不与已有 session 冲突的 legacy session id
def _isolated_session_id(store: ExecutionStore, base: str, digest: str) -> str:
    candidate = f"{base}-legacy-{digest[:12]}"
    index = 1
    while store.get_session(candidate) is not None:
        candidate = f"{base}-legacy-{digest[:12]}-{index}"
        index += 1
    return candidate


# 将 legacy import 记录写入当前事务
def _insert_import_tx(
    store: ExecutionStore,
    *,
    import_id: str,
    session_id: str,
    source_path: str,
    meta_hash: str,
    thread_hash: str,
    meta_bytes: bytes,
    thread_bytes: bytes,
    status: str,
    conflict: str | None,
) -> None:
    store._conn.execute(
        """
        INSERT INTO legacy_imports(
            import_id, session_id, source_path, meta_hash, thread_hash, meta_bytes,
            thread_bytes, status, conflict, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            import_id,
            session_id,
            source_path,
            meta_hash,
            thread_hash,
            meta_bytes,
            thread_bytes,
            status,
            conflict,
            _now(),
        ),
    )


# 在当前事务中写入逐行原始 bytes 和稳定消息映射
def _insert_rows_tx(
    store: ExecutionStore,
    *,
    import_id: str,
    session_id: str,
    records: list[_LegacyLine],
    prior_import_id: str | None,
    prior_line_count: int,
) -> None:
    for record in records:
        if prior_import_id is not None and record.line_no <= prior_line_count:
            previous = store._conn.execute(
                "SELECT message_id, seq, parse_status, error, role FROM legacy_rows "
                "WHERE import_id=? AND line_no=?",
                (prior_import_id, record.line_no),
            ).fetchone()
            if previous is None:
                raise StorageError("legacy prefix mapping is incomplete")
            record.message_id = previous["message_id"]
            record.seq = previous["seq"]
            record.parse_status = str(previous["parse_status"])
            record.error = previous["error"]
            record.role = previous["role"]
        elif record.parse_status in {"mapped", "orphan"}:
            message_id, seq = store._append_message_tx(
                session_id,
                record.role or "",
                record.content,
                None,
                "legacy",
            )
            record.message_id = message_id
            record.seq = seq
        store._conn.execute(
            """
            INSERT INTO legacy_rows(
                import_id, line_no, raw_bytes, parse_status, error, role, message_id, seq
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                import_id,
                record.line_no,
                record.raw,
                record.parse_status,
                record.error,
                record.role,
                record.message_id,
                record.seq,
            ),
        )


# 从旧 meta.json/thread.jsonl 无损导入 SQLite 并支持幂等追加
def import_legacy(
    directory: Path,
    store: ExecutionStore | None = None,
) -> dict[str, Any]:
    directory = Path(directory).expanduser().resolve()
    meta_bytes, thread_bytes = _read_snapshot(directory)
    payload, workspace, meta_error = _parse_meta(meta_bytes, directory)
    records = _parse_lines(thread_bytes)
    _mark_orphans(records)
    source_path = str(directory)
    meta_hash = hashlib.sha256(meta_bytes).hexdigest()
    thread_hash = hashlib.sha256(thread_bytes).hexdigest()
    owns_store = store is None
    if store is None:
        store = ExecutionStore(directory.parent / ".kama-execution.sqlite3")

    # 由调用方提供的 store 由调用方关闭，默认 store 在函数返回时关闭
    # 检查快照幂等性、前缀关系并完成同一事务导入
    def action() -> dict[str, Any]:
        assert store is not None
        exact = store._conn.execute(
            "SELECT * FROM legacy_imports WHERE source_path=? AND meta_hash=? AND thread_hash=?",
            (source_path, meta_hash, thread_hash),
        ).fetchone()
        if exact is not None:
            return {
                "session_id": str(exact["session_id"]),
                "import_id": str(exact["import_id"]),
                "status": "already_imported",
                "idempotent": True,
                "conflict": bool(exact["conflict"]),
            }

        prior = store._conn.execute(
            "SELECT * FROM legacy_imports WHERE source_path=? ORDER BY rowid DESC LIMIT 1",
            (source_path,),
        ).fetchone()
        append = False
        prior_import_id: str | None = None
        prior_line_count = 0
        target_sid = str(payload["id"])
        conflict: str | None = None
        status = "imported"
        if prior is not None:
            old_thread = bytes(prior["thread_bytes"])
            if (
                str(prior["meta_hash"]) == meta_hash
                and thread_bytes.startswith(old_thread)
                and (not old_thread or old_thread.endswith((b"\n", b"\r")))
            ):
                append = True
                prior_import_id = str(prior["import_id"])
                prior_line_count = _line_count(old_thread)
                target_sid = str(prior["session_id"])
                status = "appended"
            else:
                conflict = f"source snapshot changed after import {prior['import_id']}"
                target_sid = _isolated_session_id(store, target_sid, thread_hash)
                status = "conflict"
        elif store.get_session(target_sid) is not None:
            conflict = "legacy session id already exists"
            target_sid = _isolated_session_id(store, target_sid, thread_hash)
            status = "conflict"

        if not append:
            session_data = dict(payload)
            session_data["id"] = target_sid
            # 插入与 put_session 相同的最小 session 行，避免嵌套事务
            now = _now()
            store._conn.execute(
                """
                INSERT INTO sessions(
                    id, data_json, workspace, current_run_id, created_at, updated_at
                )
                VALUES (?, ?, NULLIF(?, ''), NULL, ?, ?)
                """,
                (target_sid, _dump(session_data), workspace or "", now, now),
            )
        import_id = _new_id()
        _insert_import_tx(
            store,
            import_id=import_id,
            session_id=target_sid,
            source_path=source_path,
            meta_hash=meta_hash,
            thread_hash=thread_hash,
            meta_bytes=meta_bytes,
            thread_bytes=thread_bytes,
            status=status,
            conflict=conflict,
        )
        _insert_rows_tx(
            store,
            import_id=import_id,
            session_id=target_sid,
            records=records,
            prior_import_id=prior_import_id,
            prior_line_count=prior_line_count,
        )
        return {
            "session_id": target_sid,
            "import_id": import_id,
            "status": status,
            "idempotent": False,
            "conflict": conflict is not None,
            "meta_error": meta_error,
        }

    try:
        return store._write(action)
    finally:
        if owns_store:
            store.close()


__all__ = ["import_legacy"]
