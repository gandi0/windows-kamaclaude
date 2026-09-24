from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

from kama_claude.core.session.workspace import (
    WorkspaceError,
    check_call_guard,
    make_call_guard,
    refresh_call_guard,
    resolve_workspace,
    same_workspace_identity,
    workspace_identity,
)
from kama_claude.core.tools.base import ToolResult

SCHEMA_VERSION = 4
_ACTIVE_RUN_STATES = {"created", "running", "waiting_approval", "interrupted", "needs_review"}
_PENDING_CALL_STATES = {"planned", "waiting_approval", "ready", "dispatching"}
_CALL_TERMINAL_STATES = {"succeeded", "failed", "unknown", "cancelled"}
_RUN_TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
_T = TypeVar("_T")


class StorageError(Exception):
    """SQLite persistence or invariant failure."""


# 将 workspace 模块的校验异常统一转换为存储异常
def _workspace_error(exc: WorkspaceError) -> StorageError:
    return StorageError(str(exc))


# 返回当前 UTC 时间的稳定文本表示
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 将嵌套值转换为标准 JSON 可编码的结构
def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


# 将 SQLite 中的标准 JSON 结构恢复为调用方可用的值
def _from_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _from_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_from_jsonable(item) for item in value]
    return value


# 将结构编码为参数 hash 和 SQLite 文本共用的规范 JSON
def _dump(value: Any) -> str:
    try:
        return json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise StorageError(f"value is not JSON serializable: {exc}") from exc


