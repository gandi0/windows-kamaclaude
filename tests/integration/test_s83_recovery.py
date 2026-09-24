from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from tests.helpers.s83_daemon import DaemonProcess, RpcClient, RpcError

_POLL_SECONDS = 0.05
_STATUS_TIMEOUT = 30.0


# 创建真实 Shell 会执行的本地计数脚本
def _write_counter_script(workspace: Path, *, initial: int = 0) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "counter.txt").write_text(str(initial), encoding="utf-8")
    (workspace / "marker.txt").write_text("not-run", encoding="utf-8")
    (workspace / "counter.py").write_text(
        "from pathlib import Path\n"
        "counter = Path('counter.txt')\n"
        "value = int(counter.read_text(encoding='utf-8')) if counter.exists() else 0\n"
        "counter.write_text(str(value + 1), encoding='utf-8')\n"
        "Path('marker.txt').write_text('incremented', encoding='utf-8')\n",
        encoding="utf-8",
    )


# 读取本地副作用计数和 marker，确保断言来自真实工具执行
def _counter_state(workspace: Path) -> tuple[int, str]:
    return int((workspace / "counter.txt").read_text(encoding="utf-8")), (
        workspace / "marker.txt"
    ).read_text(encoding="utf-8")


# 创建指定场景的隔离 daemon 描述
def _daemon(tmp_path: Path, scenario: str, name: str) -> DaemonProcess:
    return DaemonProcess(
        profile=tmp_path / f"profile-{name}",
        workspace=tmp_path / f"workspace-{name}",
        scenario=scenario,
    )


# 创建 session 并核对 RPC 返回的稳定身份
async def _create_session(client: RpcClient, workspace: Path, title: str) -> str:
    result = await client.call(
        "session.create",
        {"mode": "chat", "title": title, "workspace": str(workspace)},
    )
    assert result.get("session_id"), result
    assert result.get("status") in {"active", "waiting_for_input"}, result
    return str(result["session_id"])


# 等待权限请求并提交一次性 allow，返回原始审批 token
async def _allow_once(client: RpcClient) -> dict[str, Any]:
    event = await client.next_event("permission.requested", timeout=_STATUS_TIMEOUT)
    assert event.get("tool_use_id"), event
    assert event.get("approval_id"), event
    params: dict[str, Any] = {
        "tool_use_id": event["tool_use_id"],
        "approval_id": event["approval_id"],
        "decision": "allow_once",
    }
    response = await client.call("permission.respond", params)
    assert response.get("ok", True) is not False, response
    return event


