from __future__ import annotations

import asyncio
from typing import Any

import pytest
from textual.widgets import Button, OptionList

from kama_claude.tui.app import KamaTuiApp, PermissionBlock, PermissionSelect
from kama_claude.tui.recovery import RecoveryStatusScreen, SessionListScreen


class _FakeClient:
    # 初始化可按方法返回预设结果并记录所有 RPC 调用
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # 记录 RPC 并返回 fake daemon 结果
    async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params.copy()))
        response = self.responses.get(method, {})
        if isinstance(response, list):
            response = response.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _PilotApp(KamaTuiApp):
    # Pilot 测试跳过真实 socket worker，只保留可交互的 TUI 组件
    def on_mount(self) -> None:
        self._slash_items = self._build_slash_items()

    # 覆盖基类 on_mount 可能启动的 socket worker，避免 fake client 被重置
    async def _socket_loop(self) -> None:
        await self._never_complete()

    # 保持测试 socket worker 挂起但不连接真实 daemon
    async def _never_complete(self) -> None:
        await asyncio.Event().wait()


# 生成会话列表响应，覆盖可恢复和普通等待中的持久会话
def _session_list_result(status: str = "waiting_for_input") -> dict[str, Any]:
    return {
        "sessions": [
            {
                "session_id": "sess-old",
                "title": "Old task",
                "status": status,
                "workspace": "C:/old",
                "current_run_id": "run-old",
                "run_status": "interrupted" if status == "interrupted" else "",
                "reason": "daemon stopped" if status == "interrupted" else None,
            },
            {
                "session_id": "sess-other",
                "title": "Other task",
                "status": "waiting_for_input",
                "workspace": "C:/other",
                "current_run_id": None,
                "run_status": None,
                "reason": None,
            },
        ]
    }


# 功能：验证 Ctrl+O 选择会话后当前 session 切换并加载完整历史
# 设计：使用真实 Pilot 键盘导航 OptionList，fake RPC 只提供列表和历史以隔离 daemon
@pytest.mark.asyncio
async def test_pilot_session_select_loads_history() -> None:
    fake = _FakeClient(
        {
            "session.list": _session_list_result(),
            "session.get_history": {"messages": [{"role": "user", "content": "selected history"}]},
        }
    )
    app = _PilotApp("127.0.0.1", 9999)
    app._client = fake  # type: ignore[assignment]

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert isinstance(app.screen, SessionListScreen)
        assert app.screen.query_one("#session-options", OptionList).option_count == 2
        await pilot.press("down", "enter")
        await pilot.pause()
        await pilot.pause()

    assert app._session_id == "sess-other"
    assert ("session.get_history", {"session_id": "sess-other"}) in fake.calls


# 功能：验证恢复窗口的 Continue 发送完整 workspace 和 run_id 并显示立即结果
# 设计：通过 Pilot 打开 ModalScreen 后按 Enter，断言 fake RPC 参数而非内部状态猜测
@pytest.mark.asyncio
async def test_pilot_resume_sends_workspace_and_run() -> None:
    fake = _FakeClient(
        {
            "session.status": {
                "session": {
                    "session_id": "sess-r",
                    "status": "interrupted",
                    "current_run_id": "run-r",
                    "reason": "daemon stopped",
                },
                "runs": [],
                "calls": [],
                "reviews": [],
                "children": [],
            },
            "session.resume": {
                "run_id": "run-r",
                "status": "running",
                "started": True,
                "reason": None,
            },
        }
    )
    app = _PilotApp("127.0.0.1", 9999)
    app._client = fake  # type: ignore[assignment]
    app._session_id = "sess-r"

    async with app.run_test(size=(100, 30)) as pilot:
        await app._show_session_status()
        await pilot.pause()
        assert isinstance(app.screen, RecoveryStatusScreen)
        await pilot.click("#recovery-resume")
        await pilot.pause()
        await pilot.pause()

    resume_calls = [params for method, params in fake.calls if method == "session.resume"]
    assert resume_calls
    assert resume_calls[0]["session_id"] == "sess-r"
    assert resume_calls[0]["run_id"] == "run-r"
    assert resume_calls[0]["workspace"]
    assert app._busy


# 功能：验证恢复响应晚于 run.finished 时不会把已结束运行重新标记为 busy
# 设计：fake RPC 在返回前投递终态事件，直接覆盖响应与事件竞态而不依赖固定睡眠
@pytest.mark.asyncio
async def test_resume_response_does_not_overwrite_early_terminal_event() -> None:
    app = _PilotApp("127.0.0.1", 9999)
    app._append = lambda _widget: None  # type: ignore[method-assign]
    app._update_header = lambda _state: None  # type: ignore[method-assign]

    class _EventFirstClient(_FakeClient):
        # 在恢复 RPC 响应前模拟 daemon 已经发布 run.finished
        async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((method, params.copy()))
            if method == "session.resume":
                app._handle_event(
                    {"type": "run.finished", "run_id": "run-race", "status": "success"}
                )
                return {
                    "run_id": "run-race",
                    "status": "running",
                    "started": True,
                    "reason": None,
                }
            return {}

    fake = _EventFirstClient()
    app._client = fake  # type: ignore[assignment]

    await app._resume_session("sess-race", "run-race")

    assert "run-race" in app._terminal_run_ids
    assert not app._busy


