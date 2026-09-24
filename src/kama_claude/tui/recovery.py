from __future__ import annotations

import json
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option


# 将任意 RPC 数据编码为完整且可审阅的 JSON 文本
def format_recovery_data(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)


# 将用户或后端文本包装为禁用 Rich markup 的安全文本
def safe_text(value: Any) -> Text:
    return Text(str(value))


# 生成会话选择列表中的单行标签
def format_session_label(session: dict[str, Any]) -> str:
    session_id = str(session.get("session_id", ""))
    title = str(session.get("title") or session_id)
    status = str(session.get("status") or "unknown")
    run_status = str(session.get("run_status") or "")
    suffix = f"  run={run_status}" if run_status else ""
    return f"{title}  [{status}]  {session_id}{suffix}"


class SessionListScreen(ModalScreen[dict[str, Any] | None]):
    """持久会话选择屏幕。"""

    DEFAULT_CSS = """
    SessionListScreen { align: center middle; background: $background 85%; }
    #session-list-dialog {
        width: 92%; max-width: 100; height: 80%; min-height: 12;
        border: round $accent; background: $surface; padding: 1 2;
    }
    #session-options { height: 1fr; margin: 1 0; }
    #session-list-actions { height: auto; align-horizontal: right; }
    #session-list-actions Button { margin-left: 1; }
    """

    # 初始化会话数据和当前会话标记
    def __init__(
        self,
        sessions: list[dict[str, Any]],
        current_session_id: str | None = None,
    ) -> None:
        super().__init__()
        self._sessions = sessions
        self._current_session_id = current_session_id

    # 组合会话标题、可滚动选项和关闭按钮
    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Persistent sessions", id="session-list-title"),
            OptionList(id="session-options", markup=False),
            Horizontal(
                Button("Close", id="session-list-close"),
                id="session-list-actions",
            ),
            id="session-list-dialog",
        )

    # 将后端会话摘要填入 OptionList 并聚焦列表
    def on_mount(self) -> None:
        options = self.query_one("#session-options", OptionList)
        for session in self._sessions:
            session_id = str(session.get("session_id", ""))
            if not session_id:
                continue
            label = Text(format_session_label(session))
            options.add_option(Option(label, id=session_id))
        if options.option_count:
            current_index = next(
                (
                    index
                    for index, session in enumerate(self._sessions)
                    if session.get("session_id") == self._current_session_id
                ),
                0,
            )
            options.highlighted = min(current_index, options.option_count - 1)
            options.focus()
        else:
            self.query_one("#session-list-title", Label).update("Persistent sessions (none found)")

    # 选择会话后把摘要交给宿主 App 加载完整历史
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.dismiss({"session_id": event.option.id})

    # 关闭列表屏幕而不切换当前会话
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "session-list-close":
            self.dismiss(None)

    # Escape 关闭会话列表，保持键盘导航可用
    def on_key(self, event: Any) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)


