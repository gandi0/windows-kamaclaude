from __future__ import annotations

from pathlib import Path

from kama_claude.core.session.execution import ExecutionStore, StorageError
from kama_claude.core.session.legacy import import_legacy


# 功能：验证 legacy 导入保留每行原始 bytes、坏 JSON、未知 role 和孤儿消息
# 设计：用二进制快照和 SQLite 逐行表交叉检查，确保导入从不改写源 thread 文件
def test_legacy_snapshot_preserves_raw_rows(tmp_path: Path) -> None:
    legacy_dir = tmp_path / "old"
    legacy_dir.mkdir()
    meta = b'{"id":"old-1","title":"old","workspace":null}\n'
    thread = (
        b'{"role":"user","content":"hello"}\r\n'
        b'{bad json\n'
        b'{"role":"system","content":"keep"}\n'
        b'{"role":"assistant","content":[{"type":"tool_use","id":"orphan","name":"x","input":{}}]}\n'
    )
    (legacy_dir / "meta.json").write_bytes(meta)
    (legacy_dir / "thread.jsonl").write_bytes(thread)
    original_meta = (legacy_dir / "meta.json").read_bytes()
    original_thread = (legacy_dir / "thread.jsonl").read_bytes()
    store = ExecutionStore(tmp_path / "state.sqlite3")
    result = import_legacy(legacy_dir, store)
    assert result["status"] == "imported"
    assert result["session_id"] == "old-1"
    assert (legacy_dir / "meta.json").read_bytes() == original_meta
    assert (legacy_dir / "thread.jsonl").read_bytes() == original_thread
    assert [item["seq"] for item in store.messages("old-1")] == [1, 2]
    rows = store._conn.execute(
        "SELECT line_no, raw_bytes, parse_status FROM legacy_rows WHERE import_id=? ORDER BY line_no",
        (result["import_id"],),
    ).fetchall()
    assert [int(row["line_no"]) for row in rows] == [1, 2, 3, 4]
    assert [bytes(row["raw_bytes"]) for row in rows] == thread.splitlines(keepends=True)
    assert rows[1]["parse_status"] == "invalid_json"
    assert rows[2]["parse_status"] == "unknown_role"
    assert rows[3]["parse_status"] == "orphan"
    assert store._conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    store.close()


# 功能：验证相同 legacy 快照幂等以及只追加后保持既有消息 ID/seq
# 设计：先重复导入再追加一行，比较第一次映射与后续稳定序列
def test_legacy_idempotent_and_append(tmp_path: Path) -> None:
    legacy_dir = tmp_path / "old"
    legacy_dir.mkdir()
    (legacy_dir / "meta.json").write_bytes(b'{"id":"old-2"}')
    first_line = b'{"role":"user","content":"one"}\n'
    (legacy_dir / "thread.jsonl").write_bytes(first_line)
    store = ExecutionStore(tmp_path / "state.sqlite3")
    first = import_legacy(legacy_dir, store)
    repeated = import_legacy(legacy_dir, store)
    assert repeated["status"] == "already_imported"
    assert repeated["import_id"] == first["import_id"]
    original_message = store.messages("old-2")[0]
    (legacy_dir / "thread.jsonl").write_bytes(
        first_line + b'{"role":"assistant","content":"two"}\n'
    )
    appended = import_legacy(legacy_dir, store)
    assert appended["status"] == "appended"
    assert appended["session_id"] == "old-2"
    messages = store.messages("old-2")
    assert messages[0]["id"] == original_message["id"]
    assert [item["seq"] for item in messages] == [1, 2]
    assert messages[1]["content"] == "two"
    store.close()


# 功能：验证源文件重写后产生隔离 session 并报告 conflict
# 设计：在同一 source path 改写已有前缀，检查旧 session 不被覆盖且新快照可查
def test_legacy_rewrite_isolated(tmp_path: Path) -> None:
    legacy_dir = tmp_path / "old"
    legacy_dir.mkdir()
    (legacy_dir / "meta.json").write_bytes(b'{"id":"old-3"}')
    (legacy_dir / "thread.jsonl").write_bytes(b'{"role":"user","content":"before"}\n')
    store = ExecutionStore(tmp_path / "state.sqlite3")
    first = import_legacy(legacy_dir, store)
    (legacy_dir / "thread.jsonl").write_bytes(b'{"role":"user","content":"after"}\n')
    conflict = import_legacy(legacy_dir, store)
    assert conflict["status"] == "conflict"
    assert conflict["session_id"] != first["session_id"]
    assert store.messages(first["session_id"])[0]["content"] == "before"
    assert store.messages(conflict["session_id"])[0]["content"] == "after"
    store.close()