# 功能：验证 needs_review 核查窗口完整展示调用参数、工作区、call ID 和 attempts
# 设计：使用含 Rich 标记和长参数的 payload，读取 Text.plain 确认没有摘要截断或 markup 注入
@pytest.mark.asyncio
async def test_pilot_needs_review_shows_complete_call_details() -> None:
    payload = {
        "session": {
            "session_id": "sess-u",
            "status": "needs_review",
            "current_run_id": "run-u",
            "reason": "tool outcome unknown",
        },
        "runs": [],
        "calls": [
            {
                "call_id": "call-123",
                "tool_use_id": "tool-123",
                "name": "bash",
                "input": {"command": "echo [bold]unsafe[/bold] and a very long value"},
                "effect": "unknown",
                "workspace": "C:/workspace/project",
                "status": "dispatching",
                "result": None,
                "attempts": [{"attempt": 1, "known_fact": "process may have started"}],
            }
        ],
        "reviews": [],
        "children": [],
    }
    app = _PilotApp("127.0.0.1", 9999)

    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(RecoveryStatusScreen(payload, "sess-u", "run-u"), app._on_recovery_action)
        await pilot.pause()
        details = app.screen.query_one("#recovery-details-text").content
        assert hasattr(details, "plain")
        plain = details.plain  # type: ignore[union-attr]
        assert "call-123" in plain
        assert "echo [bold]unsafe[/bold] and a very long value" in plain
        assert "C:/workspace/project" in plain
        assert "known_fact" in plain


# 功能：验证 pause 与 abandon 都要求说明并将完整 action 参数提交给 daemon
# 设计：复用两个独立状态窗口走实际按钮路径，先验证空说明被安全拦截，再验证显式操作
@pytest.mark.asyncio
async def test_pilot_review_pause_and_abandon_require_note() -> None:
    fake = _FakeClient(
        {
            "session.review": {
                "run_id": "run-u",
                "status": "needs_review",
                "started": False,
                "reason": "recorded",
            }
        }
    )
    payload = {
        "session": {"session_id": "sess-u", "status": "needs_review", "current_run_id": "run-u"},
        "runs": [],
        "calls": [],
        "reviews": [],
        "children": [],
    }
    app = _PilotApp("127.0.0.1", 9999)
    app._client = fake  # type: ignore[assignment]
    app._session_id = "sess-u"

    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(RecoveryStatusScreen(payload, "sess-u", "run-u"), app._on_recovery_action)
        await pilot.pause()
        await pilot.click("#recovery-pause")
        await pilot.pause()
        assert isinstance(app.screen, RecoveryStatusScreen)
        note = app.screen.query_one("#recovery-note")
        note.value = "User reviewed unknown side effect; keep paused"
        app.screen.on_button_pressed(
            Button.Pressed(app.screen.query_one("#recovery-pause", Button))
        )
        await pilot.pause()
        await pilot.pause()
        assert not isinstance(app.screen, RecoveryStatusScreen)

        app.push_screen(RecoveryStatusScreen(payload, "sess-u", "run-u"), app._on_recovery_action)
        await pilot.pause()
        note = app.screen.query_one("#recovery-note")
        note.value = "Abandon after explicit review"
        app.screen.on_button_pressed(
            Button.Pressed(app.screen.query_one("#recovery-abandon", Button))
        )
        await pilot.pause()
        await pilot.pause()

    review_calls = [params for method, params in fake.calls if method == "session.review"]
    assert {call["action"] for call in review_calls} == {"pause", "abandon"}
    assert all(call["note"] for call in review_calls)


# 功能：验证传输失败重试复用 request_id，成功发送和新输入则生成新的 ID
# 设计：直接调用发送 worker 的实际 fake RPC 路径，故障只发生一次且不会依赖临时 JSON-RPC ID
@pytest.mark.asyncio
async def test_request_id_is_stable_across_transport_retry() -> None:
    fake = _FakeClient({"session.send_message": [ConnectionError("disconnected"), {"run_id": "r"}, {"run_id": "r2"}]})
    app = _PilotApp("127.0.0.1", 9999)
    app._client = fake  # type: ignore[assignment]
    app._session_id = "sess-id"
    app._append = lambda _widget: None  # type: ignore[method-assign]
    app._update_header = lambda _state: None  # type: ignore[method-assign]

    await app._do_send_message("same input", app._request_id_for_content("same input"))
    failed_id = fake.calls[-1][1]["request_id"]
    await app._do_send_message("same input", app._request_id_for_content("same input"))
    retried_id = fake.calls[-1][1]["request_id"]
    await app._do_send_message("new input", app._request_id_for_content("new input"))
    new_id = fake.calls[-1][1]["request_id"]

    assert failed_id == retried_id
    assert retried_id != new_id
    assert app._retry_request_id is None