class RecoveryStatusScreen(ModalScreen[dict[str, Any] | None]):
    """显示完整运行核查信息并接收恢复决策。"""

    DEFAULT_CSS = """
    RecoveryStatusScreen { align: center middle; background: $background 85%; }
    #recovery-dialog {
        width: 96%; max-width: 120; height: 92%; min-height: 16;
        border: round $warning; background: $surface; padding: 1 2;
    }
    #recovery-summary { width: 1fr; height: auto; color: $text; }
    #recovery-guidance { width: 1fr; height: auto; color: $warning; margin-top: 1; }
    #recovery-details { height: 1fr; margin: 1 0; border: round $surface-lighten-2; padding: 0 1; }
    #recovery-note { margin: 0 0 1 0; }
    #recovery-actions { height: auto; }
    #recovery-actions-primary, #recovery-actions-secondary {
        height: auto; align-horizontal: right;
    }
    #recovery-actions Button { margin-left: 1; }
    """

    # 初始化完整状态负载以及默认运行和会话标识
    def __init__(
        self,
        payload: dict[str, Any],
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        super().__init__()
        self.payload = payload
        self.session_id = session_id or str(payload.get("session_id") or "")
        session = payload.get("session")
        if isinstance(session, dict):
            self.session_id = self.session_id or str(session.get("session_id") or "")
        self.run_id = run_id or str(
            payload.get("run_id")
            or (session.get("current_run_id") if isinstance(session, dict) else "")
            or ""
        )

    # 组合状态摘要、完整 JSON 核查内容、说明输入和显式操作按钮
    def compose(self) -> ComposeResult:
        session = self.payload.get("session")
        status = session.get("status") if isinstance(session, dict) else self.payload.get("status")
        reason = session.get("reason") if isinstance(session, dict) else self.payload.get("reason")
        run_status = self._run_status(session)
        uncertain = self._has_uncertain_call(run_status)
        summary = f"Session {self.session_id or '(unknown)'}  status={status or 'unknown'}"
        if run_status:
            summary += f"  run_status={run_status}"
        if self.run_id:
            summary += f"  run={self.run_id}"
        if reason:
            summary += f"  reason={reason}"
        guidance = self._guidance(run_status, reason, uncertain)
        yield Vertical(
            Label(safe_text(summary), id="recovery-summary"),
            Label(safe_text(guidance), id="recovery-guidance"),
            VerticalScroll(
                Static(
                    safe_text(format_recovery_data(self.payload)),
                    markup=False,
                    id="recovery-details-text",
                ),
                id="recovery-details",
            ),
            Input(
                placeholder="Human explanation required for pause or abandon",
                id="recovery-note",
            ),
            Vertical(
                Horizontal(
                    Button("Continue", id="recovery-resume", variant="primary", disabled=uncertain),
                    Button("Pause", id="recovery-pause"),
                    id="recovery-actions-primary",
                ),
                Horizontal(
                    Button("Abandon", id="recovery-abandon", variant="error"),
                    Button("Close", id="recovery-close"),
                    id="recovery-actions-secondary",
                ),
                id="recovery-actions",
            ),
            id="recovery-dialog",
        )

    # 从会话、当前 run 或调用记录解析最具体的运行状态
    def _run_status(self, session: Any) -> str:
        if isinstance(session, dict):
            value = session.get("run_status")
            if value:
                return str(value)
        runs = self.payload.get("runs")
        if isinstance(runs, list):
            for run in runs:
                if isinstance(run, dict) and str(run.get("run_id") or "") == self.run_id:
                    value = run.get("status") or run.get("run_status")
                    if value:
                        return str(value)
        calls = self.payload.get("calls")
        if isinstance(calls, list):
            statuses = {str(call.get("status") or "") for call in calls if isinstance(call, dict)}
            for value in ("unknown", "dispatching", "needs_review"):
                if value in statuses:
                    return value
        return str(self.payload.get("run_status") or "")

    # 判断当前核查是否含有不允许自动重放的调用
    def _has_uncertain_call(self, run_status: str) -> bool:
        if run_status in {"unknown", "dispatching"}:
            return True
        calls = self.payload.get("calls")
        if not isinstance(calls, list):
            return False
        return any(
            isinstance(call, dict)
            and str(call.get("status") or "") in {"unknown", "dispatching"}
            for call in calls
        )

    # 生成明确说明，帮助用户区分暂停、继续和放弃的副作用语义
    def _guidance(self, run_status: str, reason: Any, uncertain: bool) -> str:
        reason_text = str(reason or "").lower()
        if "workspace" in reason_text or "conflict" in reason_text:
            return (
                "Workspace or file conflict detected. Restore the original directory/files, "
                "then try again. Pause preserves this review. Abandon closes the session "
                "without rolling back side effects."
            )
        if uncertain or run_status in {"unknown", "dispatching"}:
            return (
                "Execution may have started but has no reliable result; "
                "do not replay automatically. "
                "Pause preserves needs_review and this explanation. Abandon closes the session "
                "without rolling back side effects."
            )
        return (
            "Pause preserves the review record. Abandon closes the session without rolling back "
            "side effects."
        )

    # 首次显示时把焦点放在继续前必须阅读的核查内容上
    def on_mount(self) -> None:
        self.query_one("#recovery-details", VerticalScroll).focus()

    # 将按钮动作转成宿主 App 可执行的恢复或审查结果
    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "recovery-close":
            self.dismiss(None)
            return
        action = {
            "recovery-resume": "resume",
            "recovery-pause": "pause",
            "recovery-abandon": "abandon",
        }.get(button_id)
        if action is None:
            return
        note = self.query_one("#recovery-note", Input).value.strip()
        if action in {"pause", "abandon"} and not note:
            note_input = self.query_one("#recovery-note", Input)
            note_input.border_title = "explanation required"
            note_input.focus()
            return
        self.dismiss({
            "action": action,
            "note": note,
            "session_id": self.session_id,
            "run_id": self.run_id,
        })

    # Enter 继续当前高亮按钮，Escape 关闭核查窗口
    def on_key(self, event: Any) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)