# 轮询持久状态直到满足谓词，避免固定 sleep 掩盖恢复竞态
async def _wait_status(
    client: RpcClient,
    session_id: str,
    predicate: Any,
    *,
    timeout: float = _STATUS_TIMEOUT,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = await client.call("session.status", {"session_id": session_id})
        if predicate(latest):
            return latest
        await asyncio.sleep(_POLL_SECONDS)
    raise AssertionError(f"session status did not reach target: {latest}")


# 从 session.status 结果中提取指定 run、调用和子任务
def _status_parts(status: dict[str, Any], run_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    runs = list(status.get("runs", []))
    run: dict[str, Any] = next(
        (row for row in runs if str(row.get("run_id", row.get("id"))) == run_id),
        {},
    )
    calls = [
        row for row in status.get("calls", [])
        if row.get("run_id") is None or str(row.get("run_id")) == run_id
    ]
    children = [row for row in status.get("children", []) if str(row.get("parent_run_id")) == run_id]
    return run, calls, children


# 以只读 URI 打开执行数据库并返回 call/attempt 原始事实
def _read_execution_facts(database: Path) -> dict[str, list[dict[str, Any]]]:
    conn = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        calls = [dict(row) for row in conn.execute(
            "SELECT call_id, run_id, name, status, result_json FROM tool_calls ORDER BY created_at"
        )]
        attempts = [dict(row) for row in conn.execute(
            "SELECT attempt_id, call_id, attempt_no, phase, outcome, result_json "
            "FROM attempts ORDER BY started_at"
        )]
        return {"calls": calls, "attempts": attempts}
    finally:
        conn.close()


# 判断旧审批 token 是否被 daemon 明确拒绝
async def _assert_old_approval_rejected(
    client: RpcClient, event: dict[str, Any],
) -> None:
    params = {
        "tool_use_id": event["tool_use_id"],
        "approval_id": event["approval_id"],
        "decision": "allow_once",
    }
    try:
        response = await client.call("permission.respond", params)
    except RpcError:
        return
    assert (
        response.get("ok") is False
        or response.get("accepted") is False
        or response.get("status") in {"rejected", "stale", "expired"}
    ), response


# 功能：验证结果提交后 daemon 硬中断、重启和双客户端恢复只执行一次副作用
# 设计：第二次模型入口屏障证明 result 已提交；两客户端同时 resume 与同 request_id 重发共同覆盖持久去重和执行权抢占
async def test_recovery_reuses_committed_result_and_request_identity(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, "committed-result", "committed")
    _write_counter_script(daemon.workspace)
    await daemon.start()
    client = await daemon.connect("initial")
    try:
        session_id = await _create_session(client, daemon.workspace, "committed recovery")
        sent = await client.call(
            "session.send_message",
            {
                "session_id": session_id,
                "content": "increment once",
                "request_id": "stable-committed",
            },
        )
        run_id = str(sent["run_id"])
        await _allow_once(client)
        await daemon.wait_barrier("second-model-entry")
        await daemon.kill()

        await daemon.start()
        first = await daemon.connect("resume-a")
        second = await daemon.connect("resume-b")
        try:
            resumes = await asyncio.gather(
                first.call(
                    "session.resume",
                    {"session_id": session_id, "run_id": run_id, "workspace": str(daemon.workspace)},
                ),
                second.call(
                    "session.resume",
                    {"session_id": session_id, "run_id": run_id, "workspace": str(daemon.workspace)},
                ),
            )
            assert sum(bool(item.get("started")) for item in resumes) == 1, resumes
            duplicate = await first.call(
                "session.send_message",
                {
                    "session_id": session_id,
                    "content": "increment once",
                    "request_id": "stable-committed",
                },
            )
            assert duplicate["run_id"] == run_id, duplicate
            status = await _wait_status(
                first,
                session_id,
                lambda item: _status_parts(item, run_id)[0].get("status") == "succeeded",
            )
            run, calls, _children = _status_parts(status, run_id)
            assert run.get("status") == "succeeded", run
            assert len(calls) == 1, calls
            assert calls[0].get("status") == "succeeded", calls
            assert len(calls[0].get("attempts", [])) == 1, calls
            history = await first.call("session.get_history", {"session_id": session_id})
            tool_results = [
                block
                for message in history.get("messages", [])
                if isinstance(message.get("content"), list)
                for block in message["content"]
                if isinstance(block, dict) and block.get("type") == "tool_result"
            ]
            assert len(tool_results) == 1, history
            count, marker = _counter_state(daemon.workspace)
            assert (count, marker) == (1, "incremented")
            facts = _read_execution_facts(await daemon.execution_db())
            assert len(facts["calls"]) == 1, facts
            assert len(facts["attempts"]) == 1, facts
            assert facts["calls"][0]["status"] == "succeeded", facts
        finally:
            await first.close()
            await second.close()
    finally:
        await daemon.stop()


# 功能：验证副作用已经发生但 commit_result 前硬中断会持久化为 needs_review 且不重放
# 设计：commit_result wrapper 在真实脚本返回后暂停，重启后直接读取 call/attempt 数据并断言 counter 仍为一次
async def test_unknown_result_stays_reviewable_without_replay(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, "unknown-result", "unknown")
    _write_counter_script(daemon.workspace)
    await daemon.start()
    client = await daemon.connect("unknown-initial")
    try:
        session_id = await _create_session(client, daemon.workspace, "unknown result")
        sent = await client.call(
            "session.send_message",
            {
                "session_id": session_id,
                "content": "increment with uncertain commit",
                "request_id": "stable-unknown",
            },
        )
        run_id = str(sent["run_id"])
        await _allow_once(client)
        await daemon.wait_barrier("before-result-commit")
        count, marker = _counter_state(daemon.workspace)
        assert (count, marker) == (1, "incremented")
        await daemon.kill()

        await daemon.start()
        recovered = await daemon.connect("unknown-recovered")
        try:
            status = await _wait_status(
                recovered,
                session_id,
                lambda item: _status_parts(item, run_id)[0].get("status") == "needs_review",
            )
            run, calls, _children = _status_parts(status, run_id)
            assert run.get("status") == "needs_review", run
            assert calls and calls[0].get("status") == "unknown", calls
            assert calls[0].get("result") in (None, {}), calls
            assert len(calls[0].get("attempts", [])) == 1, calls
            assert calls[0]["attempts"][0].get("phase") == "dispatching", calls
            resume = await recovered.call(
                "session.resume",
                {"session_id": session_id, "run_id": run_id, "workspace": str(daemon.workspace)},
            )
            assert resume.get("started") is False, resume
            assert _counter_state(daemon.workspace) == (1, "incremented")
            facts = _read_execution_facts(await daemon.execution_db())
            assert facts["calls"][0]["status"] == "unknown", facts
            assert facts["attempts"][0]["phase"] == "dispatching", facts
        finally:
            await recovered.close()
    finally:
        await daemon.stop()


# 功能：验证一次性审批在 daemon 重启后失效并要求新的 approval_id 才能派发
# 设计：第一次审批后在 start_attempt 前硬中断，重启 resume 等待新事件，旧 token 必须拒绝且副作用只发生一次
async def test_restart_invalidates_one_time_approval(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, "approval", "approval")
    _write_counter_script(daemon.workspace)
    await daemon.start()
    client = await daemon.connect("approval-initial")
    try:
        session_id = await _create_session(client, daemon.workspace, "approval recovery")
        sent = await client.call(
            "session.send_message",
            {
                "session_id": session_id,
                "content": "approve one increment",
                "request_id": "stable-approval",
            },
        )
        run_id = str(sent["run_id"])
        old_approval = await _allow_once(client)
        await daemon.wait_barrier("before-start-attempt")
        await daemon.kill()

        await daemon.start()
        recovered = await daemon.connect("approval-recovered")
        try:
            resumed = await recovered.call(
                "session.resume",
                {"session_id": session_id, "run_id": run_id, "workspace": str(daemon.workspace)},
            )
            assert resumed.get("started") is True, resumed
            new_approval = await recovered.next_event("permission.requested", timeout=_STATUS_TIMEOUT)
            assert new_approval["approval_id"] != old_approval["approval_id"], new_approval
            if old_approval.get("daemon_epoch") is not None:
                assert new_approval.get("daemon_epoch") != old_approval.get("daemon_epoch")
            await _assert_old_approval_rejected(recovered, old_approval)
            await recovered.call(
                "permission.respond",
                {
                    "tool_use_id": new_approval["tool_use_id"],
                    "approval_id": new_approval["approval_id"],
                    "decision": "allow_once",
                },
            )
            status = await _wait_status(
                recovered,
                session_id,
                lambda item: _status_parts(item, run_id)[0].get("status") == "succeeded",
            )
            run, calls, _children = _status_parts(status, run_id)
            assert run.get("status") == "succeeded", run
            assert len(calls) == 1 and len(calls[0].get("attempts", [])) == 1, calls
            assert _counter_state(daemon.workspace) == (1, "incremented")
        finally:
            await recovered.close()
    finally:
        await daemon.stop()


# 功能：验证 workspace 绑定不匹配时恢复被拦截，工具 intent 未执行
# 设计：在 start_attempt 屏障后强杀 daemon，使用不同绝对路径恢复并检查目标文件和 attempt 均未被创建
async def test_workspace_mismatch_blocks_unstarted_write(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, "workspace", "workspace-mismatch")
    daemon.workspace.mkdir(parents=True, exist_ok=True)
    (daemon.workspace / "target.txt").write_text("original", encoding="utf-8")
    await daemon.start()
    client = await daemon.connect("workspace-initial")
    try:
        session_id = await _create_session(client, daemon.workspace, "workspace mismatch")
        sent = await client.call(
            "session.send_message",
            {
                "session_id": session_id,
                "content": "write target",
                "request_id": "stable-workspace-mismatch",
            },
        )
        run_id = str(sent["run_id"])
        await _allow_once(client)
        await daemon.wait_barrier("before-start-attempt")
        await daemon.kill()

        wrong_workspace = daemon.profile / "different-workspace"
        wrong_workspace.mkdir(parents=True, exist_ok=True)
        await daemon.start()
        recovered = await daemon.connect("workspace-recovered")
        try:
            result = await recovered.call(
                "session.resume",
                {"session_id": session_id, "run_id": run_id, "workspace": str(wrong_workspace)},
            )
            assert result.get("started") is False, result
            assert result.get("status") in {"needs_review", "interrupted"}, result
            status = await recovered.call("session.status", {"session_id": session_id})
            _run, calls, _children = _status_parts(status, run_id)
            assert calls and not calls[0].get("attempts"), calls
            assert (daemon.workspace / "target.txt").read_text(encoding="utf-8") == "original"
            facts = _read_execution_facts(await daemon.execution_db())
            assert facts["attempts"] == [], facts
        finally:
            await recovered.close()
    finally:
        await daemon.stop()


# 功能：验证恢复前相关文件被外部修改时写工具暂停且不覆盖用户内容
# 设计：沿用真实 write_file intent 的硬中断窗口，外部写入后重启恢复并核对内容、状态和 SQLite attempt
async def test_workspace_file_change_blocks_write(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, "workspace", "workspace-file")
    daemon.workspace.mkdir(parents=True, exist_ok=True)
    (daemon.workspace / "target.txt").write_text("original", encoding="utf-8")
    await daemon.start()
    client = await daemon.connect("file-initial")
    try:
        session_id = await _create_session(client, daemon.workspace, "workspace file conflict")
        sent = await client.call(
            "session.send_message",
            {
                "session_id": session_id,
                "content": "write target",
                "request_id": "stable-workspace-file",
            },
        )
        run_id = str(sent["run_id"])
        await _allow_once(client)
        await daemon.wait_barrier("before-start-attempt")
        await daemon.kill()
        (daemon.workspace / "target.txt").write_text("external-change", encoding="utf-8")

        await daemon.start()
        recovered = await daemon.connect("file-recovered")
        try:
            result = await recovered.call(
                "session.resume",
                {"session_id": session_id, "run_id": run_id, "workspace": str(daemon.workspace)},
            )
            assert result.get("started") is False, result
            assert result.get("status") == "needs_review", result
            assert (daemon.workspace / "target.txt").read_text(encoding="utf-8") == "external-change"
            facts = _read_execution_facts(await daemon.execution_db())
            assert facts["attempts"] == [], facts
        finally:
            await recovered.close()
    finally:
        await daemon.stop()


# 功能：验证后台 child run 在 daemon 重启后保留父子关系并显示中断，不自动重启整棵任务树
# 设计：child provider 在真实子任务模型入口屏障，强杀后检查持久 children 和 provider 调用计数不再增长
async def test_background_child_relationship_survives_restart(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, "child", "child-interrupted")
    await daemon.start()
    client = await daemon.connect("child-initial")
    try:
        session_id = await _create_session(client, daemon.workspace, "child recovery")
        sent = await client.call(
            "session.send_message",
            {
                "session_id": session_id,
                "content": "start child",
                "request_id": "stable-child",
            },
        )
        run_id = str(sent["run_id"])
        await _allow_once(client)
        await daemon.wait_barrier("child-model-entry")
        count_before = json.loads((daemon.profile / "provider-count.json").read_text())[
            "count"
        ]
        await daemon.kill()
        await daemon.start()
        recovered = await daemon.connect("child-recovered")
        try:
            status = await _wait_status(
                recovered,
                session_id,
                lambda item: bool(_status_parts(item, run_id)[2]),
            )
            _run, _calls, children = _status_parts(status, run_id)
            assert children, status
            assert all(child.get("status") in {"interrupted", "needs_review"} for child in children)
            count_after = json.loads((daemon.profile / "provider-count.json").read_text())["count"]
            assert count_after == count_before, (count_before, count_after)
        finally:
            await recovered.close()
    finally:
        await daemon.stop()


# 功能：验证后台 child 完成后新请求仍能通过持久 runner 查询真实 child tool_result
# 设计：父子完成后重启 daemon 再发送新 request_id，经新 Runner 查询 agent_result，核对持久工具结果文本
async def test_completed_child_result_is_queryable_across_runner(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, "child-complete", "child-complete")
    (daemon.profile / "child-complete.fast").parent.mkdir(parents=True, exist_ok=True)
    (daemon.profile / "child-complete.fast").write_text("fast", encoding="utf-8")
    await daemon.start()
    client = await daemon.connect("child-complete")
    try:
        session_id = await _create_session(client, daemon.workspace, "completed child")
        sent = await client.call(
            "session.send_message",
            {
                "session_id": session_id,
                "content": "start child",
                "request_id": "stable-child-complete",
            },
        )
        run_id = str(sent["run_id"])
        await _allow_once(client)
        status = await _wait_status(
            client,
            session_id,
            lambda item: (
                _status_parts(item, run_id)[0].get("status") == "succeeded"
                and any(child.get("status") == "succeeded"
                        for child in _status_parts(item, run_id)[2])
            ),
        )
        _run, _calls, children = _status_parts(status, run_id)
        assert children, status
        child = next((item for item in children if item.get("status") == "succeeded"), None)
        assert child is not None, children
        child_id = str(child.get("run_id", child.get("id", "")))
        assert child_id, child
        (daemon.profile / "query-child-id.txt").write_text(child_id, encoding="utf-8")

        await daemon.kill()
        await daemon.start()
        client = await daemon.connect("child-query-after-restart")

        second_sent = await client.call(
            "session.send_message",
            {
                "session_id": session_id,
                "content": f"query child {child_id}",
                "request_id": "stable-child-query",
            },
        )
        second_run_id = str(second_sent["run_id"])
        assert second_run_id != run_id
        await _allow_once(client)
        second_status = await _wait_status(
            client,
            session_id,
            lambda item: _status_parts(item, second_run_id)[0].get("status") == "succeeded",
        )
        second_run, _second_calls, _second_children = _status_parts(second_status, second_run_id)
        assert second_run.get("status") == "succeeded", second_status

        history = await client.call("session.get_history", {"session_id": session_id})
        tool_results = [
            block.get("content", "")
            for message in history.get("messages", [])
            if isinstance(message, dict) and isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        assert any("child complete" in str(content) for content in tool_results), history
    finally:
        await daemon.stop()


# 功能：验证同一 sessions root 上不同端口的第二 daemon 被持久 OS/数据库锁拒绝
# 设计：两个真实 CoreApp 子进程使用同一 profile 但不同 TCP 端口，第二个启动失败且第一个仍可 ping
async def test_second_daemon_same_profile_is_rejected(tmp_path: Path) -> None:
    profile = tmp_path / "shared-profile"
    workspace = tmp_path / "shared-workspace"
    first = DaemonProcess(profile=profile, workspace=workspace, scenario="committed-result")
    second = DaemonProcess(
        profile=profile,
        workspace=workspace,
        scenario="committed-result",
        port=None,
    )
    await first.start()
    try:
        first_client = await first.connect("owner")
        try:
            with pytest.raises(RuntimeError, match="daemon exited during startup"):
                await second.start()
            ping = await first_client.call("core.ping", {"client": "owner-check"})
            assert ping.get("server_version"), ping
        finally:
            await first_client.close()
    finally:
        await second.stop()
        await first.stop()
