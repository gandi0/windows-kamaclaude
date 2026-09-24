from __future__ import annotations

import multiprocessing
import sqlite3
import subprocess
from pathlib import Path

import pytest

from kama_claude.core.session.daemon_lock import DaemonLock
from kama_claude.core.session.execution import SCHEMA_VERSION, ExecutionStore, StorageError
from kama_claude.core.tools.base import ToolAttempt, ToolResult


# 创建绑定当前临时工作区的执行存储
def _store(path: Path, workspace: Path) -> ExecutionStore:
    store = ExecutionStore(path)
    store.put_session({"id": "s1", "title": ""}, workspace=str(workspace))
    return store


# 写入可被 S8.3 迁移识别的最小 S8.2 schema
def _write_v1(path: Path, workspace: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE sessions(
            id TEXT PRIMARY KEY, data_json TEXT NOT NULL, workspace TEXT,
            current_run_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE runs(
            run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            parent_run_id TEXT REFERENCES runs(run_id), workspace TEXT NOT NULL,
            status TEXT NOT NULL, reason TEXT, step INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE messages(
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            seq INTEGER NOT NULL, role TEXT NOT NULL, content_json TEXT NOT NULL,
            run_id TEXT, source TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(session_id, seq)
        );
        CREATE TABLE tool_calls(
            call_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            run_id TEXT NOT NULL REFERENCES runs(run_id), step INTEGER NOT NULL,
            ordinal INTEGER NOT NULL, tool_use_id TEXT NOT NULL, name TEXT NOT NULL,
            input_json TEXT NOT NULL, params_hash TEXT NOT NULL, effect TEXT NOT NULL,
            retry_safe INTEGER NOT NULL, workspace TEXT NOT NULL, status TEXT NOT NULL,
            result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE attempts(
            attempt_id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES tool_calls(call_id),
            attempt_no INTEGER NOT NULL, phase TEXT NOT NULL, outcome TEXT, error_type TEXT,
            cleanup_confirmed INTEGER, result_json TEXT, started_at TEXT NOT NULL,
            finished_at TEXT, UNIQUE(call_id, attempt_no)
        );
        CREATE TABLE checkpoints(
            run_id TEXT PRIMARY KEY REFERENCES runs(run_id), version INTEGER NOT NULL,
            seq INTEGER NOT NULL, message_seq INTEGER NOT NULL, step INTEGER NOT NULL,
            status TEXT NOT NULL, reason TEXT, pending_json TEXT NOT NULL,
            next_action TEXT NOT NULL, summary_ref TEXT, summary_from INTEGER,
            summary_to INTEGER, updated_at TEXT NOT NULL
        );
        CREATE TABLE legacy_imports(
            import_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            source_path TEXT NOT NULL, meta_hash TEXT NOT NULL, thread_hash TEXT NOT NULL,
            meta_bytes BLOB NOT NULL, thread_bytes BLOB NOT NULL, status TEXT NOT NULL,
            conflict TEXT, created_at TEXT NOT NULL,
            UNIQUE(source_path, meta_hash, thread_hash)
        );
        CREATE TABLE legacy_rows(
            import_id TEXT NOT NULL REFERENCES legacy_imports(import_id), line_no INTEGER NOT NULL,
            raw_bytes BLOB NOT NULL, parse_status TEXT NOT NULL, error TEXT, role TEXT,
            message_id TEXT, seq INTEGER, PRIMARY KEY(import_id, line_no)
        );
        INSERT INTO sessions VALUES('s1', '{"id":"s1","title":"old","run_ids":["r1"]}', NULL, NULL, 't0', 't0');
        INSERT INTO runs VALUES('r1', 's1', NULL, '', 'succeeded', NULL, 1, 't1', 't1');
        INSERT INTO messages VALUES('m1', 's1', 1, 'user', '"old message"', 'r1', 'native', 't1');
        INSERT INTO checkpoints VALUES('r1', 1, 1, 1, 1, 'succeeded', NULL, '[]', 'done', NULL, NULL, NULL, 't1');
        PRAGMA user_version=1;
        """,
    )
    conn.execute("UPDATE sessions SET workspace=? WHERE id='s1'", (str(workspace),))
    conn.execute("UPDATE runs SET workspace=? WHERE run_id='r1'", (str(workspace),))
    conn.commit()
    conn.close()


# 功能：验证 v1 迁移补充 S8.3 表列并保留旧 session、run、消息和检查点
# 设计：手工构造缺少新增列的真实 v1 数据库，重开后同时查公开 API 和 user_version
def test_v1_migration_preserves_facts(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    _write_v1(db, tmp_path)
    store = ExecutionStore(db)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert store.get_session("s1")["run_ids"] == ["r1"]
    assert store.messages("s1")[0]["content"] == "old message"
    assert store.get_run("r1")["status"] == "succeeded"
    assert store.get_checkpoint("r1")["next_action"] == "done"
    store.close()


# 功能：验证 request_id 相同内容原子去重且不同原始内容被拒绝
# 设计：通过两次 begin_run 比较稳定 run_id、消息数量和冲突异常，覆盖重试不追加消息
def test_request_id_dedup_and_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path / "state.sqlite3", tmp_path)
    first = store.begin_run(
        "s1", "r1", workspace=str(tmp_path), request_id="req", user_content="same"
    )
    assert first == "r1"
    repeated = store.begin_run(
        "s1", "r2", workspace=str(tmp_path), request_id="req", user_content="same"
    )
    assert repeated == "r1"
    assert len(store.messages("s1")) == 1
    with pytest.raises(StorageError, match="different"):
        store.begin_run("s1", "r3", workspace=str(tmp_path), request_id="req", user_content="other")
    store.close()


# 功能：验证两个连接对同一中断 run 只有一个 epoch 能成功 CAS 认领
# 设计：两个独立 SQLite 连接分别启动 daemon 后竞争 claim，检查第二个连接保持非 running
def test_claim_run_is_single_winner(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    first = _store(db, tmp_path)
    first.begin_run("s1", "r1", workspace=str(tmp_path), user_content="goal")
    first.close()
    left = ExecutionStore(db)
    right = ExecutionStore(db)
    left.start_daemon("e1")
    right.start_daemon("e1")
    assert left.claim_run("r1", "e1", str(tmp_path)) is True
    assert right.claim_run("r1", "e1", str(tmp_path)) is False
    assert left.get_run("r1")["status"] == "running"
    left.close()
    right.close()


# 功能：验证启动扫描把未知派发置为 needs_review，同时保留已提交结果
# 设计：分别构造 dispatching 和已提交成功 call，重开扫描后检查状态与结果事实
def test_daemon_scan_preserves_unknown_and_result(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    store = _store(db, tmp_path)
    store.begin_run("s1", "r1", workspace=str(tmp_path))
    store.commit_response(
        "r1",
        1,
        [{"type": "tool_use", "id": "u1", "name": "read_file", "input": {"path": "x"}}],
        [
            {
                "call_id": "c1",
                "tool_use_id": "u1",
                "name": "read_file",
                "input": {"path": "x"},
                "effect": "read_only",
                "retry_safe": True,
            }
        ],
    )
    store.start_attempt("c1", "a1", 1)
    store.close()
    reopened = ExecutionStore(db)
    reopened.start_daemon("e1")
    assert reopened.get_run("r1")["status"] == "needs_review"
    reopened.close()

    db2 = tmp_path / "result.sqlite3"
    stored = _store(db2, tmp_path)
    stored.begin_run("s1", "r1", workspace=str(tmp_path))
    stored.commit_response(
        "r1",
        1,
        [{"type": "tool_use", "id": "u1", "name": "read_file", "input": {"path": "x"}}],
        [
            {
                "call_id": "c1",
                "tool_use_id": "u1",
                "name": "read_file",
                "input": {"path": "x"},
                "effect": "read_only",
                "retry_safe": True,
            }
        ],
    )
    stored.start_attempt("c1", "a1", 1)
    stored.commit_result(
        "c1", ToolResult("ok", call_id="c1", attempts=[ToolAttempt("a1", 1, "known", None)])
    )
    stored.close()
    result_store = ExecutionStore(db2)
    result_store.start_daemon("e1")
    assert result_store.get_run("r1")["status"] == "interrupted"
    assert result_store.get_call("c1")["result"]["content"] == "ok"
    result_store.close()


# 功能：验证相关文件在恢复前被改写时进入 needs_review 且派发前冲突抛错
# 设计：用 read_file guard 捕获文件 hash，外部改写后分别检查 start_attempt 和 claim 的阻止结果
def test_workspace_file_conflict_blocks_recovery(tmp_path: Path) -> None:
    target = tmp_path / "input.txt"
    target.write_text("before", encoding="utf-8")
    db = tmp_path / "state.sqlite3"
    store = _store(db, tmp_path)
    store.begin_run("s1", "r1", workspace=str(tmp_path))
    store.commit_response(
        "r1",
        1,
        [{"type": "tool_use", "id": "u1", "name": "read_file", "input": {"path": "input.txt"}}],
        [
            {
                "call_id": "c1",
                "tool_use_id": "u1",
                "name": "read_file",
                "input": {"path": "input.txt"},
                "effect": "read_only",
                "retry_safe": True,
            }
        ],
    )
    target.write_text("after", encoding="utf-8")
    with pytest.raises(StorageError, match="workspace_conflict"):
        store.start_attempt("c1", "a1", 1)
    assert store.get_run("r1")["status"] == "needs_review"
    store.close()


# 功能：验证同一路径先读后写后恢复只校验累计最后文件状态
# 设计：先提交 read 结果再提交写结果并改变文件，重启后按最后 write guard 成功认领
def test_workspace_guard_uses_cumulative_last_state(tmp_path: Path) -> None:
    target = tmp_path / "state.txt"
    target.write_text("before", encoding="utf-8")
    store = _store(tmp_path / "state.sqlite3", tmp_path)
    store.begin_run("s1", "r1", workspace=str(tmp_path))
    store.commit_response(
        "r1",
        1,
        [{"type": "tool_use", "id": "u1", "name": "read_file", "input": {"path": "state.txt"}}],
        [
            {
                "call_id": "read",
                "tool_use_id": "u1",
                "name": "read_file",
                "input": {"path": "state.txt"},
                "effect": "read_only",
                "retry_safe": True,
            }
        ],
    )
    store.start_attempt("read", "a1", 1)
    store.commit_result(
        "read", ToolResult("before", call_id="read", attempts=[ToolAttempt("a1", 1, "known", None)])
    )
    store.commit_response(
        "r1",
        2,
        [{"type": "tool_use", "id": "u2", "name": "write_file", "input": {"path": "state.txt"}}],
        [
            {
                "call_id": "write",
                "tool_use_id": "u2",
                "name": "write_file",
                "input": {"path": "state.txt"},
                "effect": "write",
                "retry_safe": False,
            }
        ],
    )
    store.start_attempt("write", "a2", 1)
    target.write_text("after", encoding="utf-8")
    store.commit_result(
        "write", ToolResult("done", call_id="write", attempts=[ToolAttempt("a2", 1, "known", None)])
    )
    store.close()
    reopened = ExecutionStore(tmp_path / "state.sqlite3")
    reopened.start_daemon("e1")
    assert reopened.claim_run("r1", "e1", str(tmp_path)) is True
    reopened.close()


# 功能：验证同一模型批次的两个写调用按顺序传播前一个后置文件状态
# 设计：两条同路径 write intent 同批提交，第一条完成后第二条仍可派发并支持重启认领
def test_workspace_guard_propagates_within_batch(tmp_path: Path) -> None:
    target = tmp_path / "batch.txt"
    target.write_text("zero", encoding="utf-8")
    store = _store(tmp_path / "state.sqlite3", tmp_path)
    store.begin_run("s1", "r1", workspace=str(tmp_path))
    write_input = {"path": "batch.txt"}
    store.commit_response(
        "r1", 1,
        [
            {"type": "tool_use", "id": "u1", "name": "write_file", "input": write_input},
            {"type": "tool_use", "id": "u2", "name": "write_file", "input": write_input},
        ],
        [
            {"call_id": "w1", "tool_use_id": "u1", "name": "write_file", "input": write_input, "effect": "write", "retry_safe": False},
            {"call_id": "w2", "tool_use_id": "u2", "name": "write_file", "input": write_input, "effect": "write", "retry_safe": False},
        ],
    )
    store.start_attempt("w1", "a1", 1)
    target.write_text("one", encoding="utf-8")
    store.commit_result("w1", ToolResult("one", call_id="w1", attempts=[ToolAttempt("a1", 1, "known", None)]))
    store.start_attempt("w2", "a2", 1)
    target.write_text("two", encoding="utf-8")
    store.commit_result("w2", ToolResult("two", call_id="w2", attempts=[ToolAttempt("a2", 1, "known", None)]))
    store.close()
    reopened = ExecutionStore(tmp_path / "state.sqlite3")
    reopened.start_daemon("e1")
    assert reopened.claim_run("r1", "e1", str(tmp_path)) is True
    reopened.close()


# 功能：验证 workspace junction 目标被替换后恢复拒绝继续
# 设计：使用 Windows mklink /J 建立专用目录链接，替换链接目标并检查 root identity 冲突
def test_workspace_junction_replacement_blocks_claim(tmp_path: Path) -> None:
    target_a = tmp_path / "target-a"
    target_b = tmp_path / "target-b"
    link = tmp_path / "workspace-link"
    target_a.mkdir()
    target_b.mkdir()
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target_a)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"mklink /J unavailable: {result.stderr.strip()}")
    try:
        store = _store(tmp_path / "state.sqlite3", link)
        store.begin_run("s1", "r1", workspace=str(link))
        store.close()
        reopened = ExecutionStore(tmp_path / "state.sqlite3")
        reopened.start_daemon("e1")
        subprocess.run(["cmd", "/c", "rmdir", str(link)], check=True, capture_output=True)
        replacement = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target_b)],
            capture_output=True,
            text=True,
        )
        if replacement.returncode != 0:
            pytest.skip(f"mklink replacement unavailable: {replacement.stderr.strip()}")
        assert reopened.claim_run("r1", "e1", str(link)) is False
        assert reopened.get_run("r1")["status"] == "needs_review"
        reopened.close()
    finally:
        if link.exists():
            subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)


# 功能：验证审批按 epoch 持久化、旧 epoch 启动失效并可一次性消费
# 设计：完整走 create/resolve/consume 流程，再重启新 epoch 检查旧 pending 被标记 expired
def test_approval_epoch_and_consume(tmp_path: Path) -> None:
    store = _store(tmp_path / "state.sqlite3", tmp_path)
    store.begin_run("s1", "r1", workspace=str(tmp_path))
    store.commit_response(
        "r1",
        1,
        [{"type": "tool_use", "id": "u1", "name": "write_file", "input": {"path": "x"}}],
        [
            {
                "call_id": "c1",
                "tool_use_id": "u1",
                "name": "write_file",
                "input": {"path": "x"},
                "effect": "write",
                "retry_safe": False,
            }
        ],
    )
    store.create_approval("c1", "ap1", "e1", {"path": "x"})
    assert store.resolve_approval("ap1", "e1", "allow_once") is True
    assert store.consume_approval("ap1", "e1") is True
    assert store.consume_approval("ap1", "e1") is False
    assert store.get_call("c1")["status"] == "planned"
    store.create_approval("c1", "ap2", "e1", {"path": "x"})
    store.start_daemon("e2")
    assert store.get_approvals("r1")[-1]["status"] == "expired"
    store.close()


# 功能：验证 child 持久关系、结果和 daemon 重启中断状态不影响主 session current_run
# 设计：建立主 run 与 child，重启扫描后检查父子字段、子状态和 session run_ids 排除 child
def test_child_relationship_and_recovery_visibility(tmp_path: Path) -> None:
    store = _store(tmp_path / "state.sqlite3", tmp_path)
    parent = store.begin_run("s1", "r1", workspace=str(tmp_path), user_content="parent")
    store.create_child(parent, "c1", "child goal", "profile-a", True)
    assert store.get_run("c1")["goal"] == "child goal"
    assert store.get_session("s1")["run_ids"] == ["r1"]
    store.close()
    reopened = ExecutionStore(tmp_path / "state.sqlite3")
    reopened.start_daemon("e2")
    child = reopened.get_run("c1")
    assert child["status"] == "interrupted"
    assert child["reason"] == "child_recovery_not_supported"
    reopened.finish_child("c1", "failed", {"error": "stopped"}, "worker stopped")
    assert reopened.get_run("c1")["result"] == {"error": "stopped"}
    reopened.close()


# 持锁子进程的辅助函数
def _hold_lock(path: str, ready: multiprocessing.Queue[str]) -> None:
    lock = DaemonLock(Path(path))
    lock.acquire()
    ready.put("ready")
    try:
        ready.get()
    finally:
        lock.close()


# 功能：验证 daemon lock 不依赖 PID 文件且可跨进程互斥
# 设计：子进程持有真实文件锁，父进程抢锁必须失败，释放后再次获取成功
def test_daemon_lock_cross_process(tmp_path: Path) -> None:
    path = tmp_path / "daemon.lock"
    queue: multiprocessing.Queue[str] = multiprocessing.Queue()
    process = multiprocessing.Process(target=_hold_lock, args=(str(path), queue))
    process.start()
    assert queue.get(timeout=10) == "ready"
    contender = DaemonLock(path)
    with pytest.raises(StorageError):
        contender.acquire()
    queue.put("stop")
    process.join(timeout=10)
    assert process.exitcode == 0
    contender.acquire()
    contender.close()
