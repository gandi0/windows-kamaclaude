from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kama_claude.core.session.execution import ExecutionStore, StorageError
from kama_claude.core.tools.base import ToolAttempt, ToolResult


# 创建具备一个 session 的真实 SQLite store
def _store(path: Path) -> ExecutionStore:
    store = ExecutionStore(path)
    store.put_session({"id": "s1", "title": ""}, workspace=str(path.parent))
    return store


# 功能：验证 WAL/FULL/FK、workspace 独立字段、稳定消息 seq 和重开读取
# 设计：真实连接关闭后重新打开并检查 PRAGMA 与公开 API，覆盖持久化而非内存缓存
def test_store_pragmas_seq_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = _store(path)
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store.append_message("s1", "user", "one") == 1
    assert store.append_message("s1", "assistant", [{"type": "text", "text": "two"}]) == 2
    store.close()

    reopened = ExecutionStore(path)
    assert reopened.get_session("s1")["workspace"] == str(tmp_path)
    assert [item["seq"] for item in reopened.messages("s1")] == [1, 2]
    assert reopened.messages("s1")[1]["content"][0]["text"] == "two"
    reopened.close()


# 功能：验证包含内部保留字样式键的用户字典按标准 JSON 原样回放
# 设计：真实关闭重开后比较类型和值，防止内部编码约定污染用户消息
def test_magic_key_dict_is_not_decoded(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = _store(path)
    content = {"__kama_bytes__": "ordinary text", "nested": {"value": 1}}
    store.append_message("s1", "user", content)
    store.close()
    reopened = ExecutionStore(path)
    assert reopened.messages("s1")[0]["content"] == content
    reopened.close()


# 功能：验证响应意图、派发、结果和最终回复的原子状态链
# 设计：使用真实 ToolResult 和数据库重开，交叉检查 call/attempt/message/checkpoint 四类事实
def test_response_attempt_result_and_final(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = _store(path)
    store.begin_run("s1", "r1", workspace=str(tmp_path), user_content="goal")
    store.commit_response(
        "r1",
        1,
        [{"type": "tool_use", "id": "tu1", "name": "read_file", "input": {"path": "a"}}],
        [{
            "call_id": "c1", "tool_use_id": "tu1", "name": "read_file", "input": {"path": "a"},
            "effect": "read_only", "retry_safe": True,
        }],
    )
    store.start_attempt("c1", "a1", 1)
    store.commit_result(
        "c1",
        ToolResult("ok", call_id="c1", attempts=[ToolAttempt("a1", 1, "known", None)]),
    )
    assert store.get_call("c1")["status"] == "succeeded"
    assert store.get_attempts("c1")[0]["phase"] == "result_committed"
    assert store.messages("s1")[-1]["content"][0]["tool_use_id"] == "tu1"
    assert store.get_checkpoint("r1")["pending_call_ids"] == []
    store.commit_response("r1", 2, [{"type": "text", "text": "done"}], [], final=True)
    assert store.get_run("r1")["status"] == "succeeded"
    store.close()

    reopened = ExecutionStore(path)
    assert reopened.get_checkpoint("r1")["next_action"] == "done"
    assert reopened.get_call("c1")["result"]["content"] == "ok"
    reopened.close()


# 功能：验证提交钩子失败会回滚 assistant、调用意图和初始检查点
# 设计：注入真实事务提交边界并重开数据库，证明没有半提交 marker
def test_before_commit_rolls_back_response(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = _store(path)
    store.begin_run("s1", "r1", workspace=str(tmp_path), user_content="goal")

    # 在实际事务提交前注入故障以验证整批回滚
    def fail_commit() -> None:
        raise RuntimeError("injected commit failure")

    store._before_commit = fail_commit
    with pytest.raises(StorageError):
        store.commit_response(
            "r1", 1,
            [{"type": "tool_use", "id": "tu1", "name": "read_file", "input": {}}],
            [{
                "call_id": "c1", "tool_use_id": "tu1", "name": "read_file", "input": {},
                "effect": "read_only", "retry_safe": True,
            }],
        )
    store._before_commit = lambda: None
    assert store.get_call("c1") is None
    assert len(store.messages("s1")) == 1
    store.close()
    reopened = ExecutionStore(path)
    assert reopened.get_call("c1") is None
    assert reopened.get_checkpoint("r1")["message_seq"] == 1
    reopened.close()


# 功能：验证退避中取消不创建新 attempt 且不改写暂时失败事实
# 设计：retry=True 后直接提交无新 attempt 的取消结果，再检查 attempt 与终态 call 分离
def test_cancel_after_retry_preserves_attempt_result(tmp_path: Path) -> None:
    store = _store(tmp_path / "state.sqlite3")
    store.begin_run("s1", "r1", workspace=str(tmp_path))
    store.commit_response(
        "r1", 1,
        [{"type": "tool_use", "id": "tu1", "name": "read_file", "input": {}}],
        [{
            "call_id": "c1", "tool_use_id": "tu1", "name": "read_file", "input": {},
            "effect": "read_only", "retry_safe": True,
        }],
    )
    store.start_attempt("c1", "a1", 1)
    store.commit_result(
        "c1",
        ToolResult(
            "temporary", True, "runtime_error", transient=True, call_id="c1",
            attempts=[ToolAttempt("a1", 1, "known", "runtime_error")],
        ),
        retry=True,
    )
    store.commit_result(
        "c1",
        ToolResult(
            "cancelled", True, "cancelled", outcome="unknown", call_id="c1",
            attempts=[ToolAttempt("a1", 1, "known", "runtime_error")],
        ),
    )
    attempts = store.get_attempts("c1")
    assert len(attempts) == 1
    assert attempts[0]["result"]["content"] == "temporary"
    assert store.get_call("c1")["status"] == "unknown"
    assert store.get_run("r1")["status"] == "needs_review"
    store.close()


# 功能：验证未知版本数据库在任何 WAL 写入前被拒绝
# 设计：先建立真实文件再用独立 sqlite 连接设置 user_version，重新构造 store 断言统一异常
def test_newer_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    store = ExecutionStore(path)
    store.close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version=99")
    conn.commit()
    conn.close()
    before = path.read_bytes()
    with pytest.raises(StorageError, match="newer"):
        ExecutionStore(path)
    assert path.read_bytes() == before


# 功能：验证 step、tool_use 配对和同 session 未完成 run 的安全约束
# 设计：用真实调用链触发三类拒绝并确认失败事务没有新增消息或调用
def test_response_validation_and_run_ownership(tmp_path: Path) -> None:
    store = _store(tmp_path / "state.sqlite3")
    store.begin_run("s1", "r1", workspace=str(tmp_path))
    with pytest.raises(StorageError):
        store.commit_response("r1", 2, [], [])
    with pytest.raises(StorageError):
        store.commit_response(
            "r1", 1, [{"type": "tool_use", "id": "tu1", "name": "x", "input": {}}], [],
        )
    assert store.messages("s1") == []
    with pytest.raises(StorageError):
        store.begin_run("s1", "r2", workspace=str(tmp_path))
    store.close()