# 功能：验证重写缩短后的新快照仍可追加且前缀消息映射保持稳定
# 设计：先冲突隔离，再对隔离快照追加后缀，比较前缀 message id/seq 与新消息序号
def test_legacy_shorten_then_append_keeps_mapping(tmp_path: Path) -> None:
    legacy_dir = tmp_path / "old"
    legacy_dir.mkdir()
    (legacy_dir / "meta.json").write_bytes(b'{"id":"old-4"}')
    first = (
        b'{"role":"user","content":"one"}\n'
        b'{"role":"assistant","content":"two"}\n'
    )
    (legacy_dir / "thread.jsonl").write_bytes(first)
    store = ExecutionStore(tmp_path / "state.sqlite3")
    original = import_legacy(legacy_dir, store)
    (legacy_dir / "thread.jsonl").write_bytes(first.splitlines(keepends=True)[0])
    rewritten = import_legacy(legacy_dir, store)
    assert rewritten["status"] == "conflict"
    prefix = store.messages(rewritten["session_id"])[0]
    (legacy_dir / "thread.jsonl").write_bytes(
        first.splitlines(keepends=True)[0] + b'{"role":"user","content":"three"}\n'
    )
    appended = import_legacy(legacy_dir, store)
    assert appended["status"] == "appended"
    current = store.messages(rewritten["session_id"])
    assert current[0]["id"] == prefix["id"]
    assert [item["seq"] for item in current] == [1, 2]
    assert current[1]["content"] == "three"
    assert store.messages(original["session_id"])[1]["content"] == "two"
    store.close()


# 功能：验证 legacy 导入提交失败时整批回滚且源文件保持不变
# 设计：在真实 _before_commit 边界注入异常后重开数据库，检查 import/row/message 均无残留
def test_legacy_import_rolls_back_as_one_transaction(tmp_path: Path) -> None:
    legacy_dir = tmp_path / "old"
    legacy_dir.mkdir()
    (legacy_dir / "meta.json").write_bytes(b'{"id":"old-5"}')
    raw = b'{"role":"user","content":"keep bytes"}\r\n'
    (legacy_dir / "thread.jsonl").write_bytes(raw)
    before = (legacy_dir / "thread.jsonl").read_bytes()
    db_path = tmp_path / "state.sqlite3"
    store = ExecutionStore(db_path)

    # 在实际事务提交前注入故障以验证整批回滚
    def fail_commit() -> None:
        raise RuntimeError("legacy commit failure")

    store._before_commit = fail_commit
    try:
        try:
            import_legacy(legacy_dir, store)
        except StorageError:
            pass
        else:
            raise AssertionError("legacy import should fail at commit hook")
    finally:
        store.close()
    assert (legacy_dir / "thread.jsonl").read_bytes() == before
    reopened = ExecutionStore(db_path)
    assert reopened.get_session("old-5") is None
    assert reopened._conn.execute("SELECT COUNT(*) FROM legacy_imports").fetchone()[0] == 0
    assert reopened._conn.execute("SELECT COUNT(*) FROM legacy_rows").fetchone()[0] == 0
    assert reopened.messages("old-5") == []
    reopened.close()


# 功能：验证目录缺失或空目录不会静默伪造空 legacy session
# 设计：使用真实文件系统边界并断言统一 StorageError
def test_legacy_missing_snapshot_rejected(tmp_path: Path) -> None:
    store = ExecutionStore(tmp_path / "state.sqlite3")
    empty = tmp_path / "empty"
    empty.mkdir()
    try:
        try:
            import_legacy(empty, store)
        except StorageError:
            pass
        else:
            raise AssertionError("empty legacy directory should be rejected")
    finally:
        store.close()