# 解析 SQLite 中保存的规范 JSON
def _load(value: str | bytes | None) -> Any:
    if value is None:
        return None
    try:
        return _from_jsonable(json.loads(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StorageError("invalid JSON stored in execution database") from exc


# 根据完整参数生成不参与去重的完整性 hash
def _params_hash(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


# 将 ToolResult 和嵌套 attempt 转成持久化字典
def _result_dict(result: ToolResult) -> dict[str, Any]:
    raw = asdict(result)
    raw["attempts"] = [asdict(item) for item in result.attempts]
    return raw


# 生成一次新的内部稳定标识
def _new_id() -> str:
    return uuid.uuid4().hex


# 规范化并校验 session workspace 的绝对路径形式
def _workspace_value(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(resolve_workspace(str(value)))
    except WorkspaceError as exc:
        raise _workspace_error(exc) from exc


# 读取工作区身份并转换为事务层异常
def _workspace_identity(value: str | Path) -> dict[str, Any]:
    try:
        return workspace_identity(value)
    except WorkspaceError as exc:
        raise _workspace_error(exc) from exc


class ExecutionStore:
    """S8.2 执行事实的 SQLite 权威存储。"""

    # 初始化 WAL/FULL/FK SQLite 数据库并拒绝未知版本
    def __init__(self, path: Path) -> None:
        candidate = Path(path).expanduser()
        candidate_text = str(candidate)
        if not candidate.is_absolute():
            raise StorageError("execution database path must be absolute")
        if candidate_text.startswith(("\\\\", "//")):
            raise StorageError("execution database cannot use a UNC path")
        self.path = candidate.resolve()
        self._lock = threading.RLock()
        self._closed = False
        self.epoch: str | None = None
        self._before_commit: Callable[[], None] = lambda: None
        self._after_summary_insert: Callable[[], None] = lambda: None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.path),
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            # 先检查版本，避免对未知新库执行 WAL 等可能写入的 PRAGMA
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise StorageError(
                    f"execution database schema {version} is newer than supported {SCHEMA_VERSION}"
                )
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            journal = str(self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            if journal != "wal":
                raise StorageError(f"SQLite WAL unavailable, got {journal!r}")
            self._conn.execute("PRAGMA synchronous=FULL")
            if version == 0:
                self._create_schema()
            elif version < SCHEMA_VERSION:
                self._migrate(version)
        except StorageError:
            self._close_after_init_failure()
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            self._close_after_init_failure()
            raise StorageError(f"cannot open execution database: {exc}") from exc

    # 关闭初始化失败后可能已经打开的 SQLite 连接
    def _close_after_init_failure(self) -> None:
        conn = getattr(self, "_conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._closed = True

    # 创建 S8.2 初始 schema
    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                data_json TEXT NOT NULL,
                workspace TEXT,
                workspace_identity_json TEXT,
                current_run_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                parent_run_id TEXT REFERENCES runs(run_id),
                workspace TEXT NOT NULL,
                workspace_identity_json TEXT,
                status TEXT NOT NULL,
                reason TEXT,
                request_id TEXT,
                request_content_json TEXT,
                owner_epoch TEXT,
                execution_config_json TEXT,
                child_goal TEXT,
                child_profile TEXT,
                child_background INTEGER NOT NULL DEFAULT 0,
                child_result_json TEXT,
                step INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_runs_request ON runs(session_id, request_id);
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                content_json TEXT NOT NULL,
                run_id TEXT,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(session_id, seq)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages(session_id, seq);
            CREATE TABLE IF NOT EXISTS tool_calls (
                call_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                step INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                tool_use_id TEXT NOT NULL,
                name TEXT NOT NULL,
                input_json TEXT NOT NULL,
                params_hash TEXT NOT NULL,
                effect TEXT NOT NULL,
                retry_safe INTEGER NOT NULL,
                dispatch_allowed INTEGER NOT NULL DEFAULT 1,
                workspace TEXT NOT NULL,
                guard_json TEXT,
                status TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_calls_run ON tool_calls(run_id, step, ordinal);
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id TEXT PRIMARY KEY,
                call_id TEXT NOT NULL REFERENCES tool_calls(call_id),
                attempt_no INTEGER NOT NULL,
                phase TEXT NOT NULL,
                outcome TEXT,
                error_type TEXT,
                cleanup_confirmed INTEGER,
                result_json TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                UNIQUE(call_id, attempt_no)
            );
            CREATE INDEX IF NOT EXISTS idx_attempts_call ON attempts(call_id, attempt_no);
            CREATE TABLE IF NOT EXISTS summaries (
                summary_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                version INTEGER NOT NULL,
                from_seq INTEGER NOT NULL,
                to_seq INTEGER NOT NULL,
                source_run_id TEXT NOT NULL REFERENCES runs(run_id),
                source_checkpoint_version INTEGER NOT NULL,
                summary_text TEXT NOT NULL,
                generation_config_version TEXT NOT NULL,
                generation_config_json TEXT NOT NULL,
                summary_kind TEXT NOT NULL DEFAULT 'handoff',
                created_at TEXT NOT NULL,
                UNIQUE(session_id, version),
                CHECK(from_seq >= 1 AND to_seq >= from_seq)
            );
            CREATE INDEX IF NOT EXISTS idx_summaries_session_version
                ON summaries(session_id, version);
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                version INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                message_seq INTEGER NOT NULL,
                step INTEGER NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                pending_json TEXT NOT NULL,
                next_action TEXT NOT NULL,
                summary_ref TEXT,
                summary_from INTEGER,
                summary_to INTEGER,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS legacy_imports (
                import_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                source_path TEXT NOT NULL,
                meta_hash TEXT NOT NULL,
                thread_hash TEXT NOT NULL,
                meta_bytes BLOB NOT NULL,
                thread_bytes BLOB NOT NULL,
                status TEXT NOT NULL,
                conflict TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(source_path, meta_hash, thread_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_legacy_source
                ON legacy_imports(source_path, thread_hash);
            CREATE TABLE IF NOT EXISTS legacy_rows (
                import_id TEXT NOT NULL REFERENCES legacy_imports(import_id),
                line_no INTEGER NOT NULL,
                raw_bytes BLOB NOT NULL,
                parse_status TEXT NOT NULL,
                error TEXT,
                role TEXT,
                message_id TEXT,
                seq INTEGER,
                PRIMARY KEY(import_id, line_no)
            );
            CREATE TABLE IF NOT EXISTS daemon_state (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reviews (
                review_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                action TEXT NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reviews_run ON reviews(run_id, created_at);
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                call_id TEXT NOT NULL REFERENCES tool_calls(call_id),
                epoch TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                status TEXT NOT NULL,
                decision TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                consumed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_approvals_call ON approvals(call_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, epoch);
            PRAGMA user_version=3;
            COMMIT;
            """
        )

    # 返回指定表的现有列名集合
    def _table_columns(self, table: str) -> set[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row[1]) for row in rows}

    # 在迁移事务内按需补充一列
    def _add_column(self, table: str, column: str, definition: str) -> None:
        if column not in self._table_columns(table):
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # 将旧 schema 逐版无损迁移到当前版本
    def _migrate(self, version: int) -> None:
        if version not in {1, 2, 3}:
            raise StorageError(f"unsupported execution database schema {version}")
        began = False
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            began = True
            if version == 1:
                self._add_column("sessions", "workspace_identity_json", "TEXT")
                self._add_column("runs", "workspace_identity_json", "TEXT")
                self._add_column("runs", "request_id", "TEXT")
                self._add_column("runs", "request_content_json", "TEXT")
                self._add_column("runs", "owner_epoch", "TEXT")
                self._add_column("runs", "execution_config_json", "TEXT")
                self._add_column("runs", "child_goal", "TEXT")
                self._add_column("runs", "child_profile", "TEXT")
                self._add_column("runs", "child_background", "INTEGER NOT NULL DEFAULT 0")
                self._add_column("runs", "child_result_json", "TEXT")
                self._add_column("tool_calls", "dispatch_allowed", "INTEGER NOT NULL DEFAULT 1")
                self._add_column("tool_calls", "guard_json", "TEXT")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_runs_request ON runs(session_id, request_id)"
                )
                for statement in (
                    "CREATE TABLE IF NOT EXISTS daemon_state ("
                    "key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS reviews ("
                    "review_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id), "
                    "action TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL)",
                    "CREATE INDEX IF NOT EXISTS idx_reviews_run ON reviews(run_id, created_at)",
                    "CREATE TABLE IF NOT EXISTS approvals ("
                    "approval_id TEXT PRIMARY KEY, "
                    "call_id TEXT NOT NULL REFERENCES tool_calls(call_id), "
                    "epoch TEXT NOT NULL, scope_json TEXT NOT NULL, status TEXT NOT NULL, "
                    "decision TEXT, created_at TEXT NOT NULL, resolved_at TEXT, consumed_at TEXT)",
                    "CREATE INDEX IF NOT EXISTS idx_approvals_call "
                    "ON approvals(call_id, created_at)",
                    "CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, epoch)",
                ):
                    self._conn.execute(statement)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS summaries ("
                "summary_id TEXT PRIMARY KEY, "
                "session_id TEXT NOT NULL REFERENCES sessions(id), "
                "version INTEGER NOT NULL, from_seq INTEGER NOT NULL, to_seq INTEGER NOT NULL, "
                "source_run_id TEXT NOT NULL REFERENCES runs(run_id), "
                "source_checkpoint_version INTEGER NOT NULL, summary_text TEXT NOT NULL, "
                "generation_config_version TEXT NOT NULL, generation_config_json TEXT NOT NULL, "
                "summary_kind TEXT NOT NULL DEFAULT 'handoff', "
                "created_at TEXT NOT NULL, UNIQUE(session_id, version), "
                "CHECK(from_seq >= 1 AND to_seq >= from_seq))"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_summaries_session_version "
                "ON summaries(session_id, version)"
            )
            # S9.1: v3 → v4 — summaries 表加 summary_kind 列
            # 无论从 v1/v2/v3 哪版进来都安全（_add_column 幂等）
            self._add_column("summaries", "summary_kind", "TEXT NOT NULL DEFAULT 'handoff'")
            self._before_commit()
            self._conn.execute("PRAGMA user_version=4")
            self._conn.commit()
        except StorageError:
            if began:
                self._rollback()
            raise
        except Exception as exc:
            if began:
                self._rollback()
            raise StorageError(f"schema migration failed: {exc}") from exc

    # 确认连接仍可用于读写
    def _ensure_open(self) -> None:
        if self._closed:
            raise StorageError("execution store is closed")

    # 在短事务中运行写操作并统一处理 rollback
    def _write(self, action: Callable[[], _T]) -> _T:
        with self._lock:
            self._ensure_open()
            began = False
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                began = True
                value = action()
                self._before_commit()
                self._conn.commit()
                return value
            except StorageError:
                if began:
                    self._rollback()
                raise
            except Exception as exc:
                if began:
                    self._rollback()
                raise StorageError(f"SQLite transaction failed: {exc}") from exc

    # 在锁内执行只读查询并包装 SQLite 错误
    def _read(self, action: Callable[[], _T]) -> _T:
        with self._lock:
            self._ensure_open()
            try:
                return action()
            except StorageError:
                raise
            except Exception as exc:
                raise StorageError(f"SQLite query failed: {exc}") from exc

    # 回滚当前未提交的 SQLite 事务
    def _rollback(self) -> None:
        try:
            self._conn.rollback()
        except sqlite3.Error:
            pass

    # 关闭数据库连接并释放文件句柄
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._conn.close()
            except sqlite3.Error as exc:
                raise StorageError(f"cannot close execution database: {exc}") from exc
            finally:
                self._closed = True

    # 校验 session 存在并返回其数据库行
    def _session_row(self, sid: str) -> sqlite3.Row:
        row = cast(
            sqlite3.Row | None,
            self._conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone(),
        )
        if row is None:
            raise StorageError(f"unknown session: {sid}")
        return row

    # 校验 run 存在并返回其数据库行
    def _run_row(self, run_id: str) -> sqlite3.Row:
        row = cast(
            sqlite3.Row | None,
            self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone(),
        )
        if row is None:
            raise StorageError(f"unknown run: {run_id}")
        return row

    # 校验 call 存在并返回其数据库行
    def _call_row(self, call_id: str) -> sqlite3.Row:
        row = cast(
            sqlite3.Row | None,
            self._conn.execute("SELECT * FROM tool_calls WHERE call_id=?", (call_id,)).fetchone(),
        )
        if row is None:
            raise StorageError(f"unknown call: {call_id}")
        return row

    # 返回 session 当前最大的稳定消息序号
    def _message_max_seq(self, sid: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS value FROM messages WHERE session_id=?", (sid,)
        ).fetchone()
        return int(row["value"])

    # 在当前事务中追加一条原始消息并返回 id/seq
    def _append_message_tx(
        self,
        sid: str,
        role: str,
        content: Any,
        run_id: str | None,
        source: str,
        *,
        message_id: str | None = None,
        seq: int | None = None,
    ) -> tuple[str, int]:
        self._session_row(sid)
        if not role or not source:
            raise StorageError("message role and source must be non-empty")
        actual_seq = self._message_max_seq(sid) + 1 if seq is None else int(seq)
        if actual_seq < 1:
            raise StorageError("message seq must be positive")
        actual_id = message_id or _new_id()
        self._conn.execute(
            """
            INSERT INTO messages(
                id, session_id, seq, role, content_json, run_id, source, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (actual_id, sid, actual_seq, role, _dump(content), run_id, source, _now()),
        )
        return actual_id, actual_seq

    # 返回运行中待处理调用的稳定 call_id 列表
    def _pending_call_ids(self, run_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT call_id FROM tool_calls WHERE run_id=? AND status IN (?, ?, ?, ?) "
            "ORDER BY step, ordinal, call_id",
            (run_id, *sorted(_PENDING_CALL_STATES)),
        ).fetchall()
        return [str(row["call_id"]) for row in rows]

    # 在事务中更新 run 检查点并保留摘要引用字段
    def _checkpoint_tx(
        self,
        run: sqlite3.Row,
        *,
        step: int | None = None,
        message_seq: int | None = None,
        status: str | None = None,
        reason: str | None = None,
    ) -> None:
        run_id = str(run["run_id"])
        old = self._conn.execute("SELECT * FROM checkpoints WHERE run_id=?", (run_id,)).fetchone()
        version = int(old["version"]) + 1 if old else 1
        current_message_seq = self._message_max_seq(str(run["session_id"]))
        if message_seq is not None:
            current_message_seq = max(current_message_seq, int(message_seq))
        if old is not None:
            current_message_seq = max(current_message_seq, int(old["message_seq"]))
        actual_status = status or str(run["status"])
        actual_reason = reason if reason is not None else (old["reason"] if old else run["reason"])
        actual_step = int(step if step is not None else (old["step"] if old else run["step"]))
        pending = self._pending_call_ids(run_id)
        if actual_status == "needs_review":
            next_action = "review"
        elif actual_status in _RUN_TERMINAL_STATES:
            next_action = "done"
        elif (
            self._conn.execute(
                "SELECT 1 FROM attempts a JOIN tool_calls c ON c.call_id=a.call_id "
                "WHERE c.run_id=? AND a.phase='dispatching' LIMIT 1",
                (run_id,),
            ).fetchone()
            is not None
        ):
            next_action = "await_result"
        elif pending:
            next_action = "dispatch"
        else:
            next_action = "model"
        self._conn.execute(
            """
            INSERT INTO checkpoints(
                run_id, version, seq, message_seq, step, status, reason, pending_json,
                next_action, summary_ref, summary_from, summary_to, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                version=excluded.version,
                seq=excluded.seq,
                message_seq=excluded.message_seq,
                step=excluded.step,
                status=excluded.status,
                reason=excluded.reason,
                pending_json=excluded.pending_json,
                next_action=excluded.next_action,
                updated_at=excluded.updated_at
            """,
            (
                run_id,
                version,
                current_message_seq,
                current_message_seq,
                actual_step,
                actual_status,
                actual_reason,
                _dump(pending),
                next_action,
                old["summary_ref"] if old else None,
                old["summary_from"] if old else None,
                old["summary_to"] if old else None,
                _now(),
            ),
        )

    # 将数据库 session 行还原为公开字典
    def _session_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        data = _load(row["data_json"])
        if not isinstance(data, dict):
            raise StorageError("stored session data is not an object")
        result = dict(data)
        result["id"] = str(row["id"])
        result["workspace"] = row["workspace"]
        identity = _load(row["workspace_identity_json"])
        result["workspace_identity"] = identity
        historical_run_ids = data.get("run_ids", [])
        if not isinstance(historical_run_ids, list):
            historical_run_ids = []
        child_rows = self._conn.execute(
            "SELECT run_id FROM runs WHERE session_id=? AND parent_run_id IS NOT NULL",
            (str(row["id"]),),
        ).fetchall()
        child_run_ids = {str(item["run_id"]) for item in child_rows}
        run_ids = [
            run_id for run_id in historical_run_ids if str(run_id) not in child_run_ids
        ]
        known_run_ids = {str(run_id) for run_id in run_ids}
        run_rows = self._conn.execute(
            "SELECT run_id FROM runs WHERE session_id=? AND parent_run_id IS NULL "
            "ORDER BY created_at, run_id",
            (str(row["id"]),),
        ).fetchall()
        for item in run_rows:
            run_id = str(item["run_id"])
            if run_id not in known_run_ids:
                run_ids.append(run_id)
                known_run_ids.add(run_id)
        result["run_ids"] = run_ids
        current = row["current_run_id"]
        if current is not None:
            main = self._conn.execute(
                "SELECT 1 FROM runs WHERE run_id=? AND session_id=? AND parent_run_id IS NULL",
                (str(current), str(row["id"])),
            ).fetchone()
            if main is not None:
                result["current_run_id"] = str(current)
                current_run = self._conn.execute(
                    "SELECT status FROM runs WHERE run_id=?", (str(current),)
                ).fetchone()
                if (
                    result.get("status") != "closed"
                    and current_run is not None
                    and str(current_run["status"]) in _RUN_TERMINAL_STATES
                ):
                    result["status"] = (
                        "closed" if result.get("mode") == "one_shot" else "waiting_for_input"
                    )
        return result

    # 将新 session 数据写入 SQLite 并单独持久化 workspace
    def put_session(self, data: dict[str, Any], workspace: str | None = None) -> None:
        if not isinstance(data, dict):
            raise StorageError("session data must be a dict")
        raw_sid = data.get("id", data.get("session_id", data.get("sid")))
        if raw_sid is None or not str(raw_sid):
            raise StorageError("session data requires id")
        sid = str(raw_sid)
        payload = dict(data)
        payload["id"] = sid
        actual_workspace = workspace if workspace is not None else payload.get("workspace")
        identity: dict[str, Any] | None = None
        if actual_workspace is not None:
            raw_workspace = str(actual_workspace)
            actual_workspace = _workspace_value(str(actual_workspace))
            if actual_workspace is None:
                raise StorageError("workspace must be an absolute path")
            payload["workspace"] = actual_workspace
            if Path(actual_workspace).exists():
                identity = _workspace_identity(raw_workspace)
            payload["workspace_identity"] = identity
        current_run = payload.get("current_run_id")
        created = _now()

        # 写入 session 元数据但不影响既有消息和运行
        # 校验 workspace 绑定不会被隐式改写
        def action() -> None:
            existing = self._conn.execute(
                "SELECT workspace FROM sessions WHERE id=?", (sid,)
            ).fetchone()
            if (
                existing is not None
                and existing["workspace"] is not None
                and actual_workspace is not None
                and str(existing["workspace"]) != actual_workspace
            ):
                raise StorageError("session workspace binding cannot be changed implicitly")
            self._conn.execute(
                """
                INSERT INTO sessions(
                    id, data_json, workspace, workspace_identity_json, current_run_id,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    data_json=excluded.data_json,
                    workspace=COALESCE(excluded.workspace, sessions.workspace),
                    workspace_identity_json=COALESCE(
                        sessions.workspace_identity_json, excluded.workspace_identity_json
                    ),
                    current_run_id=COALESCE(excluded.current_run_id, sessions.current_run_id),
                    updated_at=excluded.updated_at
                """,
                (
                    sid,
                    _dump(payload),
                    actual_workspace,
                    None if identity is None else _dump(identity),
                    current_run,
                    created,
                    created,
                ),
            )

        self._write(action)

    # 读取 session 元数据及独立 workspace 字段
    def get_session(self, sid: str) -> dict[str, Any] | None:
        # 查询 session 行并还原公开字段
        def action() -> dict[str, Any] | None:
            row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (str(sid),)).fetchone()
            return None if row is None else self._session_dict(row)

        return self._read(action)

    # 从 SQLite 权威记录重建全部 session 索引
    def list_sessions(self) -> list[dict[str, Any]]:
        # 查询全部 session 并由 runs 表还原 run_ids
        def action() -> list[dict[str, Any]]:
            rows = self._conn.execute("SELECT * FROM sessions ORDER BY created_at, id").fetchall()
            return [self._session_dict(row) for row in rows]

        return self._read(action)

    # 返回一个 session 的全部主 run 和子 run
    def list_runs(self, sid: str) -> list[dict[str, Any]]:
        # 按创建顺序读取 session 运行记录
        def action() -> list[dict[str, Any]]:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE session_id=? ORDER BY created_at, run_id", (str(sid),)
            ).fetchall()
            return [self._run_dict(row) for row in rows]

        return self._read(action)

    # 返回一个 run 的全部工具调用及审批状态
    def list_calls(self, run_id: str) -> list[dict[str, Any]]:
        # 按模型步骤、顺序和稳定 call_id 读取调用
        def action() -> list[dict[str, Any]]:
            rows = self._conn.execute(
                "SELECT * FROM tool_calls WHERE run_id=? ORDER BY step, ordinal, call_id",
                (str(run_id),),
            ).fetchall()
            calls = [self._call_dict(row) for row in rows]
            approval_rows = self._conn.execute(
                "SELECT * FROM approvals WHERE call_id IN "
                "(SELECT call_id FROM tool_calls WHERE run_id=?) ORDER BY created_at, approval_id",
                (str(run_id),),
            ).fetchall()
            approvals: dict[str, list[dict[str, Any]]] = {}
            for row in approval_rows:
                approvals.setdefault(str(row["call_id"]), []).append(self._approval_dict(row))
            for call in calls:
                call["approvals"] = approvals.get(str(call["call_id"]), [])
            return calls

        return self._read(action)

    # 追加一条稳定 seq 的原始消息
    def append_message(
        self,
        sid: str,
        role: str,
        content: Any,
        run_id: str | None = None,
        source: str = "native",
    ) -> int:
        # 在事务中分配下一个稳定消息序号
        def action() -> int:
            _, seq = self._append_message_tx(str(sid), role, content, run_id, source)
            return seq

        return int(self._write(action))

    # 返回 session 的完整消息序列，不裁剪孤儿工具块
    def messages(self, sid: str) -> list[dict[str, Any]]:
        # 读取消息并按稳定 seq 排序
        def action() -> list[dict[str, Any]]:
            rows = self._conn.execute(
                "SELECT id, seq, role, content_json, run_id, source FROM messages "
                "WHERE session_id=? ORDER BY seq",
                (str(sid),),
            ).fetchall()
            return [
                {
                    "id": str(row["id"]),
                    "seq": int(row["seq"]),
                    "role": str(row["role"]),
                    "content": _load(row["content_json"]),
                    "run_id": row["run_id"],
                    "source": str(row["source"]),
                }
                for row in rows
            ]

        return self._read(action)

    # 将摘要数据库行还原为公开字典并解码生成配置
    def _summary_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        config = _load(row["generation_config_json"])
        if not isinstance(config, dict):
            raise StorageError("stored summary generation config is not an object")
        return {
            "summary_id": str(row["summary_id"]),
            "session_id": str(row["session_id"]),
            "version": int(row["version"]),
            "from_seq": int(row["from_seq"]),
            "to_seq": int(row["to_seq"]),
            "source_run_id": str(row["source_run_id"]),
            "source_checkpoint_version": int(row["source_checkpoint_version"]),
            "summary_text": str(row["summary_text"]),
            "generation_config_version": str(row["generation_config_version"]),
            "generation_config": config,
            "created_at": str(row["created_at"]),
        }

    # 读取稳定摘要记录，不依赖 checkpoint 是否仍引用它
    def get_summary(self, summary_id: str) -> dict[str, Any] | None:
        # 按稳定摘要 ID 查询单条记录
        def action() -> dict[str, Any] | None:
            row = self._conn.execute(
                "SELECT * FROM summaries WHERE summary_id=?", (str(summary_id),)
            ).fetchone()
            return None if row is None else self._summary_dict(row)

        return self._read(action)

    # 返回 session 当前主 run 的有效摘要及其 checkpoint 引用
    def current_summary(self, sid: str) -> dict[str, Any] | None:
        # 联结当前 checkpoint 并校验引用范围
        def action() -> dict[str, Any] | None:
            row = self._conn.execute(
                "SELECT sm.* FROM sessions s "
                "JOIN checkpoints cp ON cp.run_id=s.current_run_id "
                "JOIN summaries sm ON sm.summary_id=cp.summary_ref "
                "WHERE s.id=? AND cp.summary_from=sm.from_seq AND cp.summary_to=sm.to_seq",
                (str(sid),),
            ).fetchone()
            return None if row is None else self._summary_dict(row)

        return self._read(action)

    # 列出 session 的全部摘要版本供诊断和验收查询
    def list_summaries(self, sid: str) -> list[dict[str, Any]]:
        # 按 session 内版本顺序读取摘要
        def action() -> list[dict[str, Any]]:
            rows = self._conn.execute(
                "SELECT * FROM summaries WHERE session_id=? ORDER BY version", (str(sid),)
            ).fetchall()
            return [self._summary_dict(row) for row in rows]

        return self._read(action)

    # 对摘要覆盖前缀计算包含稳定身份和内容的规范摘要
    def _messages_digest(self, rows: list[dict[str, Any]]) -> str:
        payload = [
            {
                "id": row["id"],
                "seq": row["seq"],
                "role": row["role"],
                "content": row["content"],
                "run_id": row["run_id"],
                "source": row["source"],
            }
            for row in rows
        ]
        return hashlib.sha256(_dump(payload).encode("utf-8")).hexdigest()

    # 用单条查询捕获当前 checkpoint、摘要引用和稳定原始消息快照
    def capture_compaction(self, sid: str, run_id: str) -> dict[str, Any]:
        # 在读取锁内构造不可变压缩候选快照
        def action() -> dict[str, Any]:
            rows = self._conn.execute(
                "SELECT m.id, m.seq, m.role, m.content_json, m.run_id, m.source, "
                "cp.version AS checkpoint_version, cp.summary_ref, cp.summary_from, cp.summary_to "
                "FROM sessions s JOIN checkpoints cp ON cp.run_id=s.current_run_id "
                "JOIN messages m ON m.session_id=s.id "
                "WHERE s.id=? AND s.current_run_id=? ORDER BY m.seq",
                (str(sid), str(run_id)),
            ).fetchall()
            if not rows:
                raise StorageError("compaction requires current committed messages")
            messages = [
                {
                    "id": str(row["id"]),
                    "seq": int(row["seq"]),
                    "role": str(row["role"]),
                    "content": _load(row["content_json"]),
                    "run_id": row["run_id"],
                    "source": str(row["source"]),
                }
                for row in rows
            ]
            summary_ref = rows[0]["summary_ref"]
            summary = None
            if summary_ref is not None:
                summary_row = self._conn.execute(
                    "SELECT * FROM summaries WHERE summary_id=?", (str(summary_ref),)
                ).fetchone()
                if summary_row is None:
                    raise StorageError("checkpoint references a missing summary")
                summary = self._summary_dict(summary_row)
                if (
                    int(rows[0]["summary_from"]) != summary["from_seq"]
                    or int(rows[0]["summary_to"]) != summary["to_seq"]
                ):
                    raise StorageError("checkpoint summary range does not match summary record")
            return {
                "rows": messages,
                "source_checkpoint_version": int(rows[0]["checkpoint_version"]),
                "source_summary_ref": summary_ref,
                "summary": summary,
                "from_seq": summary["from_seq"] if summary is not None else messages[0]["seq"],
                "to_seq": messages[-1]["seq"],
                "message_digest": self._messages_digest(messages),
            }

        return self._read(action)

    # 原子提交摘要并以 checkpoint 版本 CAS 切换有效指针
    def commit_summary(
        self,
        sid: str,
        run_id: str,
        *,
        source_checkpoint_version: int,
        source_summary_ref: str | None,
        message_digest: str,
        from_seq: int,
        to_seq: int,
        summary_text: str,
        generation_config_version: str,
        generation_config: dict[str, Any],
        summary_kind: str = "handoff",
    ) -> dict[str, Any]:
        if (
            not summary_text.strip()
            or not generation_config_version
            or not isinstance(generation_config, dict)
            or from_seq < 1
            or to_seq < from_seq
        ):
            raise StorageError("invalid summary payload or message range")

        # 在一个短事务内校验候选并切换有效摘要
        def action() -> dict[str, Any]:
            session = self._session_row(str(sid))
            if session["current_run_id"] != str(run_id):
                raise StorageError("compaction source run is no longer current")
            run = self._run_row(str(run_id))
            if str(run["session_id"]) != str(sid) or run["parent_run_id"] is not None:
                raise StorageError("compaction source must be the current main run")
            checkpoint = self._conn.execute(
                "SELECT * FROM checkpoints WHERE run_id=?", (str(run_id),)
            ).fetchone()
            if checkpoint is None or int(checkpoint["version"]) != source_checkpoint_version:
                raise StorageError("compaction checkpoint changed during summary generation")
            if checkpoint["summary_ref"] != source_summary_ref:
                raise StorageError("effective summary changed during summary generation")
            prefix_rows = self._conn.execute(
                "SELECT id, seq, role, content_json, run_id, source FROM messages "
                "WHERE session_id=? AND seq<=? ORDER BY seq", (str(sid), to_seq)
            ).fetchall()
            prefix = [
                {
                    "id": str(row["id"]), "seq": int(row["seq"]),
                    "role": str(row["role"]), "content": _load(row["content_json"]),
                    "run_id": row["run_id"], "source": str(row["source"]),
                }
                for row in prefix_rows
            ]
            if self._messages_digest(prefix) != message_digest:
                raise StorageError("compaction message prefix changed during summary generation")
            bounds = self._conn.execute(
                "SELECT MIN(seq) AS first_seq, MAX(seq) AS last_seq FROM messages "
                "WHERE session_id=?", (str(sid),)
            ).fetchone()
            if (
                bounds["first_seq"] is None
                or from_seq < int(bounds["first_seq"])
                or to_seq > int(bounds["last_seq"])
            ):
                raise StorageError("summary range is outside committed messages")
            old_ref = checkpoint["summary_ref"]
            if old_ref is not None:
                old = self._conn.execute(
                    "SELECT * FROM summaries WHERE summary_id=?", (old_ref,)
                ).fetchone()
                if old is None or int(old["from_seq"]) != from_seq or to_seq < int(old["to_seq"]):
                    raise StorageError("summary range does not extend the effective summary")
            version_row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS value FROM summaries WHERE session_id=?",
                (str(sid),),
            ).fetchone()
            summary_id = _new_id()
            summary_version = int(version_row["value"]) + 1
            created_at = _now()
            self._conn.execute(
                "INSERT INTO summaries(summary_id, session_id, version, from_seq, to_seq, "
                "source_run_id, source_checkpoint_version, summary_text, "
                "generation_config_version, generation_config_json, summary_kind, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    summary_id, str(sid), summary_version, from_seq, to_seq, str(run_id),
                    source_checkpoint_version, summary_text, generation_config_version,
                    _dump(generation_config), summary_kind, created_at,
                ),
            )
            self._after_summary_insert()
            current_message_seq = self._message_max_seq(str(sid))
            changed = self._conn.execute(
                "UPDATE checkpoints SET version=version+1, seq=MAX(seq, ?), "
                "message_seq=MAX(message_seq, ?), summary_ref=?, summary_from=?, "
                "summary_to=?, updated_at=? WHERE run_id=? AND version=?",
                (
                    current_message_seq, current_message_seq, summary_id, from_seq, to_seq,
                    created_at, str(run_id),
                    source_checkpoint_version,
                ),
            ).rowcount
            if changed != 1:
                raise StorageError("compaction checkpoint changed during summary commit")
            row = self._conn.execute(
                "SELECT * FROM summaries WHERE summary_id=?", (summary_id,)
            ).fetchone()
            assert row is not None
            return self._summary_dict(row)

        return self._write(action)

    # S9.1: 轻量写入人类可读摘要（summarize skill 完成后调用）
    # 不走 compactor 的 CAS 校验，直接 INSERT，kind='human_readable'
    def write_skill_summary(
        self,
        sid: str,
        run_id: str,
        *,
        summary_text: str,
        generation_config: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """在 summaries 表写入一条 kind='human_readable' 的记录。

        用于 summarize skill 执行完后持久化人类可读摘要。
        与 commit_summary 的区别：不走 checkpoint CAS 校验、不切换 summary_ref，
        纯存储用途，不影响模型恢复上下文。
        """
        if not summary_text.strip():
            return None
        cfg = generation_config or {"source": "skill", "name": "summarize"}

        def action() -> dict[str, Any]:
            # 找最新 summary 的 version + 1，或从消息 seq 推 from_seq/to_seq
            version_row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS value FROM summaries WHERE session_id=?",
                (str(sid),),
            ).fetchone()
            summary_version = int(version_row["value"]) + 1
            seq_row = self._conn.execute(
                "SELECT COALESCE(MIN(seq), 1) AS first_seq, "
                "COALESCE(MAX(seq), 1) AS last_seq FROM messages WHERE session_id=?",
                (str(sid),),
            ).fetchone()
            from_seq = int(seq_row["first_seq"] or 1)
            to_seq = int(seq_row["last_seq"] or from_seq)
            summary_id = _new_id()
            created_at = _now()
            self._conn.execute(
                "INSERT INTO summaries(summary_id, session_id, version, from_seq, to_seq, "
                "source_run_id, source_checkpoint_version, summary_text, "
                "generation_config_version, generation_config_json, summary_kind, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    summary_id, str(sid), summary_version, from_seq, to_seq, str(run_id),
                    0, summary_text, "s9-skill-v1",
                    _dump(cfg), "human_readable", created_at,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM summaries WHERE summary_id=?", (summary_id,)
            ).fetchone()
            assert row is not None
            return self._summary_dict(row)

        try:
            return self._write(action)
        except StorageError:
            logger.exception("execution: failed to persist skill summary")
            return None

    # 原子创建 run、可选 user 消息和初始 checkpoint
    def begin_run(
        self,
        sid: str,
        run_id: str | None = None,
        *,
        workspace: str,
        user_content: str | None = None,
        parent_run_id: str | None = None,
        request_id: str | None = None,
        owner_epoch: str | None = None,
        execution_config: dict[str, Any] | None = None,
    ) -> str:
        sid = str(sid)
        actual_run_id = str(run_id) if run_id is not None else _new_id()
        if not actual_run_id or not isinstance(workspace, str):
            raise StorageError("run_id and workspace are required")
        if request_id is not None and not str(request_id):
            raise StorageError("request_id must be non-empty when provided")
        if execution_config is not None and not isinstance(execution_config, dict):
            raise StorageError("execution_config must be a dict")
        actual_owner_epoch = owner_epoch if owner_epoch is not None else self.epoch

        # 原子占用 session 执行权并建立初始检查点
        # 校验绑定、插入 run 并追加可选用户消息
        def action() -> str:
            self._assert_epoch_available(actual_owner_epoch)
            session = self._session_row(sid)
            inherited_checkpoint = None
            if parent_run_id is None and session["current_run_id"] is not None:
                inherited_checkpoint = self._conn.execute(
                    "SELECT * FROM checkpoints WHERE run_id=?",
                    (str(session["current_run_id"]),),
                ).fetchone()
            if request_id is not None:
                duplicate = self._conn.execute(
                    "SELECT * FROM runs WHERE session_id=? AND request_id=? "
                    "ORDER BY created_at LIMIT 1",
                    (sid, str(request_id)),
                ).fetchone()
                if duplicate is not None:
                    stored = _load(duplicate["request_content_json"])
                    if stored != user_content:
                        raise StorageError("request_id was reused with different user content")
                    return str(duplicate["run_id"])
            session_data = _load(session["data_json"])
            if isinstance(session_data, dict) and session_data.get("status") == "closed":
                raise StorageError("session is closed")
            requested_workspace = _workspace_value(workspace)
            if requested_workspace is None or not Path(requested_workspace).exists():
                raise StorageError("run workspace must be an existing absolute path")
            bound_workspace = session["workspace"]
            if bound_workspace is None:
                raise StorageError("session has no bound workspace")
            if str(Path(str(bound_workspace)).resolve()) != requested_workspace:
                raise StorageError("run workspace does not match session binding")
            if self._conn.execute("SELECT 1 FROM runs WHERE run_id=?", (actual_run_id,)).fetchone():
                raise StorageError(f"run already exists: {actual_run_id}")
            active = self._conn.execute(
                "SELECT run_id, status FROM runs WHERE session_id=? AND parent_run_id IS NULL "
                "AND status IN (?, ?, ?, ?, ?) "
                "ORDER BY created_at DESC LIMIT 1",
                (sid, *sorted(_ACTIVE_RUN_STATES)),
            ).fetchone()
            if active is not None:
                raise StorageError(
                    f"session already has unfinished run {active['run_id']} ({active['status']})"
                )
            if parent_run_id is not None:
                parent = self._run_row(str(parent_run_id))
                if str(parent["session_id"]) != sid:
                    raise StorageError("parent run belongs to another session")
            now = _now()
            identity = _workspace_identity(workspace)
            identity_json = _dump(identity)
            session_identity = session["workspace_identity_json"]
            if session_identity is not None:
                expected_identity = _load(session_identity)
                if not isinstance(expected_identity, dict) or not same_workspace_identity(
                    expected_identity, identity
                ):
                    raise StorageError(
                        "session workspace identity does not match current workspace"
                    )
                source_path = expected_identity.get("source_path")
                if isinstance(source_path, str):
                    current_source_identity = _workspace_identity(source_path)
                    if not same_workspace_identity(expected_identity, current_source_identity):
                        raise StorageError("session workspace identity source changed")
                    identity = expected_identity
                    identity_json = _dump(identity)
            self._conn.execute(
                """
                INSERT INTO runs(
                    run_id, session_id, parent_run_id, workspace, workspace_identity_json,
                    status, reason, request_id, request_content_json, owner_epoch,
                    execution_config_json, child_goal, child_profile, child_background,
                    child_result_json, step, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'running', NULL, ?, ?, ?, ?, NULL, NULL, 0, NULL, 0, ?, ?)
                """,
                (
                    actual_run_id,
                    sid,
                    parent_run_id,
                    requested_workspace,
                    identity_json,
                    request_id,
                    _dump(user_content),
                    actual_owner_epoch,
                    None if execution_config is None else _dump(execution_config),
                    now,
                    now,
                ),
            )
            if user_content is not None:
                self._append_message_tx(sid, "user", user_content, actual_run_id, "native")
            run = self._run_row(actual_run_id)
            self._checkpoint_tx(run, step=0)
            if inherited_checkpoint is not None and inherited_checkpoint["summary_ref"] is not None:
                self._conn.execute(
                    "UPDATE checkpoints SET summary_ref=?, summary_from=?, summary_to=? "
                    "WHERE run_id=?",
                    (
                        inherited_checkpoint["summary_ref"],
                        inherited_checkpoint["summary_from"],
                        inherited_checkpoint["summary_to"],
                        actual_run_id,
                    ),
                )
            session_data = _load(session["data_json"])
            if not isinstance(session_data, dict):
                raise StorageError("stored session data is not an object")
            run_ids = session_data.get("run_ids")
            if not isinstance(run_ids, list):
                run_ids = []
            if parent_run_id is None and actual_run_id not in run_ids:
                run_ids.append(actual_run_id)
            session_data["run_ids"] = run_ids
            session_data["workspace_identity"] = identity
            if user_content is not None and not str(session_data.get("title", "")).strip():
                session_data["title"] = user_content[:40]
            if parent_run_id is None:
                self._conn.execute(
                    "UPDATE sessions SET data_json=?, workspace_identity_json=?, "
                    "current_run_id=?, updated_at=? WHERE id=?",
                    (_dump(session_data), identity_json, actual_run_id, now, sid),
                )
            else:
                self._conn.execute(
                    "UPDATE sessions SET data_json=?, workspace_identity_json=?, "
                    "updated_at=? WHERE id=?",
                    (_dump(session_data), identity_json, now, sid),
                )
            return actual_run_id

        return str(self._write(action))

    # 将 run 行还原为公开字典
    def _run_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        execution_config = _load(row["execution_config_json"])
        workspace_identity_value = _load(row["workspace_identity_json"])
        child_result = _load(row["child_result_json"])
        return {
            "run_id": str(row["run_id"]),
            "id": str(row["run_id"]),
            "session_id": str(row["session_id"]),
            "parent_run_id": row["parent_run_id"],
            "workspace": str(row["workspace"]),
            "workspace_identity": workspace_identity_value,
            "status": str(row["status"]),
            "reason": row["reason"],
            "request_id": row["request_id"],
            "owner_epoch": row["owner_epoch"],
            "execution_config": execution_config,
            "goal": row["child_goal"],
            "profile": row["child_profile"],
            "background": bool(row["child_background"]),
            "result": child_result,
            "step": int(row["step"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # 校验当前连接是否仍拥有 run 的持久 epoch
    def _stored_daemon_epoch(self) -> str | None:
        row = self._conn.execute(
            "SELECT value_json FROM daemon_state WHERE key='daemon_epoch'"
        ).fetchone()
        if row is None:
            return None
        value = _load(row["value_json"])
        return None if value is None else str(value)

    # 校验当前连接可使用数据库中最新 daemon epoch
    def _assert_epoch_available(self, epoch: str | None) -> None:
        if self.epoch is not None and (epoch is None or str(epoch) != self.epoch):
            raise StorageError("daemon epoch is stale or missing")
        stored_epoch = self._stored_daemon_epoch()
        if stored_epoch is not None and (epoch is None or str(epoch) != stored_epoch):
            raise StorageError("daemon epoch is stale or missing")

    # 校验当前连接是否仍拥有 run 的持久 epoch
    def _check_owner_epoch(
        self,
        run: sqlite3.Row,
        requested_epoch: str | None = None,
    ) -> None:
        stored = run["owner_epoch"]
        expected = requested_epoch if requested_epoch is not None else self.epoch
        if (
            self.epoch is not None
            and requested_epoch is not None
            and str(requested_epoch) != self.epoch
        ):
            raise StorageError("daemon epoch is stale")
        daemon_epoch = self._stored_daemon_epoch()
        if daemon_epoch is not None and (expected is None or str(expected) != daemon_epoch):
            raise StorageError("daemon epoch is stale or missing")
        if stored is not None and expected is not None and str(stored) != str(expected):
            raise StorageError("run is owned by another daemon epoch")

    # 读取单个 run 的持久状态
    def get_run(self, run_id: str) -> dict[str, Any] | None:
        # 查询 run 行并还原状态字典
        def action() -> dict[str, Any] | None:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (str(run_id),)).fetchone()
            return None if row is None else self._run_dict(row)

        return self._read(action)

    # 原子提交 assistant 消息、全部调用意图和检查点
    def commit_response(
        self,
        run_id: str,
        step: int,
        blocks: list[dict[str, Any]],
        calls: list[dict[str, Any]],
        *,
        final: bool = False,
        epoch: str | None = None,
    ) -> None:
        if not isinstance(step, int) or step < 0:
            raise StorageError("step must be a non-negative integer")
        if not isinstance(blocks, list) or not isinstance(calls, list):
            raise StorageError("blocks and calls must be lists")

        # 在一次事务中提交模型原文、工具意图和最终状态
        # 校验响应边界后写入 assistant、calls 与 checkpoint
        def action() -> None:
            run = self._run_row(str(run_id))
            self._check_owner_epoch(run, epoch)
            if str(run["status"]) in _RUN_TERMINAL_STATES:
                raise StorageError("cannot append response to terminal run")
            if str(run["status"]) == "needs_review":
                raise StorageError("cannot append response while run needs review")
            existing_pending = self._conn.execute(
                "SELECT call_id, status FROM tool_calls "
                "WHERE run_id=? AND status IN (?, ?, ?, ?, ?) "
                "ORDER BY step, ordinal LIMIT 1",
                (run_id, *sorted(_PENDING_CALL_STATES | {"unknown"})),
            ).fetchone()
            if existing_pending is not None:
                raise StorageError(
                    f"run has unresolved call {existing_pending['call_id']} "
                    f"({existing_pending['status']})"
                )
            if final and calls:
                raise StorageError("final response cannot contain tool calls")
            if step != int(run["step"]) + 1:
                raise StorageError(f"response step must be {int(run['step']) + 1}, got {step}")
            tool_blocks: dict[str, dict[str, Any]] = {}
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_use_id = str(block.get("id", ""))
                if not tool_use_id or tool_use_id in tool_blocks:
                    raise StorageError("tool_use ids must be non-empty and unique")
                tool_blocks[tool_use_id] = block
            call_tool_ids: set[str] = set()
            for call in calls:
                if not isinstance(call, dict):
                    raise StorageError("each call intent must be a dict")
                tool_use_id = str(call.get("tool_use_id", ""))
                if not tool_use_id or tool_use_id in call_tool_ids:
                    raise StorageError("call tool_use_id must be non-empty and unique")
                call_tool_ids.add(tool_use_id)
                matched_block = tool_blocks.get(tool_use_id)
                if matched_block is None:
                    raise StorageError("call intent has no matching tool_use block")
                if str(call.get("name", "")) != str(matched_block.get("name", "")):
                    raise StorageError("call intent name does not match tool_use")
                if call.get("input") != matched_block.get("input"):
                    raise StorageError("call intent input does not match tool_use")
            if call_tool_ids != set(tool_blocks):
                raise StorageError("tool_use blocks and call intents do not match")
            self._append_message_tx(
                str(run["session_id"]), "assistant", blocks, str(run_id), "native"
            )
            seen: set[str] = set()
            for ordinal, call in enumerate(calls):
                if not isinstance(call, dict):
                    raise StorageError("each call intent must be a dict")
                required = ("call_id", "tool_use_id", "name", "input", "effect", "retry_safe")
                if any(key not in call for key in required):
                    raise StorageError("call intent misses a required field")
                call_id = str(call["call_id"])
                if not call_id or call_id in seen:
                    raise StorageError("call_id must be unique in a response")
                seen.add(call_id)
                if self._conn.execute(
                    "SELECT 1 FROM tool_calls WHERE call_id=?", (call_id,)
                ).fetchone():
                    raise StorageError(f"call already exists: {call_id}")
                if self._conn.execute(
                    "SELECT 1 FROM tool_calls WHERE run_id=? AND tool_use_id=?",
                    (run_id, str(call["tool_use_id"])),
                ).fetchone():
                    raise StorageError(f"tool_use_id already exists in run: {call['tool_use_id']}")
                input_value = call["input"]
                if not isinstance(input_value, dict):
                    raise StorageError("call input must be a dict")
                try:
                    guard = make_call_guard(
                        str(run["workspace"]),
                        str(call["name"]),
                        input_value,
                        call.get("guard", call.get("workspace_guard")),
                    )
                except WorkspaceError as exc:
                    raise _workspace_error(exc) from exc
                now = _now()
                self._conn.execute(
                    """
                    INSERT INTO tool_calls(
                        call_id, session_id, run_id, step, ordinal, tool_use_id, name, input_json,
                        params_hash, effect, retry_safe, dispatch_allowed, workspace, guard_json,
                        status, result_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', NULL, ?, ?)
                    """,
                    (
                        call_id,
                        run["session_id"],
                        run_id,
                        step,
                        ordinal,
                        str(call["tool_use_id"]),
                        str(call["name"]),
                        _dump(input_value),
                        _params_hash(input_value),
                        str(call["effect"]),
                        1 if bool(call["retry_safe"]) else 0,
                        1 if bool(call.get("dispatch_allowed", True)) else 0,
                        str(run["workspace"]),
                        _dump(guard),
                        now,
                        now,
                    ),
                )
            now = _now()
            self._conn.execute(
                "UPDATE runs SET step=?, status=?, updated_at=? WHERE run_id=?",
                (step, "succeeded" if final else "running", now, run_id),
            )
            updated = self._run_row(str(run_id))
            self._checkpoint_tx(updated, step=step)

        self._write(action)

    # 将 call 行还原为公开字典并解码完整结果
    def _call_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "call_id": str(row["call_id"]),
            "session_id": str(row["session_id"]),
            "run_id": str(row["run_id"]),
            "step": int(row["step"]),
            "ordinal": int(row["ordinal"]),
            "tool_use_id": str(row["tool_use_id"]),
            "name": str(row["name"]),
            "input": _load(row["input_json"]),
            "params_hash": str(row["params_hash"]),
            "input_hash": str(row["params_hash"]),
            "effect": str(row["effect"]),
            "retry_safe": bool(row["retry_safe"]),
            "dispatch_allowed": bool(row["dispatch_allowed"]),
            "workspace": str(row["workspace"]),
            "guard": _load(row["guard_json"]),
            "status": str(row["status"]),
            "result": _load(row["result_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # 将已提交调用的文件后置状态传播到同一批后续调用
    def _propagate_guard_tx(
        self,
        run_id: str,
        call: sqlite3.Row,
        refreshed_guard: dict[str, Any] | None,
    ) -> None:
        if refreshed_guard is None:
            return
        refreshed_files = refreshed_guard.get("files", [])
        if not isinstance(refreshed_files, list):
            return
        latest: dict[str, dict[str, Any]] = {}
        for item in refreshed_files:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                source = str(item.get("source_path", item["path"]))
                latest[source] = item
        if not latest:
            return
        later_rows = self._conn.execute(
            "SELECT * FROM tool_calls WHERE run_id=? AND "
            "(step>? OR (step=? AND ordinal>?)) ORDER BY step, ordinal, call_id",
            (run_id, int(call["step"]), int(call["step"]), int(call["ordinal"])),
        ).fetchall()
        state_keys = ("path", "exists", "kind", "dev", "ino", "size", "mtime_ns", "sha256")
        for later in later_rows:
            guard = _load(later["guard_json"])
            if not isinstance(guard, dict) or not isinstance(guard.get("files"), list):
                continue
            changed = False
            for item in guard["files"]:
                if not isinstance(item, dict):
                    continue
                source = str(item.get("source_path", item.get("path", "")))
                replacement = latest.get(source)
                if replacement is None:
                    continue
                for key in state_keys:
                    if key in replacement and item.get(key) != replacement[key]:
                        item[key] = replacement[key]
                        changed = True
            if changed:
                self._conn.execute(
                    "UPDATE tool_calls SET guard_json=? WHERE call_id=?",
                    (_dump(guard), later["call_id"]),
                )

    # 读取一个调用意图及其最后持久化结果
    def get_call(self, call_id: str) -> dict[str, Any] | None:
        # 查询调用并解码最后结果
        def action() -> dict[str, Any] | None:
            row = self._conn.execute(
                "SELECT * FROM tool_calls WHERE call_id=?", (str(call_id),)
            ).fetchone()
            return None if row is None else self._call_dict(row)

        return self._read(action)

    # 原子记录 dispatching attempt 并推进调用状态
    def start_attempt(
        self,
        call_id: str,
        attempt_id: str,
        number: int,
        *,
        epoch: str | None = None,
    ) -> None:
        call_id = str(call_id)
        attempt_id = str(attempt_id)
        if not call_id or not attempt_id or not isinstance(number, int) or number < 1:
            raise StorageError("call_id, attempt_id and positive attempt number are required")

        conflict_reason: list[str] = []

        # 先持久化 dispatching 边界，再允许执行层调用工具
        # 校验归属、递增 attempt 号并更新调用检查点
        def action() -> None:
            call = self._call_row(call_id)
            status = str(call["status"])
            run = self._run_row(str(call["run_id"]))
            self._check_owner_epoch(run, epoch)
            if str(run["status"]) != "running":
                raise StorageError(f"run is not dispatchable: {run['status']}")
            if status in _CALL_TERMINAL_STATES:
                raise StorageError("cannot dispatch a terminal call")
            if status not in {"planned", "ready"}:
                raise StorageError(f"call is not dispatchable: {status}")
            if not bool(call["dispatch_allowed"]):
                raise StorageError("model output was incomplete; call must not be dispatched")
            try:
                guard_reason = check_call_guard(
                    _load(call["guard_json"]),
                    str(call["workspace"]),
                )
            except WorkspaceError as exc:
                guard_reason = str(exc)
            if guard_reason is not None:
                now = _now()
                reason = f"workspace_conflict:{guard_reason}"
                self._conn.execute(
                    "UPDATE runs SET status='needs_review', reason=?, updated_at=? WHERE run_id=?",
                    (reason, now, run["run_id"]),
                )
                updated = self._run_row(str(run["run_id"]))
                self._checkpoint_tx(
                    updated, step=int(updated["step"]), status="needs_review", reason=reason
                )
                conflict_reason.append(reason)
                return
            dispatching = self._conn.execute(
                "SELECT 1 FROM attempts WHERE call_id=? AND phase='dispatching'", (call_id,)
            ).fetchone()
            if dispatching is not None:
                raise StorageError("call already has a dispatching attempt")
            latest = self._conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) AS number FROM attempts WHERE call_id=?",
                (call_id,),
            ).fetchone()
            expected = int(latest["number"]) + 1
            if number != expected:
                raise StorageError(f"attempt number must be {expected}, got {number}")
            if self._conn.execute(
                "SELECT 1 FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone():
                raise StorageError(f"attempt already exists: {attempt_id}")
            now = _now()
            self._conn.execute(
                """
                INSERT INTO attempts(
                    attempt_id, call_id, attempt_no, phase, outcome, error_type,
                    cleanup_confirmed, result_json, started_at, finished_at
                ) VALUES (?, ?, ?, 'dispatching', NULL, NULL, NULL, NULL, ?, NULL)
                """,
                (attempt_id, call_id, number, now),
            )
            self._conn.execute(
                "UPDATE tool_calls SET status='dispatching', updated_at=? WHERE call_id=?",
                (now, call_id),
            )
            self._checkpoint_tx(run, step=int(call["step"]))

        self._write(action)
        if conflict_reason:
            raise StorageError(conflict_reason[0])

    # 将 ToolResult 写入 attempt/call 并按需追加模型 tool_result 消息
    def commit_result(
        self,
        call_id: str,
        result: ToolResult,
        *,
        retry: bool = False,
        epoch: str | None = None,
    ) -> None:
        if not isinstance(result, ToolResult):
            raise StorageError("result must be ToolResult")
        call_id = str(call_id)
        if not call_id or not result.call_id or str(result.call_id) != call_id:
            raise StorageError("ToolResult.call_id must match call_id")
        if not isinstance(result.attempts, list):
            raise StorageError("ToolResult.attempts must be a list")
        result_payload = _result_dict(result)

        # 结果、调用状态、模型消息和检查点必须在同一事务内落盘
        # 处理当前 attempt、重试或取消收尾
        def action() -> None:
            call = self._call_row(call_id)
            status = str(call["status"])
            run = self._run_row(str(call["run_id"]))
            self._check_owner_epoch(run, epoch)
            # 晚到取消或重复回调不能覆盖已提交的终态事实
            if status in _CALL_TERMINAL_STATES:
                return
            attempt = self._conn.execute(
                "SELECT * FROM attempts WHERE call_id=? AND phase='dispatching' "
                "ORDER BY attempt_no DESC LIMIT 1",
                (call_id,),
            ).fetchone()
            latest_completed = self._conn.execute(
                "SELECT * FROM attempts WHERE call_id=? AND phase='result_committed' "
                "ORDER BY attempt_no DESC LIMIT 1",
                (call_id,),
            ).fetchone()
            stored_attempts = self._conn.execute(
                "SELECT attempt_id, call_id, attempt_no FROM attempts WHERE call_id=?",
                (call_id,),
            ).fetchall()
            seen_attempts: set[str] = set()
            for item in result.attempts:
                attempt_id = str(getattr(item, "attempt_id", ""))
                attempt_no = getattr(item, "number", None)
                if not attempt_id or not isinstance(attempt_no, int) or attempt_id in seen_attempts:
                    raise StorageError("ToolResult contains an invalid attempt record")
                seen_attempts.add(attempt_id)
                matching = next(
                    (row for row in stored_attempts if str(row["attempt_id"]) == attempt_id), None
                )
                if matching is None or str(matching["call_id"]) != call_id:
                    raise StorageError("ToolResult attempt does not belong to call")
                if int(matching["attempt_no"]) != attempt_no:
                    raise StorageError("ToolResult attempt number does not match database")
            if {str(row["attempt_id"]) for row in stored_attempts} != seen_attempts:
                raise StorageError("ToolResult attempts do not match database history")
            if retry and attempt is None:
                raise StorageError("retry result requires a dispatching attempt")
            if retry and (
                not bool(call["retry_safe"])
                or not result.transient
                or result.outcome != "known"
                or not result.is_error
                or result.error_type not in {"runtime_error", "rate_limited"}
            ):
                raise StorageError("result is not eligible for retry")
            # retry 退避期间取消没有新 attempt，允许用取消结果终结 ready call，
            # 但保留前一个 attempt 的真实暂时失败结果不被覆盖。
            cancellation_after_retry = (
                not retry
                and attempt is None
                and latest_completed is not None
                and status == "ready"
                and result.error_type == "cancelled"
            )
            if attempt is None and not cancellation_after_retry and result.outcome != "not_started":
                raise StorageError("known/unknown result requires a dispatching attempt")
            now = _now()
            if attempt is not None:
                self._conn.execute(
                    """
                    UPDATE attempts SET phase='result_committed', outcome=?, error_type=?,
                        cleanup_confirmed=?, result_json=?, finished_at=? WHERE attempt_id=?
                    """,
                    (
                        result.outcome,
                        result.error_type,
                        None if result.cleanup_confirmed is None else int(result.cleanup_confirmed),
                        _dump(result_payload),
                        now,
                        attempt["attempt_id"],
                    ),
                )
            if retry:
                new_status = "ready"
            elif result.outcome == "unknown":
                new_status = "unknown"
            elif result.error_type == "cancelled":
                new_status = "cancelled"
            elif result.is_error or result.outcome == "not_started":
                new_status = "failed"
            else:
                new_status = "succeeded"
            self._conn.execute(
                "UPDATE tool_calls SET status=?, result_json=?, updated_at=? WHERE call_id=?",
                (new_status, _dump(result_payload), now, call_id),
            )
            if not retry and result.outcome != "unknown":
                try:
                    refreshed_guard = refresh_call_guard(_load(call["guard_json"]))
                except WorkspaceError as exc:
                    raise _workspace_error(exc) from exc
                self._conn.execute(
                    "UPDATE tool_calls SET guard_json=? WHERE call_id=?",
                    (None if refreshed_guard is None else _dump(refreshed_guard), call_id),
                )
                self._propagate_guard_tx(str(call["run_id"]), call, refreshed_guard)
            run_status = str(run["status"])
            reason = run["reason"]
            if not retry and result.outcome == "unknown":
                run_status = "needs_review"
                reason = reason or f"unknown result for call {call_id}"
                self._conn.execute(
                    "UPDATE runs SET status=?, reason=?, updated_at=? WHERE run_id=?",
                    (run_status, reason, now, run["run_id"]),
                )
                run = self._run_row(str(call["run_id"]))
            if not retry:
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": str(call["tool_use_id"]),
                    "content": result.content,
                }
                if result.is_error:
                    block["is_error"] = True
                self._append_message_tx(
                    str(call["session_id"]),
                    "user",
                    [block],
                    str(call["run_id"]),
                    "native",
                )
            self._checkpoint_tx(run, step=int(call["step"]))

        self._write(action)

    # 返回调用全部 attempt，按 attempt_no 稳定排序
    def get_attempts(self, call_id: str) -> list[dict[str, Any]]:
        # 查询并解码调用的全部 attempt
        def action() -> list[dict[str, Any]]:
            rows = self._conn.execute(
                "SELECT * FROM attempts WHERE call_id=? ORDER BY attempt_no", (str(call_id),)
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                result.append(
                    {
                        "attempt_id": str(row["attempt_id"]),
                        "call_id": str(row["call_id"]),
                        "number": int(row["attempt_no"]),
                        "attempt_no": int(row["attempt_no"]),
                        "phase": str(row["phase"]),
                        "outcome": row["outcome"],
                        "error_type": row["error_type"],
                        "cleanup_confirmed": (
                            None
                            if row["cleanup_confirmed"] is None
                            else bool(row["cleanup_confirmed"])
                        ),
                        "result": _load(row["result_json"]),
                        "started_at": row["started_at"],
                        "finished_at": row["finished_at"],
                    }
                )
            return result

        return self._read(action)

    # 原子收尾 run 状态并写入最终检查点
    def finish_run(
        self,
        run_id: str,
        status: str,
        reason: str | None = None,
        *,
        epoch: str | None = None,
    ) -> None:
        if status not in {"succeeded", "failed", "cancelled", "needs_review"}:
            raise StorageError(f"invalid terminal run status: {status}")

        # dispatching 未提交结果时不允许伪造安全终态
        # 校验未完成 attempt 后更新 run 与 checkpoint
        def action() -> None:
            run = self._run_row(str(run_id))
            self._check_owner_epoch(run, epoch)
            current = str(run["status"])
            if current in _RUN_TERMINAL_STATES:
                if current == status:
                    return
                raise StorageError("cannot overwrite terminal run")
            dispatching = self._conn.execute(
                """
                SELECT 1 FROM attempts a
                JOIN tool_calls c ON c.call_id=a.call_id
                WHERE c.run_id=? AND a.phase='dispatching'
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if dispatching is not None and status in _RUN_TERMINAL_STATES:
                raise StorageError("run has an attempt without a committed result")
            if status in _RUN_TERMINAL_STATES:
                if current == "needs_review":
                    raise StorageError(
                        "needs_review run requires explicit review before terminal state"
                    )
                unknown = self._conn.execute(
                    "SELECT 1 FROM tool_calls WHERE run_id=? AND status='unknown' LIMIT 1",
                    (run_id,),
                ).fetchone()
                if unknown is not None:
                    raise StorageError("run has an unknown call result")
                if status == "succeeded":
                    pending = self._conn.execute(
                        "SELECT 1 FROM tool_calls WHERE run_id=? "
                        "AND status IN (?, ?, ?, ?) LIMIT 1",
                        (run_id, *sorted(_PENDING_CALL_STATES)),
                    ).fetchone()
                    if pending is not None:
                        raise StorageError("run has an unresolved pending call")
            now = _now()
            self._conn.execute(
                "UPDATE runs SET status=?, reason=?, updated_at=? WHERE run_id=?",
                (status, reason, now, run_id),
            )
            updated = self._run_row(str(run_id))
            self._checkpoint_tx(updated, step=int(updated["step"]), status=status, reason=reason)

        self._write(action)

    # 在 daemon 启动时持久化 epoch 并扫描所有未完成运行
    def start_daemon(self, epoch: str) -> None:
        if not isinstance(epoch, str) or not epoch:
            raise StorageError("daemon epoch must be non-empty")

        # 记录 epoch、失效旧审批并把运行恢复为显式可审查状态
        def action() -> None:
            now = _now()
            self._conn.execute(
                "INSERT INTO daemon_state(key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
                "updated_at=excluded.updated_at",
                ("daemon_epoch", _dump(epoch), now),
            )
            self._conn.execute(
                "UPDATE approvals SET status='expired', resolved_at=? "
                "WHERE epoch<>? AND status IN ('pending', 'approved')",
                (now, epoch),
            )
            self._conn.execute(
                "UPDATE tool_calls SET status='ready', updated_at=? "
                "WHERE status='waiting_approval' AND EXISTS "
                "(SELECT 1 FROM attempts WHERE attempts.call_id=tool_calls.call_id "
                "AND attempts.phase='result_committed')",
                (now,),
            )
            self._conn.execute(
                "UPDATE tool_calls SET status='planned', updated_at=? "
                "WHERE status='waiting_approval' AND NOT EXISTS "
                "(SELECT 1 FROM attempts WHERE attempts.call_id=tool_calls.call_id)",
                (now,),
            )
            runs = self._conn.execute(
                "SELECT * FROM runs WHERE status IN (?, ?, ?, ?, ?)",
                tuple(sorted(_ACTIVE_RUN_STATES)),
            ).fetchall()
            for row in runs:
                run_id = str(row["run_id"])
                if row["parent_run_id"] is not None:
                    child_status = str(row["status"])
                    target_status = (
                        "needs_review" if child_status == "needs_review" else "interrupted"
                    )
                    reason = (
                        str(row["reason"])
                        if target_status == "needs_review" and row["reason"]
                        else "child_recovery_not_supported"
                    )
                    self._conn.execute(
                        "UPDATE runs SET status=?, reason=?, updated_at=? WHERE run_id=?",
                        (target_status, reason, now, run_id),
                    )
                    updated = self._run_row(run_id)
                    self._checkpoint_tx(
                        updated, step=int(updated["step"]), status=target_status, reason=reason
                    )
                    continue
                uncertain = self._conn.execute(
                    "SELECT c.call_id, c.status AS call_status "
                    "FROM tool_calls c LEFT JOIN attempts a "
                    "ON a.call_id=c.call_id AND a.phase='dispatching' "
                    "WHERE c.run_id=? AND (c.status IN ('dispatching', 'unknown') "
                    "OR a.call_id IS NOT NULL) "
                    "ORDER BY c.step, c.ordinal, c.call_id LIMIT 1",
                    (run_id,),
                ).fetchone()
                current_status = str(row["status"])
                if uncertain is not None:
                    if str(uncertain["call_status"]) == "dispatching":
                        self._conn.execute(
                            "UPDATE tool_calls SET status='unknown', updated_at=? WHERE call_id=?",
                            (now, uncertain["call_id"]),
                        )
                    reason = str(row["reason"] or f"unknown result for call {uncertain['call_id']}")
                    target_status = "needs_review"
                elif current_status == "needs_review":
                    reason = row["reason"] or "daemon_restart_requires_review"
                    target_status = current_status
                else:
                    reason = "daemon_restarted"
                    target_status = "interrupted"
                self._conn.execute(
                    "UPDATE runs SET status=?, reason=?, updated_at=? WHERE run_id=?",
                    (target_status, reason, now, run_id),
                )
                updated = self._run_row(run_id)
                self._checkpoint_tx(
                    updated, step=int(updated["step"]), status=target_status, reason=reason
                )

        self._write(action)
        self.epoch = epoch

    # 在事务内把 run 置为 needs_review 并保留审计原因
    def block_run(self, run_id: str, reason: str, *, epoch: str | None = None) -> None:
        if not isinstance(reason, str) or not reason:
            raise StorageError("block reason must be non-empty")

        # 更新 run 和 checkpoint 但不改写任何未知调用事实
        def action() -> None:
            run = self._run_row(str(run_id))
            self._check_owner_epoch(run, epoch)
            if str(run["status"]) in _RUN_TERMINAL_STATES:
                raise StorageError("cannot block terminal run")
            now = _now()
            self._conn.execute(
                "UPDATE runs SET status='needs_review', reason=?, updated_at=? WHERE run_id=?",
                (reason, now, run_id),
            )
            updated = self._run_row(str(run_id))
            self._checkpoint_tx(
                updated, step=int(updated["step"]), status="needs_review", reason=reason
            )

        self._write(action)

    # 校验 workspace、相关文件和未知调用后以 CAS 方式认领 run
    def claim_run(self, run_id: str, epoch: str, workspace: str) -> bool:
        if not isinstance(epoch, str) or not epoch:
            raise StorageError("daemon epoch must be non-empty")
        if self.epoch is not None and self.epoch != epoch:
            return False
        failure: list[str] = []

        # 仅在所有恢复前置条件满足时把 run 从中断状态切为 running
        def action() -> bool:
            stored_epoch = self._stored_daemon_epoch()
            if stored_epoch is not None and stored_epoch != epoch:
                return False
            run = self._run_row(str(run_id))
            status = str(run["status"])
            if status not in {"interrupted", "needs_review"}:
                return False
            unknown = self._conn.execute(
                "SELECT c.call_id FROM tool_calls c LEFT JOIN attempts a "
                "ON a.call_id=c.call_id AND a.phase='dispatching' "
                "WHERE c.run_id=? AND (c.status IN ('dispatching', 'unknown') "
                "OR a.call_id IS NOT NULL) "
                "ORDER BY c.step, c.ordinal, c.call_id LIMIT 1",
                (run_id,),
            ).fetchone()
            if unknown is not None:
                failure.append(f"unknown_call:{unknown['call_id']}")
            expected_identity = _load(run["workspace_identity_json"])
            if expected_identity is None:
                failure.append("legacy_identity_unverified")
            try:
                requested_workspace = _workspace_value(workspace)
            except StorageError as exc:
                requested_workspace = None
                failure.append(str(exc))
            if requested_workspace != str(run["workspace"]):
                failure.append("workspace_path_mismatch")
            try:
                actual_identity = _workspace_identity(requested_workspace or workspace)
            except StorageError as exc:
                failure.append(str(exc))
                actual_identity = None
            if actual_identity is not None and (
                not isinstance(expected_identity, dict)
                or not same_workspace_identity(expected_identity, actual_identity)
            ):
                failure.append("workspace_identity_changed")
            if isinstance(expected_identity, dict) and isinstance(
                expected_identity.get("source_path"), str
            ):
                try:
                    source_identity = _workspace_identity(expected_identity["source_path"])
                except StorageError:
                    source_identity = None
                if source_identity is None or not same_workspace_identity(
                    expected_identity, source_identity
                ):
                    failure.append("workspace_identity_changed")
            call_rows = self._conn.execute(
                "SELECT * FROM tool_calls WHERE run_id=? ORDER BY step, ordinal, call_id", (run_id,)
            ).fetchall()
            latest_files: dict[str, dict[str, Any]] = {}
            latest_guard: dict[str, Any] | None = None
            for call in call_rows:
                guard = _load(call["guard_json"])
                if not isinstance(guard, dict):
                    continue
                latest_guard = guard
                files = guard.get("files", [])
                if not isinstance(files, list):
                    failure.append("invalid_workspace_guard")
                    continue
                for item in files:
                    if isinstance(item, dict) and isinstance(item.get("path"), str):
                        source = str(item.get("source_path", item["path"]))
                        latest_files[source] = item
            if latest_guard is not None:
                latest_guard = dict(latest_guard)
                latest_guard["files"] = list(latest_files.values())
                try:
                    guard_reason = check_call_guard(latest_guard, str(run["workspace"]))
                except WorkspaceError as exc:
                    guard_reason = str(exc)
                if guard_reason is not None:
                    failure.append(guard_reason)
            if failure:
                reason = "recovery_blocked:" + ",".join(dict.fromkeys(failure))
                now = _now()
                self._conn.execute(
                    "UPDATE runs SET status='needs_review', reason=?, updated_at=? WHERE run_id=?",
                    (reason, now, run_id),
                )
                updated = self._run_row(str(run_id))
                self._checkpoint_tx(
                    updated, step=int(updated["step"]), status="needs_review", reason=reason
                )
                return False
            for call in call_rows:
                if (
                    bool(call["dispatch_allowed"])
                    or str(call["status"]) not in _PENDING_CALL_STATES
                ):
                    continue
                payload = {
                    "content": "incomplete_model_output",
                    "is_error": True,
                    "error_type": "incomplete_model_output",
                    "transient": False,
                    "outcome": "not_started",
                    "cleanup_confirmed": None,
                    "call_id": str(call["call_id"]),
                    "attempts": [],
                }
                now = _now()
                self._conn.execute(
                    "UPDATE tool_calls SET status='failed', result_json=?, "
                    "updated_at=? WHERE call_id=?",
                    (_dump(payload), now, call["call_id"]),
                )
                self._append_message_tx(
                    str(call["session_id"]),
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(call["tool_use_id"]),
                            "content": "incomplete_model_output",
                            "is_error": True,
                        }
                    ],
                    str(call["run_id"]),
                    "native",
                )
            now = _now()
            updated_identity = _dump(actual_identity)
            changed = self._conn.execute(
                "UPDATE runs SET status='running', owner_epoch=?, workspace_identity_json=?, "
                "reason=NULL, updated_at=? "
                "WHERE run_id=? AND status IN ('interrupted', 'needs_review')",
                (epoch, updated_identity, now, run_id),
            ).rowcount
            if changed != 1:
                return False
            updated = self._run_row(str(run_id))
            self._checkpoint_tx(updated, step=int(updated["step"]), status="running", reason=None)
            return True

        claimed = bool(self._write(action))
        if claimed:
            self.epoch = epoch
        return claimed

    # 记录用户对 needs_review run 的暂停或放弃选择
    def review_run(self, run_id: str, action: str, note: str) -> None:
        if action not in {"pause", "abandon"}:
            raise StorageError("review action must be pause or abandon")
        if not isinstance(note, str) or not note.strip():
            raise StorageError("review note must be non-empty text")

        # 将放弃选择和 session 关闭状态放在同一个事务中
        def write_review() -> None:
            self._assert_epoch_available(self.epoch)
            run = self._run_row(str(run_id))
            if str(run["status"]) not in {"interrupted", "needs_review"}:
                raise StorageError("only interrupted or needs_review runs can be reviewed")
            now = _now()
            self._conn.execute(
                "INSERT INTO reviews(review_id, run_id, action, note, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (_new_id(), run_id, action, note, now),
            )
            if action == "abandon":
                self._conn.execute(
                    "UPDATE runs SET status='cancelled', reason=?, updated_at=? WHERE run_id=?",
                    (note or "abandoned_by_user", now, run_id),
                )
                session = self._session_row(str(run["session_id"]))
                data = _load(session["data_json"])
                if not isinstance(data, dict):
                    raise StorageError("stored session data is not an object")
                data["status"] = "closed"
                data["closed_at"] = now
                self._conn.execute(
                    "UPDATE sessions SET data_json=?, updated_at=? WHERE id=?",
                    (_dump(data), now, run["session_id"]),
                )
                updated = self._run_row(str(run_id))
                self._checkpoint_tx(
                    updated, step=int(updated["step"]), status="cancelled", reason=note
                )

        self._write(write_review)

    # 返回某个 run 的全部人工审查记录
    def get_reviews(self, run_id: str) -> list[dict[str, Any]]:
        # 按时间顺序读取审查动作和备注
        def action() -> list[dict[str, Any]]:
            rows = self._conn.execute(
                "SELECT review_id, run_id, action, note, created_at FROM reviews "
                "WHERE run_id=? ORDER BY created_at, review_id",
                (str(run_id),),
            ).fetchall()
            return [
                {
                    "review_id": str(row["review_id"]),
                    "run_id": str(row["run_id"]),
                    "action": str(row["action"]),
                    "note": str(row["note"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

        return self._read(action)

    # 按稳定业务 request_id 查询已有 run 并校验原始用户内容
    def get_request(
        self,
        sid: str,
        request_id: str,
        content: str | None = None,
    ) -> dict[str, Any] | None:
        # 查询同一 session 的业务请求映射
        def action() -> dict[str, Any] | None:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE session_id=? AND request_id=? "
                "ORDER BY created_at LIMIT 1",
                (str(sid), str(request_id)),
            ).fetchone()
            if row is None:
                return None
            if content is not None and _load(row["request_content_json"]) != content:
                raise StorageError("request_id was reused with different user content")
            return self._run_dict(row)

        return self._read(action)

    # 将审批行还原为可供权限层使用的字典
    def _approval_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "approval_id": str(row["approval_id"]),
            "call_id": str(row["call_id"]),
            "epoch": str(row["epoch"]),
            "scope": _load(row["scope_json"]),
            "status": str(row["status"]),
            "decision": row["decision"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "consumed_at": row["consumed_at"],
        }

    # 持久化一个与当前 daemon epoch 绑定的一次性审批请求
    def create_approval(
        self,
        call_id: str,
        approval_id: str,
        epoch: str,
        scope: dict[str, Any],
    ) -> None:
        if not approval_id or not epoch or not isinstance(scope, dict):
            raise StorageError("approval_id, epoch and scope are required")

        # 写审批并将调用置于等待审批状态
        def action() -> None:
            call = self._call_row(str(call_id))
            run = self._run_row(str(call["run_id"]))
            self._check_owner_epoch(run, epoch)
            if (
                self._conn.execute(
                    "SELECT 1 FROM approvals WHERE approval_id=?", (approval_id,)
                ).fetchone()
                is not None
            ):
                raise StorageError(f"approval already exists: {approval_id}")
            now = _now()
            self._conn.execute(
                "INSERT INTO approvals(approval_id, call_id, epoch, scope_json, status, "
                "decision, created_at, resolved_at, consumed_at) "
                "VALUES (?, ?, ?, ?, 'pending', NULL, ?, NULL, NULL)",
                (approval_id, call_id, epoch, _dump(scope), now),
            )
            if str(call["status"]) in {"planned", "ready"}:
                self._conn.execute(
                    "UPDATE tool_calls SET status='waiting_approval', updated_at=? WHERE call_id=?",
                    (now, call_id),
                )

        self._write(action)

    # 在当前 epoch 内解析 pending 审批并保留用户 decision
    def resolve_approval(self, approval_id: str, epoch: str, decision: str) -> bool:
        if not approval_id or not epoch or not isinstance(decision, str):
            raise StorageError("approval_id, epoch and decision are required")
        valid_decisions = {
            "allow_once", "always_allow", "deny_once", "always_deny", "timeout", "cancelled",
        }
        if decision not in valid_decisions:
            raise StorageError(f"invalid approval decision: {decision}")
        allowed = {"allow_once", "always_allow"}

        # 条件更新 pending 审批，防止旧 daemon 越权响应
        def action() -> bool:
            self._assert_epoch_available(epoch)
            now = _now()
            status = "approved" if decision in allowed else "denied"
            changed = self._conn.execute(
                "UPDATE approvals SET status=?, decision=?, resolved_at=? "
                "WHERE approval_id=? AND epoch=? AND status='pending'",
                (status, decision, now, approval_id, epoch),
            ).rowcount
            return changed == 1

        return bool(self._write(action))

    # 消费一次已解析审批并把等待中的调用恢复为 planned
    def consume_approval(self, approval_id: str, epoch: str) -> bool:
        if not approval_id or not epoch:
            raise StorageError("approval_id and epoch are required")

        # 一次性消费 allow/deny 结果并解除调用等待状态
        def action() -> bool:
            self._assert_epoch_available(epoch)
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE approval_id=? AND epoch=? "
                "AND status IN ('approved', 'denied')",
                (approval_id, epoch),
            ).fetchone()
            if row is None:
                return False
            call = self._call_row(str(row["call_id"]))
            run = self._run_row(str(call["run_id"]))
            self._check_owner_epoch(run, epoch)
            now = _now()
            changed = self._conn.execute(
                "UPDATE approvals SET status='consumed', consumed_at=? "
                "WHERE approval_id=? AND epoch=? AND status IN ('approved', 'denied')",
                (now, approval_id, epoch),
            ).rowcount
            if changed == 1:
                next_status = (
                    "ready"
                    if self._conn.execute(
                        "SELECT 1 FROM attempts WHERE call_id=? "
                        "AND phase='result_committed' LIMIT 1",
                        (row["call_id"],),
                    ).fetchone()
                    is not None
                    else "planned"
                )
                self._conn.execute(
                    "UPDATE tool_calls SET status=?, updated_at=? "
                    "WHERE call_id=? AND status='waiting_approval'",
                    (next_status, now, row["call_id"]),
                )
            return changed == 1

        return bool(self._write(action))

    # 返回某个 run 关联的全部审批记录
    def get_approvals(self, run_id: str) -> list[dict[str, Any]]:
        # 通过 tool_calls 关联 run 并按时间读取审批
        def action() -> list[dict[str, Any]]:
            rows = self._conn.execute(
                "SELECT a.* FROM approvals a JOIN tool_calls c ON c.call_id=a.call_id "
                "WHERE c.run_id=? ORDER BY a.created_at, a.approval_id",
                (str(run_id),),
            ).fetchall()
            return [self._approval_dict(row) for row in rows]

        return self._read(action)

    # 创建持久化后台子任务而不改变主 session 当前 run
    def create_child(
        self,
        parent_run_id: str,
        child_run_id: str,
        goal: str,
        profile: str,
        background: bool,
    ) -> None:
        if not child_run_id or not isinstance(goal, str) or not isinstance(profile, str):
            raise StorageError("child run id, goal and profile are required")

        # 在派发后台任务前建立父子关系和独立 checkpoint
        def action() -> None:
            self._assert_epoch_available(self.epoch)
            parent = self._run_row(str(parent_run_id))
            self._check_owner_epoch(parent)
            if (
                self._conn.execute("SELECT 1 FROM runs WHERE run_id=?", (child_run_id,)).fetchone()
                is not None
            ):
                raise StorageError(f"run already exists: {child_run_id}")
            now = _now()
            self._conn.execute(
                "INSERT INTO runs(run_id, session_id, parent_run_id, workspace, "
                "workspace_identity_json, status, reason, request_id, request_content_json, "
                "owner_epoch, execution_config_json, child_goal, child_profile, "
                "child_background, child_result_json, step, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'running', NULL, NULL, NULL, ?, NULL, ?, ?, ?, "
                "NULL, 0, ?, ?)",
                (
                    child_run_id,
                    parent["session_id"],
                    parent_run_id,
                    parent["workspace"],
                    parent["workspace_identity_json"],
                    self.epoch,
                    goal,
                    profile,
                    1 if background else 0,
                    now,
                    now,
                ),
            )
            child = self._run_row(child_run_id)
            self._checkpoint_tx(child, step=0)

        self._write(action)

    # 持久化后台子任务的结果和最终状态
    def finish_child(
        self,
        child_run_id: str,
        status: str,
        result: Any,
        reason: str | None = None,
    ) -> None:
        if status not in {"succeeded", "failed", "cancelled", "interrupted", "needs_review"}:
            raise StorageError(f"invalid child status: {status}")

        # 只允许更新真正的 child run，不触碰父 run 当前状态
        def action() -> None:
            run = self._run_row(str(child_run_id))
            if run["parent_run_id"] is None:
                raise StorageError("run is not a child")
            self._check_owner_epoch(run)
            now = _now()
            self._conn.execute(
                "UPDATE runs SET status=?, reason=?, child_result_json=?, "
                "updated_at=? WHERE run_id=?",
                (status, reason, _dump(result), now, child_run_id),
            )
            updated = self._run_row(str(child_run_id))
            self._checkpoint_tx(updated, step=int(updated["step"]), status=status, reason=reason)

        self._write(action)

    # 读取 run 的最新 checkpoint 及待处理调用引用
    def get_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        # 查询 checkpoint 并展开 pending_call_ids 别名
        def action() -> dict[str, Any] | None:
            row = self._conn.execute(
                "SELECT * FROM checkpoints WHERE run_id=?", (str(run_id),)
            ).fetchone()
            if row is None:
                return None
            pending = _load(row["pending_json"])
            if not isinstance(pending, list):
                raise StorageError("checkpoint pending calls is not a list")
            return {
                "run_id": str(row["run_id"]),
                "version": int(row["version"]),
                "seq": int(row["seq"]),
                "message_seq": int(row["message_seq"]),
                "step": int(row["step"]),
                "status": str(row["status"]),
                "reason": row["reason"],
                "pending": list(pending),
                "pending_calls": list(pending),
                "pending_call_ids": list(pending),
                "next_action": str(row["next_action"]),
                "summary_ref": row["summary_ref"],
                "summary_from": row["summary_from"],
                "summary_to": row["summary_to"],
                "updated_at": row["updated_at"],
            }

        return self._read(action)

    # 将指定旧 session 快照导入当前 SQLite store
    def import_legacy(self, directory: Path) -> dict[str, Any]:
        from kama_claude.core.session.legacy import import_legacy

        return import_legacy(directory, self)


# 从旧目录导入 session 快照到指定或默认 SQLite store
def import_legacy(
    directory: Path,
    store: ExecutionStore | None = None,
) -> dict[str, Any]:
    from kama_claude.core.session.legacy import import_legacy as _import_legacy

    return _import_legacy(directory, store)


__all__ = ["ExecutionStore", "SCHEMA_VERSION", "StorageError", "import_legacy"]