# 功能：验证新的审批 approval_id 和 daemon_epoch 被原样回传
# 设计：投递带新字段的 permission.requested 事件，再用 Pilot 的 y 快捷键触发真实审批消息
@pytest.mark.asyncio
async def test_permission_response_forwards_approval_identity() -> None:
    fake = _FakeClient()
    app = _PilotApp("127.0.0.1", 9999)
    app._client = fake  # type: ignore[assignment]
    app._append = lambda widget: app.query_one("#log-view").mount(widget)  # type: ignore[method-assign]

    async with app.run_test(size=(100, 30)) as pilot:
        app._handle_event(
            {
                "type": "permission.requested",
                "tool_use_id": "old-tool-id",
                "approval_id": "approval-new",
                "daemon_epoch": "daemon-new",
                "tool_name": "bash",
                "param_preview": "echo hi",
            }
        )
        await pilot.pause()
        assert "approval-new" in app._pending_permission_blocks
        await pilot.press("y")
        await pilot.pause()
        await pilot.pause()

    response_calls = [params for method, params in fake.calls if method == "permission.respond"]
    assert response_calls == [
        {
            "tool_use_id": "old-tool-id",
            "decision": "allow_once",
            "approval_id": "approval-new",
            "daemon_epoch": "daemon-new",
        }
    ]
    assert not app.query(PermissionSelect)


# 功能：验证 permission.respond 返回 ok=False 时不会先显示 allowed，并触发状态刷新
# 设计：真实 Pilot 走审批快捷键，fake daemon 返回过期响应；断言控件是失败态且状态 RPC 被调用
@pytest.mark.asyncio
async def test_permission_response_failure_does_not_show_allowed_and_refreshes_status() -> None:
    fake = _FakeClient(
        {
            "permission.respond": {"ok": False, "reason": "approval expired"},
            "session.status": {
                "session": {
                    "session_id": "sess-u",
                    "status": "needs_review",
                    "current_run_id": "run-u",
                    "reason": "approval expired",
                },
                "runs": [],
                "calls": [],
                "reviews": [],
                "children": [],
            },
        }
    )
    app = _PilotApp("127.0.0.1", 9999)
    app._client = fake  # type: ignore[assignment]
    app._session_id = "sess-u"
    app._append = lambda widget: app.query_one("#log-view").mount(widget)  # type: ignore[method-assign]

    async with app.run_test(size=(100, 30)) as pilot:
        app._handle_event(
            {
                "type": "permission.requested",
                "session_id": "sess-u",
                "run_id": "run-u",
                "tool_use_id": "tool-expired",
                "approval_id": "approval-expired",
                "tool_name": "bash",
                "param_preview": "echo hi",
            }
        )
        await pilot.pause()
        block = app.query_one(PermissionBlock)
        prompt = app._prompt()
        assert prompt is not None
        await pilot.press("y")
        await pilot.pause()
        await pilot.pause()

        assert block._resolved  # type: ignore[attr-defined]
        assert "request expired" in str(block.content).lower()
        assert "allowed" not in str(block.content).lower()
        assert prompt.disabled
        assert "expired" in prompt.border_title.lower()
        assert any(method == "session.status" for method, _ in fake.calls)


# 功能：验证带旧 approval_id 的 granted/denied 事件不会清理同 tool 的新审批
# 设计：不依赖挂载 UI，直接投递两类旧 token 事件并检查新审批仍待处理
def test_stale_permission_token_cannot_resolve_new_request() -> None:
    app = _PilotApp("127.0.0.1", 9999)
    app._session_id = "sess-u"
    block = PermissionBlock(
        "shared-tool",
        "bash",
        "echo new",
        "approval-new",
        "epoch-new",
    )
    app._pending_permission_blocks["approval-new"] = block
    app._append = lambda _widget: None  # type: ignore[method-assign]

    for event_type in ("permission.denied", "permission.granted"):
        app._handle_event(
            {
                "type": event_type,
                "session_id": "sess-u",
                "run_id": "run-u",
                "tool_use_id": "shared-tool",
                "approval_id": "approval-old",
                "decision": "deny_once" if event_type.endswith("denied") else "allow_once",
            }
        )

    assert app._permission_key("shared-tool", "approval-old") is None
    assert app._pending_permission_blocks == {"approval-new": block}
    assert not block._resolved  # type: ignore[attr-defined]


# 功能：验证没有 session_id 的未知 global run.started 不会被 busy 状态猜成当前运行
# 设计：selected_run_id 为空时投递未知 run，确认事件被过滤且不会建立错误归属
def test_unknown_global_run_started_is_not_claimed_by_busy_session() -> None:
    app = _PilotApp("127.0.0.1", 9999)
    app._session_id = "sess-u"
    app._busy = True
    appended: list[Any] = []
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]

    app._handle_event(
        {"type": "run.started", "run_id": "foreign-run", "goal": "other session"}
    )

    assert app._selected_run_id is None
    assert appended == []

    app._handle_event(
        {
            "type": "run.started",
            "session_id": "sess-u",
            "run_id": "current-run",
            "goal": "selected session",
        }
    )
    assert app._selected_run_id == "current-run"
