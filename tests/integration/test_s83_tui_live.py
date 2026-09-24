from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, cast

import pytest
from textual.widgets import OptionList, Static

from kama_claude.core.config import KamaConfig
from kama_claude.core.transport.socket_client import SocketClient
from kama_claude.tui.app import KamaTuiApp
from kama_claude.tui.recovery import (
    RecoveryStatusScreen,
    SessionListScreen,
)
from tests.helpers.s83_daemon import RpcClient
from tests.integration.test_s83_recovery import (
    _allow_once,
    _counter_state,
    _create_session,
    _daemon,
    _read_execution_facts,
    _status_parts,
    _write_counter_script,
)

_POLL_SECONDS = 0.05
_STATUS_TIMEOUT = 45.0


# 返回本次 TUI 场景的独立截图和 JSONL 证据目录
def _artifact_root(tmp_path: Path) -> Path:
    root = Path(os.environ.get("KAMA_S83_ARTIFACTS", str(tmp_path / "artifacts")))
    root.mkdir(parents=True, exist_ok=True)
    return root


# 追加一条结构化 TUI 操作证据
def _record(path: Path, operation: str, **data: Any) -> None:
    row = {"ts": time.time(), "operation": operation, **data}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


# 用真实 SocketClient 包装 RPC 以保留请求、响应和错误证据
def _record_socket_commands(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    original = SocketClient.send_command

    # 记录真实 socket 请求和响应，不替代任何协议行为
    async def send_command(
        client: SocketClient,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        _record(path, "tui.rpc.request", method=method, params=params)
        try:
            result = await original(client, method, params)
        except Exception as exc:
            _record(
                path,
                "tui.rpc.error",
                method=method,
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            raise
        _record(path, "tui.rpc.response", method=method, result=result)
        return cast(dict[str, Any], result)

    monkeypatch.setattr(SocketClient, "send_command", send_command)


# 按真实 TUI 的公开 host/port 构造接口创建应用
def _make_tui_app(config: KamaConfig) -> Any:
    return KamaTuiApp(config.host, config.port)


# 等待 TUI 已建立真实 socket client
async def _wait_for_tui_client(app: Any, pilot: Any, *, timeout: float = _STATUS_TIMEOUT) -> SocketClient:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client = getattr(app, "_client", None)
        if isinstance(client, SocketClient) and app._session_id is not None:
            return client
        await pilot.pause(_POLL_SECONDS)
    raise AssertionError("TUI did not establish a SocketClient")


# 等待 Pilot 驱动的真实 Textual 屏幕切换
async def _wait_for_screen(app: Any, pilot: Any, screen_type: type[Any], *, timeout: float = _STATUS_TIMEOUT) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if isinstance(app.screen, screen_type):
            return app.screen
        await pilot.pause(_POLL_SECONDS)
    raise AssertionError(f"TUI did not show {screen_type.__name__}; current={app.screen!r}")


# 通过 OptionList 的真实键盘导航选中目标持久 session
async def _select_session(app: Any, pilot: Any, session_id: str) -> int:
    screen = await _wait_for_screen(app, pilot, SessionListScreen)
    options = screen.query_one("#session-options", OptionList)
    option_ids = [
        str(options.get_option_at_index(index).id)
        for index in range(options.option_count)
    ]
    assert session_id in option_ids, option_ids
    target_index = option_ids.index(session_id)
    current_index = options.highlighted or 0
    if target_index >= current_index:
        for _ in range(target_index - current_index):
            await pilot.press("down")
    else:
        for _ in range(current_index - target_index):
            await pilot.press("up")
    await pilot.press("enter")
    return target_index


# 轮询 session.status 直到指定 run 进入预期状态
async def _wait_run_status(
    client: RpcClient,
    session_id: str,
    run_id: str,
    expected: str,
    *,
    timeout: float = _STATUS_TIMEOUT,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = await client.call("session.status", {"session_id": session_id})
        run, _calls, _children = _status_parts(latest, run_id)
        if run.get("status") == expected:
            return latest
        await asyncio.sleep(_POLL_SECONDS)
    raise AssertionError(f"run {run_id} did not reach {expected}: {latest}")


# 等待恢复窗口被真实按钮动作关闭
async def _wait_for_screen_change(
    app: Any,
    pilot: Any,
    old_screen_type: type[Any],
    *,
    timeout: float = _STATUS_TIMEOUT,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not isinstance(app.screen, old_screen_type):
            return
        await pilot.pause(_POLL_SECONDS)
    raise AssertionError(f"TUI screen did not change from {old_screen_type.__name__}")


# 读取恢复窗口的 Rich 文本，确认全部事实在 UI 中可见
def _recovery_details(screen: RecoveryStatusScreen) -> str:
    widget = screen.query_one("#recovery-details-text", Static)
    content = widget.content
    return str(getattr(content, "plain", content))


# 读取持久 reviews 表中的人工说明
def _review_notes(database: Path, run_id: str) -> list[str]:
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM reviews WHERE run_id=? ORDER BY created_at",
            (run_id,),
        ).fetchall()
        return [str(row["note"]) for row in rows if row["note"] is not None]
    finally:
        connection.close()


# 保存带固定名称的 SVG 截图并核对 Textual 已输出 SVG 内容
def _save_svg(app: Any, root: Path, name: str, evidence: Path) -> Path:
    result = Path(app.save_screenshot(filename=name, path=str(root)))
    assert result.exists(), result
    assert result.read_text(encoding="utf-8").lstrip().startswith("<svg"), result
    _record(evidence, "tui.screenshot", screenshot_path=str(result), bytes=result.stat().st_size)
    return result


# 功能：验证真实 TUI 在 daemon 重启后通过 Ctrl+O 选择旧 session 并继续 committed run
# 设计：RPC 先把工具副作用推进到第二模型入口再强杀，Pilot 只走列表、RecoveryStatusScreen 按钮和真实 SocketClient，最终由持久状态与 SQLite 双重核对一次执行
@pytest.mark.asyncio  # type: ignore[untyped-decorator]
async def test_live_tui_resumes_committed_run_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = _daemon(tmp_path, "committed-result", "tui-committed")
    _write_counter_script(daemon.workspace)
    artifacts = _artifact_root(tmp_path)
    evidence = artifacts / "committed-operations.jsonl"
    _record(evidence, "scenario.start", scenario="committed-result")
    monkeypatch.chdir(daemon.workspace)
    _record_socket_commands(monkeypatch, evidence)
    await daemon.start()
    observer: RpcClient | None = None
    try:
        client = await daemon.connect("tui-committed-initial")
        session_id = await _create_session(client, daemon.workspace, "TUI committed recovery")
        sent = await client.call(
            "session.send_message",
            {
                "session_id": session_id,
                "content": "increment once",
                "request_id": "tui-stable-committed",
            },
        )
        run_id = str(sent["run_id"])
        await _allow_once(client)
        await daemon.wait_barrier("second-model-entry")
        assert _counter_state(daemon.workspace) == (1, "incremented")
        _record(evidence, "daemon.kill.request", run_id=run_id)
        await daemon.kill()
        await daemon.start()
        observer = await daemon.connect("tui-committed-observer")

        config = KamaConfig(host=daemon.host, port=int(daemon.port or 0))
        app = _make_tui_app(config)
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_tui_client(app, pilot)
                _save_svg(app, artifacts, "committed-before-session.svg", evidence)
                _record(evidence, "tui.key", key="ctrl+o")
                await pilot.press("ctrl+o")
                selected_index = await _select_session(app, pilot, session_id)
                _record(
                    evidence,
                    "tui.session.select",
                    session_id=session_id,
                    option_index=selected_index,
                )
                recovery = await _wait_for_screen(app, pilot, RecoveryStatusScreen)
                assert recovery.session_id == session_id
                _save_svg(app, artifacts, "committed-before-resume.svg", evidence)
                _record(evidence, "tui.button", selector="#recovery-resume")
                assert await pilot.click("#recovery-resume")
                await _wait_run_status(observer, session_id, run_id, "succeeded")
                await pilot.pause()
                _save_svg(app, artifacts, "committed-after-resume.svg", evidence)
                assert not isinstance(app.screen, RecoveryStatusScreen)
                await pilot.press("ctrl+q")
        finally:
            if getattr(app, "is_running", False):
                app.exit()

        facts = _read_execution_facts(await daemon.execution_db())
        assert len(facts["calls"]) == 1, facts
        assert len(facts["attempts"]) == 1, facts
        assert facts["calls"][0]["status"] == "succeeded", facts
        assert _counter_state(daemon.workspace) == (1, "incremented")
    finally:
        if observer is not None:
            await observer.close()
        await daemon.stop()


# 功能：验证真实 TUI 在 unknown result 场景展示完整核查事实并通过 Pause 持久化人工说明
# 设计：commit_result 前强杀保留一次真实副作用，Pilot 打开旧 session 的详情窗口、输入说明并点击 Pause，最后同时检查 UI、RPC 状态和 reviews 表
@pytest.mark.asyncio  # type: ignore[untyped-decorator]
async def test_live_tui_pauses_unknown_result_with_human_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = _daemon(tmp_path, "unknown-result", "tui-unknown")
    _write_counter_script(daemon.workspace)
    artifacts = _artifact_root(tmp_path)
    evidence = artifacts / "unknown-operations.jsonl"
    note = "reviewed external effect keep paused"
    _record(evidence, "scenario.start", scenario="unknown-result", note=note)
    monkeypatch.chdir(daemon.workspace)
    _record_socket_commands(monkeypatch, evidence)
    await daemon.start()
    observer: RpcClient | None = None
    try:
        client = await daemon.connect("tui-unknown-initial")
        session_id = await _create_session(client, daemon.workspace, "TUI unknown result")
        sent = await client.call(
            "session.send_message",
            {
                "session_id": session_id,
                "content": "increment with uncertain commit",
                "request_id": "tui-stable-unknown",
            },
        )
        run_id = str(sent["run_id"])
        await _allow_once(client)
        await daemon.wait_barrier("before-result-commit")
        assert _counter_state(daemon.workspace) == (1, "incremented")
        _record(evidence, "daemon.kill.request", run_id=run_id)
        await daemon.kill()
        await daemon.start()
        observer = await daemon.connect("tui-unknown-observer")

        config = KamaConfig(host=daemon.host, port=int(daemon.port or 0))
        app = _make_tui_app(config)
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_tui_client(app, pilot)
                _save_svg(app, artifacts, "unknown-before-session.svg", evidence)
                _record(evidence, "tui.key", key="ctrl+o")
                await pilot.press("ctrl+o")
                selected_index = await _select_session(app, pilot, session_id)
                _record(
                    evidence,
                    "tui.session.select",
                    session_id=session_id,
                    option_index=selected_index,
                )
                recovery = await _wait_for_screen(app, pilot, RecoveryStatusScreen)
                assert recovery.session_id == session_id
                details = _recovery_details(recovery)
                for expected in (
                    "command",
                    "call_id",
                    "attempt",
                    "workspace",
                    "counter.py",
                    "unknown",
                ):
                    assert expected in details, details
                for selector in (
                    "#recovery-details-text",
                    "#recovery-note",
                    "#recovery-pause",
                    "#recovery-abandon",
                    "#recovery-close",
                ):
                    recovery.query_one(selector)
                _save_svg(app, artifacts, "unknown-before-pause.svg", evidence)
                _record(evidence, "tui.input", selector="#recovery-note", value=note)
                assert await pilot.click("#recovery-note")
                await pilot.press(*list(note))
                _record(evidence, "tui.button", selector="#recovery-pause")
                assert await pilot.click("#recovery-pause")
                await _wait_for_screen_change(app, pilot, RecoveryStatusScreen)
                await _wait_run_status(observer, session_id, run_id, "needs_review")
                await pilot.pause()
                _save_svg(app, artifacts, "unknown-after-pause.svg", evidence)
                await pilot.press("ctrl+q")
        finally:
            if getattr(app, "is_running", False):
                app.exit()

        database = await daemon.execution_db()
        facts = _read_execution_facts(database)
        assert len(facts["calls"]) == 1, facts
        assert len(facts["attempts"]) == 1, facts
        assert facts["calls"][0]["status"] == "unknown", facts
        assert _counter_state(daemon.workspace) == (1, "incremented")
        assert note in _review_notes(database, run_id)
    finally:
        if observer is not None:
            await observer.close()
        await daemon.stop()
