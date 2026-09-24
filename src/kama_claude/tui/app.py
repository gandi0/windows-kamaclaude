from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.markup import escape
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, Static, TextArea

from kama_claude.core.config import KamaConfig
from kama_claude.core.skills.loader import SkillLoader
from kama_claude.core.transport.socket_client import IpcError, SocketClient
from kama_claude.tui.recovery import RecoveryStatusScreen, SessionListScreen, safe_text

log = logging.getLogger(__name__)


def _preview(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s




def _params_str(params: dict[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False, indent=2)


# 从工具参数中提取最适合摘要展示的关键字段
def _param_summary(tool_name: str, params: dict[str, Any], max_len: int = 72) -> str:
    keys_by_tool = {
        "read_file": ("path",),
        "write_file": ("path",),
        "list_dir": ("path", "max_depth"),
        "bash": ("command",),
        "note_save": ("content",),
    }
    keys = keys_by_tool.get(tool_name, ())
    parts = [f"{key}={params[key]!r}" for key in keys if key in params]
    if not parts:
        parts = [f"{key}={value!r}" for key, value in list(params.items())[:2]]
    return _preview(", ".join(parts), max_len)


class LLMStreamBlock(Static):
    """在同一个 Static widget 中累积 LLM 流式 token。"""

    DEFAULT_CSS = "LLMStreamBlock { padding: 0 2; color: $text; }"

    # 初始化为空文本块
    def __init__(self) -> None:
        super().__init__("")
        self._text = ""
        self._finalized = False

    # 追加一个 token 并刷新显示
    def append_token(self, token: str) -> None:
        if self._finalized:
            return
        self._text += token
        self.update(self._text)

    # 将累积文本渲染为 Markdown，供流式块结束后显示
    def finalize_markdown(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self._text.strip():
            self.update(Markdown(self._text, code_theme="monokai"))


class ToolCallBlock(Widget):
    """可折叠的工具调用块：折叠时显示摘要，点击后展开完整 params 和 output。"""

    DEFAULT_CSS = """
    ToolCallBlock { height: auto; padding: 0 2; color: $text-muted; }
    ToolCallBlock > .summary { color: $text-muted; }
    ToolCallBlock > .detail { display: none; padding: 0 2 0 4; color: $text-muted; }
    ToolCallBlock.expanded > .detail { display: block; }
    """

    # 初始化工具调用信息
    def __init__(self, tool_name: str, params: dict[str, Any]) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._params = params
        self._params_full = _params_str(params)
        self._output = ""
        self._elapsed_ms = 0
        self._is_error = False
        self._finished = False

    def compose(self) -> ComposeResult:
        yield Static(self._summary(), classes="summary")
        yield Static("", classes="detail")

    # 生成摘要行文本
    def _summary(self) -> str:
        if self._tool_name == "note_save" and self._finished and not self._is_error:
            return f"  [green]remembered[/green]  [dim]{self._elapsed_ms}ms[/dim]"

        params_pre = _param_summary(self._tool_name, self._params)
        line = f"  [dim]tool[/dim] [bold]{self._tool_name}[/bold]"
        if params_pre:
            line += f"  [dim]{params_pre}[/dim]"
        if self._finished:
            color = "red" if self._is_error else "green"
            status = "failed" if self._is_error else "done"
            hint = "  [dim](click to expand)[/dim]" if self._output else ""
            line += f"  [{color}]{status}[/{color}]  [dim]{self._elapsed_ms}ms[/dim]{hint}"
        return line

    # 工具调用完成时更新结果并刷新摘要（widget 未挂载时跳过 DOM 更新）
    def set_result(self, output: str, elapsed_ms: int, *, is_error: bool = False) -> None:
        self._output = output
        self._elapsed_ms = elapsed_ms
        self._is_error = is_error
        self._finished = True
        if self.children:
            self.query_one(".summary", Static).update(self._summary())

    # 点击时切换展开/折叠状态
    def on_click(self) -> None:
        if not self._finished:
            return
        if "expanded" in self.classes:
            self.remove_class("expanded")
        else:
            detail = self.query_one(".detail", Static)
            detail.update(
                f"[dim]params[/dim]\n{self._params_full}\n\n"
                f"[dim]output[/dim]\n{self._output}\n\n"
                f"[dim]elapsed:[/dim] {self._elapsed_ms}ms"
            )
            self.add_class("expanded")


class PermissionSelect(Static):
    """内联权限选择控件：挂载在日志流中，键盘焦点无需 ModalScreen。"""

    can_focus = True

    DEFAULT_CSS = """
    PermissionSelect {
        height: auto;
        padding: 0 2;
        margin-bottom: 1;
    }
    """

    _CHOICES: tuple[tuple[str, str, str], ...] = (
        ("allow_once",   "Allow once",   "y / 1"),
        ("always_allow", "Always allow", "a / 2"),
        ("deny_once",    "Deny",         "n / 3"),
        ("always_deny",  "Always deny",  "d / 4"),
    )
    _KEY_MAP: dict[str, str] = {
        "y": "allow_once",  "1": "allow_once",
        "a": "always_allow","2": "always_allow",
        "n": "deny_once",   "3": "deny_once",
        "d": "always_deny", "4": "always_deny",
    }

    # 用户作出权限决策时发布，携带工具 ID 和决策字符串
    class Decided(Message):
        # 初始化决策消息，存储控件引用、工具 ID 和决策
        def __init__(
            self,
            widget: PermissionSelect,
            tool_use_id: str,
            decision: str,
            approval_id: str | None = None,
            daemon_epoch: str | None = None,
        ) -> None:
            self.widget = widget
            self.tool_use_id = tool_use_id
            self.decision = decision
            self.approval_id = approval_id
            self.daemon_epoch = daemon_epoch
            super().__init__()

    # 初始化控件，存储审批 ID 和工具 ID 以防旧 daemon 请求串线
    def __init__(
        self,
        tool_use_id: str,
        approval_id: str | None = None,
        daemon_epoch: str | None = None,
    ) -> None:
        super().__init__("")
        self._tool_use_id = tool_use_id
        self._approval_id = approval_id
        self._daemon_epoch = daemon_epoch
        self._cursor = 0

    def on_mount(self) -> None:
        self.update(self._render_ui())
        self.focus()
        log.debug(
            "PermissionSelect.on_mount  can_focus=%s  focused_after=%r",
            self.can_focus,
            self.app.focused,
        )
        self.app.call_after_refresh(self._log_deferred_focus)

    # 在下一帧记录焦点是否真正转移到本控件
    def _log_deferred_focus(self) -> None:
        log.debug(
            "PermissionSelect.deferred_focus  app.focused=%r  has_focus=%s  focusable=%s",
            self.app.focused,
            self.has_focus,
            self.focusable,
        )

    # 焦点到达时记录，用于确认 focus() 是否真正生效
    def on_focus(self, event: events.Focus) -> None:
        log.debug("PermissionSelect.on_focus  has_focus=%s  app.focused=%r", self.has_focus, self.app.focused)

    # 焦点离开时记录，用于追踪是否被其他控件抢走焦点
    def on_blur(self, event: events.Blur) -> None:
        log.debug("PermissionSelect.on_blur  app.focused=%r", self.app.focused)

    # 生成带光标高亮的选项列表文本
    def _render_ui(self) -> str:
        lines: list[str] = []
        for i, (_, label, key_hint) in enumerate(self._CHOICES):
            if i == self._cursor:
                lines.append(f"  [bold cyan]❯ {label}[/bold cyan]  [dim]{key_hint}[/dim]")
            else:
                lines.append(f"    {label}  [dim]{key_hint}[/dim]")
        lines.append("[dim]  ↑↓ navigate   enter confirm[/dim]")
        return "\n".join(lines)

    # 方向键导航；快捷键直接选择；enter 确认光标位置
    def on_key(self, event: events.Key) -> None:
        log.debug("PermissionSelect.on_key  key=%r  char=%r", event.key, event.character)
        key = event.key
        if key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key == "enter":
            event.stop()
            self._pick(self._CHOICES[self._cursor][0])
        else:
            decision = self._KEY_MAP.get(key)
            if decision is not None:
                event.stop()
                self._pick(decision)

    # 发布决策消息，由宿主 App 负责 IPC 回复和控件清理
    def _pick(self, decision: str) -> None:
        log.debug("PermissionSelect._pick  decision=%s", decision)
        self.post_message(
            self.Decided(
                self,
                self._tool_use_id,
                decision,
                self._approval_id,
                self._daemon_epoch,
            )
        )


class PermissionBlock(Static):
    """日志里的权限审批摘要"""

    _LABEL_MAP: dict[str, str] = {
        "allow_once":   "allowed (once)",
        "always_allow": "always allowed",
        "deny_once":    "denied",
        "always_deny":  "always denied",
        "auto_allow":   "allowed automatically",
        "auto_deny":    "denied automatically",
        "timeout":      "⏱ timed out",
        "expired":      "request expired",
        "failed":       "response failed",
    }
    LABEL_MAP = _LABEL_MAP

    # 子类提交消息：用户作出权限决策时发布
    class Resolved(Message):
        def __init__(self, block: PermissionBlock, decision: str) -> None:
            self.block = block
            self.decision = decision
            super().__init__()

    # 初始化审批块，记录工具 ID、审批 ID、名称和参数预览
    def __init__(
        self,
        tool_use_id: str,
        tool_name: str,
        param_preview: str,
        approval_id: str | None = None,
        daemon_epoch: str | None = None,
    ) -> None:
        self._tool_use_id = tool_use_id
        self._tool_name = tool_name
        self._param_preview = param_preview
        self._approval_id = approval_id
        self._daemon_epoch = daemon_epoch
        self._resolved = False
        super().__init__(self._pending_text(), classes="log-line")

    def _pending_text(self) -> str:
        preview = f"  [dim]{self._param_preview}[/dim]" if self._param_preview else ""
        return f"[bold red]? permission[/bold red]  [bold]{self._tool_name}[/bold]{preview}"

    # 将块收缩为单行摘要并发布 Resolved 消息
    def _resolve(self, decision: str) -> None:
        if self._resolved:
            return
        self._resolved = True
        allowed = decision in ("allow_once", "always_allow", "auto_allow")
        icon = "[bold green]✓[/bold green]" if allowed else "[bold red]✗[/bold red]"
        label = self._LABEL_MAP.get(decision, decision)
        preview = f"  [dim]{self._param_preview}[/dim]" if self._param_preview else ""
        self.update(
            f"{icon} permission  [bold]{self._tool_name}[/bold]{preview}  [dim]{label}[/dim]"
        )
        self.post_message(self.Resolved(self, decision))


class SlashCompleteWidget(Static):
    """斜杠命令自动补全弹出框：输入 / 时显示可用 skill 列表并支持键盘筛选与选择。"""

    can_focus = False

    DEFAULT_CSS = """
    SlashCompleteWidget {
        height: auto;
        padding: 0 1;
        margin: 0 2;
        background: $surface;
        border: round $surface-lighten-2;
    }
    """

    # 用户选中某条命令时发布
    class Selected(Message):
        # 初始化，携带被选中的 skill 名称
        def __init__(self, skill_name: str) -> None:
            self.skill_name = skill_name
            super().__init__()

    # 初始化，接收全量 (name, description) 列表
    def __init__(self, items: list[tuple[str, str]]) -> None:
        super().__init__("")
        self._all_items = items
        self._filtered: list[tuple[str, str]] = list(items)
        self._cursor = 0

    # 根据查询字符串筛选列表，重置光标并重新渲染
    def set_query(self, query: str) -> None:
        q = query.lower()
        self._filtered = [(n, d) for n, d in self._all_items if not q or q in n.lower()]
        self._cursor = min(self._cursor, max(0, len(self._filtered) - 1))
        if self.is_attached:
            self._redraw()

    # 向上移动光标并重新渲染
    def move_up(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor - 1) % len(self._filtered)
            self._redraw()

    # 向下移动光标并重新渲染
    def move_down(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor + 1) % len(self._filtered)
            self._redraw()

    # 选中当前光标项并发布 Selected 消息
    def select_current(self) -> None:
        if self._filtered:
            self.post_message(self.Selected(self._filtered[self._cursor][0]))

    # 返回当前是否有可选项
    def has_selection(self) -> bool:
        return len(self._filtered) > 0

    def on_mount(self) -> None:
        self._redraw()

    # 渲染筛选后的命令列表，高亮当前光标项
    def _redraw(self) -> None:
        if not self._filtered:
            self.update("[dim]  no matching commands[/dim]")
            return
        lines: list[str] = []
        for i, (name, desc) in enumerate(self._filtered):
            desc_part = f"  [dim]{desc}[/dim]" if desc else ""
            if i == self._cursor:
                lines.append(f"  [bold cyan]❯ /{name}[/bold cyan]{desc_part}")
            else:
                lines.append(f"    [cyan]/{name}[/cyan]{desc_part}")
        lines.append("[dim]  ↑↓ navigate   tab/enter select   esc dismiss[/dim]")
        self.update("\n".join(lines))


class ChatTextArea(TextArea):
    """支持 Enter 提交、Cmd/Shift/Alt+Enter 换行的多行聊天输入框。"""

    DEFAULT_CSS = """
    ChatTextArea {
        height: auto;
        min-height: 3;
        max-height: 12;
        border: round $surface-lighten-2;
        background: $background;
        padding: 0 1;
        margin: 1 2;
        scrollbar-size-vertical: 1;
    }
    ChatTextArea:focus {
        border: round $accent;
        background: $background;
    }
    """

    # 子类自定义的提交消息，供宿主 App 监听
    class Submitted(Message):
        def __init__(self, area: ChatTextArea) -> None:
            self.text_area = area
            self.value = area.text
            super().__init__()

    # 输入内容以 / 开头且无空格时发布，query 为 / 之后的字符串（可为空串）；None 表示收起弹窗
    class SlashChanged(Message):
        def __init__(self, query: str | None) -> None:
            self.query = query
            super().__init__()

    # 文本变化时检测 / 前缀，通知宿主 App 更新自动补全弹窗
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = self.text
        if text.startswith("/") and " " not in text:
            self.post_message(ChatTextArea.SlashChanged(query=text[1:]))
        else:
            self.post_message(ChatTextArea.SlashChanged(query=None))

    # Enter 提交；↑↓/Tab/Esc 路由到自动补全弹窗；Cmd/Shift/Alt+Enter 插入换行；其余键交回 TextArea
    async def _on_key(self, event: events.Key) -> None:
        key = event.key

        popup: SlashCompleteWidget | None = None
        try:
            popup = self.app.query_one(SlashCompleteWidget)
        except NoMatches:
            popup = None

        if key == "enter":
            event.stop()
            event.prevent_default()
            if popup is not None and popup.has_selection():
                popup.select_current()
                return
            if self.text.strip():
                self.post_message(self.Submitted(self))
            return
        if key in ("alt+enter", "shift+enter", "ctrl+j", "super+enter"):
            event.stop()
            event.prevent_default()
            if not self.read_only:
                self.insert("\n")
            return
        if popup is not None:
            if key == "up":
                event.stop()
                event.prevent_default()
                popup.move_up()
                return
            elif key == "down":
                event.stop()
                event.prevent_default()
                popup.move_down()
                return
            elif key == "tab":
                event.stop()
                event.prevent_default()
                popup.select_current()
                return
            elif key == "escape":
                event.stop()
                event.prevent_default()
                self.post_message(ChatTextArea.SlashChanged(query=None))
                return
        await super()._on_key(event)


class KamaTuiApp(App[None]):
    """KamaClaude TUI：终端滚屏风格，实时展示 agent 执行过程。"""

    TITLE = "KamaClaude"
    BINDINGS = [
        Binding("ctrl+q", "quit", "quit"),
        Binding("ctrl+o", "sessions", "sessions"),
        Binding("ctrl+s", "status", "status"),
    ]
    CSS = """
    Screen { background: $background; }
    #header {
        height: 1;
        background: $surface;
        color: $text;
        padding: 0 1;
    }
    #log-view {
        height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }
    #banner { padding: 1 2 0 2; }
    Static.user-turn { color: $text; padding: 1 2 0 2; }
    Static.run-header { color: $text-muted; padding: 1 2 0 2; }
    Static.step-divider { color: $text-muted; padding: 0 2; }
    Static.run-ok { color: green; padding: 0 2 1 2; }
    Static.run-err { color: red; padding: 0 2 1 2; }
    Static.usage { padding: 0 2; }
    Static.log-line { padding: 0 2; }
    """

    _BANNER = (
        "[bold cyan]██╗  ██╗ █████╗ ███╗   ███╗ █████╗  ██████╗██╗      █████╗ ██╗   ██╗██████╗ ███████╗[/bold cyan]\n"
        "[bold cyan]██║ ██╔╝██╔══██╗████╗ ████║██╔══██╗██╔════╝██║     ██╔══██╗██║   ██║██╔══██╗██╔════╝[/bold cyan]\n"
        "[bold cyan]█████╔╝ ███████║██╔████╔██║███████║██║     ██║     ███████║██║   ██║██║  ██║█████╗  [/bold cyan]\n"
        "[bold cyan]██╔═██╗ ██╔══██║██║╚██╔╝██║██╔══██║██║     ██║     ██╔══██║██║   ██║██║  ██║██╔══╝  [/bold cyan]\n"
        "[bold cyan]██║  ██╗██║  ██║██║ ╚═╝ ██║██║  ██║╚██████╗███████╗██║  ██║╚██████╔╝██████╔╝███████╗[/bold cyan]\n"
        "[bold cyan]╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝╚═╝  ╚═╝ ╚═════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝[/bold cyan]\n"
        "[dim]  输入消息开始对话  ·  键入 / 触发 skill  ·  Ctrl+C 退出[/dim]"
    )

    # 初始化连接参数和 TUI 内部状态
    def __init__(self, host: str, port: int, replay_run_id: str | None = None) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._replay_run_id = replay_run_id
        self._client: SocketClient | None = None
        self._current_llm: LLMStreamBlock | None = None
        self._pending_tool_blocks: dict[str, ToolCallBlock] = {}
        self._pending_permission_blocks: dict[str, PermissionBlock] = {}
        self._permission_response_pending: set[str] = set()
        self._permission_granted_events: dict[str, str] = {}
        self._session_id: str | None = None
        self._session_summaries: dict[str, dict[str, Any]] = {}
        self._retry_content: str | None = None
        self._retry_request_id: str | None = None
        self._retry_session_id: str | None = None
        self._selected_run_id: str | None = None
        self._selected_child_run_ids: set[str] = set()
        self._pending_run_ids: set[str] = set()
        self._terminal_run_ids: set[str] = set()
        self._busy = False
        self._last_context_pct: float = 0.0
        self._slash_items: list[tuple[str, str]] = []
        self._subagent_run_ids: dict[str, str] = {}  # child run_id -> description
        self._subagent_start_times: dict[str, float] = {}  # child run_id -> start time

    def compose(self) -> ComposeResult:
        yield Label("[bold]KamaClaude[/bold]  [dim]connecting...[/dim]", id="header")
        yield VerticalScroll(id="log-view")
        yield ChatTextArea(id="prompt", show_line_numbers=False)

    def on_mount(self) -> None:
        self._slash_items = self._build_slash_items()
        self._append(Static(self._BANNER, id="banner"))
        self.run_worker(self._socket_loop(), exclusive=True, name="socket")
        prompt = self.query_one("#prompt", ChatTextArea)
        prompt.disabled = True
        prompt.border_title = "connecting..."

    # 构建斜杠命令候选列表：内建命令 + 所有已注册 skill
    def _build_slash_items(self) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = [
            ("sessions", "choose a persistent session"),
            ("status", "inspect the selected session"),
            ("resume", "continue an interrupted run"),
            ("compact", "compress context window"),
        ]
        try:
            loader = SkillLoader()
            for skill in loader.list_all_skills():
                desc = skill.description.splitlines()[0] if skill.description else ""
                if len(desc) > 60:
                    desc = desc[:57] + "..."
                items.append((skill.name, desc))
        except Exception:
            pass
        return items

    # 根据 / 前缀查询字符串挂载、更新或移除自动补全弹窗
    def on_chat_text_area_slash_changed(self, event: ChatTextArea.SlashChanged) -> None:
        query = event.query
        if query is None:
            try:
                self.query_one(SlashCompleteWidget).remove()
            except NoMatches:
                pass
            return
        try:
            popup = self.query_one(SlashCompleteWidget)
            popup.set_query(query)
        except NoMatches:
            popup = SlashCompleteWidget(self._slash_items)
            self.mount(popup, before="#prompt")
            popup.set_query(query)

    # 用户选中自动补全项后将 /{name} 填入输入框并移除弹窗
    def on_slash_complete_widget_selected(self, event: SlashCompleteWidget.Selected) -> None:
        prompt = self._prompt()
        if prompt is not None:
            prompt.text = f"/{event.skill_name} "
            prompt.move_cursor(prompt.document.end)
        try:
            self.query_one(SlashCompleteWidget).remove()
        except NoMatches:
            pass

    # 记录按键焦点；当 PermissionSelect 失去焦点后作为兜底处理权限快捷键
    def on_key(self, event: events.Key) -> None:
        log.debug("App.on_key  key=%r  focused=%r", event.key, self.focused)
        if not self._pending_permission_blocks:
            return
        try:
            select = self.query_one(PermissionSelect)
            if select.has_focus:
                return  # PermissionSelect 有焦点时自行处理，事件不会冒泡到这里
            key = event.key
            decision = PermissionSelect._KEY_MAP.get(key)
            if decision:
                event.stop()
                select._pick(decision)
            elif key in ("up", "k"):
                event.stop()
                select._cursor = (select._cursor - 1) % len(PermissionSelect._CHOICES)
                select.update(select._render_ui())
            elif key in ("down", "j"):
                event.stop()
                select._cursor = (select._cursor + 1) % len(PermissionSelect._CHOICES)
                select.update(select._render_ui())
            elif key == "enter":
                event.stop()
                select._pick(PermissionSelect._CHOICES[select._cursor][0])
        except Exception:
            pass

    # 打开持久会话选择器并在后台读取会话摘要
    def action_sessions(self) -> None:
        self.run_worker(self._open_session_list(), name="session_list", exclusive=False)

    # 清理传输失败重试状态，避免请求业务键跨会话复用
    def _clear_retry_state(self) -> None:
        self._retry_content = None
        self._retry_request_id = None
        self._retry_session_id = None

    # 清空旧会话日志、未完成控件和子任务缓存后重新放置横幅
    def _clear_session_view(self) -> None:
        self._break_llm()
        self._pending_tool_blocks.clear()
        self._pending_permission_blocks.clear()
        self._permission_response_pending.clear()
        self._permission_granted_events.clear()
        self._subagent_run_ids.clear()
        self._subagent_start_times.clear()
        try:
            for select in list(self.query(PermissionSelect)):
                select.remove()
        except Exception:
            pass
        try:
            log_view = self.query_one("#log-view", VerticalScroll)
            for child in list(log_view.children):
                if child.id != "banner":
                    child.remove()
            try:
                banner = log_view.query_one("#banner", Static)
                banner.update(self._BANNER)
            except NoMatches:
                self._append(Static(self._BANNER, id="banner"))
        except Exception:
            pass

    # 同步当前会话的主 run、子 run 和持久终态集合
    def _sync_selected_run(
        self,
        session_id: str,
        summary: dict[str, Any],
        status_result: dict[str, Any] | None = None,
    ) -> None:
        if session_id != self._session_id:
            return
        run_id = summary.get("current_run_id")
        self._selected_run_id = str(run_id) if run_id else None
        children: set[str] = set()
        if isinstance(status_result, dict):
            raw_children = status_result.get("children") or []
            if isinstance(raw_children, list):
                children.update(
                    str(child.get("run_id"))
                    for child in raw_children
                    if isinstance(child, dict) and child.get("run_id")
                )
        self._selected_child_run_ids = children
        known = children | ({self._selected_run_id} if self._selected_run_id else set())
        self._pending_run_ids.intersection_update(known)
        self._terminal_run_ids.intersection_update(known)

    # 设置输入框可用性和标题，集中处理恢复及断线状态
    def _set_prompt_state(self, disabled: bool, title: str, *, focus: bool = False) -> None:
        prompt = self._prompt()
        if prompt is None:
            return
        prompt.disabled = disabled
        prompt.read_only = False
        prompt.border_title = title
        if focus and not disabled:
            prompt.focus()

    # 请求持久会话摘要并显示可操作的选择器
    async def _open_session_list(self) -> None:
        if self._client is None:
            self._append(
                Static(safe_text("sessions unavailable: daemon disconnected"), classes="log-line")
            )
            return
        try:
            result = await self._client.send_command("session.list", {})
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(safe_text(f"session list error: {exc}"), classes="log-line"))
            return
        sessions = result.get("sessions") or []
        normalized = [session for session in sessions if isinstance(session, dict)]
        self._session_summaries = {
            str(session.get("session_id")): session
            for session in normalized
            if session.get("session_id")
        }
        self.push_screen(
            SessionListScreen(normalized, self._session_id),
            self._on_session_selected,
        )

    # 接收选择结果并异步加载对应会话的完整历史
    def _on_session_selected(self, result: dict[str, Any] | None) -> None:
        if not result or not result.get("session_id"):
            return
        self.run_worker(
            self._load_session(str(result["session_id"])),
            name="load_session",
            exclusive=False,
        )

    # 切换当前会话、读取状态和原始历史并展示中断状态
    async def _load_session(
        self,
        session_id: str,
        *,
        status_result: dict[str, Any] | None = None,
    ) -> None:
        if self._client is None:
            return
        if session_id != self._session_id:
            self._clear_retry_state()
        self._session_id = session_id
        self._clear_session_view()
        summary = dict(self._session_summaries.get(session_id, {}))
        self._sync_selected_run(session_id, summary, status_result)
        try:
            history_result = await self._client.send_command(
                "session.get_history",
                {"session_id": session_id},
            )
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(safe_text(f"history load error: {exc}"), classes="log-line"))
            return
        if status_result is None:
            try:
                status_result = await self._client.send_command(
                    "session.status",
                    {"session_id": session_id},
                )
            except (IpcError, RuntimeError, OSError):
                status_result = None
        status_summary = status_result.get("session") if isinstance(status_result, dict) else None
        if isinstance(status_summary, dict):
            summary.update(status_summary)
            self._session_summaries[session_id] = summary
            self._sync_selected_run(session_id, summary, status_result)
        messages = history_result.get("messages") or []
        self._render_session_history(session_id, messages)
        status = str(summary.get("status") or "")
        run_status = str(summary.get("run_status") or "")
        is_running = run_status in {"running", "dispatching", "waiting_approval"}
        requires_review = status in {"needs_review", "interrupted"} or run_status in {
            "unknown",
            "dispatching",
            "interrupted",
            "needs_review",
        }
        self._busy = is_running
        self._set_prompt_state(
            is_running or requires_review or status in {"closed", "abandoned"},
            "session requires review"
            if requires_review
            else "agent is working..."
            if is_running
            else "type a message — enter to send, ⌘/⇧/⌥+enter for newline",
        )
        self._update_header("running" if is_running else "ready")
        if requires_review:
            self.run_worker(
                self._show_session_status(session_id),
                name="session_status",
                exclusive=False,
            )

    # 将完整历史以禁用 markup 的文本写入当前日志视图
    def _render_session_history(self, session_id: str, messages: list[Any]) -> None:
        self._append(
            Static(
                safe_text(f"Loaded session {session_id} history ({len(messages)} messages)"),
                classes="log-line",
            )
        )
        for message in messages:
            if not isinstance(message, dict):
                self._append(Static(safe_text(message), classes="log-line"))
                continue
            role = str(message.get("role") or "message")
            content = message.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            self._append(
                Static(
                    Text(f"{role}: {content}"),
                    classes="history-message",
                    markup=False,
                )
            )

    # 请求会话完整状态并打开详细核查窗口
    async def _show_session_status(
        self,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        target_session_id = session_id or self._session_id
        if self._client is None or not target_session_id:
            self._append(
                Static(safe_text("status unavailable: no session selected"), classes="log-line")
            )
            return
        try:
            result = await self._client.send_command(
                "session.status",
                {"session_id": target_session_id},
            )
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(safe_text(f"session status error: {exc}"), classes="log-line"))
            return
        self.push_screen(
            RecoveryStatusScreen(result, target_session_id, run_id),
            self._on_recovery_action,
        )

    # 在输入命令中打开当前会话的详细状态窗口
    def action_status(self) -> None:
        self.run_worker(self._show_session_status(), name="session_status", exclusive=False)

    # 取得当前会话摘要中的可恢复 run ID
    def _current_run_id(self, session_id: str | None = None) -> str | None:
        summary = self._session_summaries.get(session_id or self._session_id or "", {})
        value = summary.get("current_run_id")
        return str(value) if value else None

    # 接收恢复窗口的显式操作并调用对应后端 RPC
    def _on_recovery_action(self, result: dict[str, Any] | None) -> None:
        if not result:
            return
        action = str(result.get("action") or "")
        session_id = str(result.get("session_id") or self._session_id or "")
        run_id = str(result.get("run_id") or self._current_run_id(session_id) or "")
        note = str(result.get("note") or "").strip()
        if action == "resume":
            self.run_worker(
                self._resume_session(session_id, run_id),
                name="session_resume",
                exclusive=False,
            )
        elif action in {"pause", "abandon"}:
            self.run_worker(
                self._review_session(session_id, run_id, action, note),
                name="session_review",
                exclusive=False,
            )

    # 请求继续指定会话的中断运行并立即显示后端结果
    async def _resume_session(self, session_id: str, run_id: str) -> None:
        if self._client is None or not session_id or not run_id:
            self._append(Static(safe_text("resume requires a session and run"), classes="log-line"))
            return
        workspace = str(Path.cwd().resolve())
        self._terminal_run_ids.discard(run_id)
        try:
            result = await self._client.send_command(
                "session.resume",
                {"session_id": session_id, "run_id": run_id, "workspace": workspace},
            )
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(safe_text(f"resume error: {exc}"), classes="log-line"))
            return
        self._session_id = session_id
        started = bool(result.get("started"))
        result_status = str(result.get("status") or "")
        terminal = run_id in self._terminal_run_ids
        active = started or result_status in {"running", "dispatching", "waiting_approval"}
        if not terminal:
            self._busy = active
        self._append(
            Static(
                safe_text(
                    f"resume {result.get('status', 'unknown')}: "
                    f"{result.get('reason') or 'accepted'}"
                ),
                classes="log-line",
            )
        )
        review = result_status in {"needs_review", "interrupted", "unknown"}
        self._set_prompt_state(
            active or review,
            "session requires review"
            if review
            else "agent is working..."
            if active and not terminal
            else "type a message — enter to send, ⌘/⇧/⌥+enter for newline",
            focus=not active and not review,
        )
        self._update_header("running" if active and not terminal else "ready")

    # 提交暂停或放弃说明并展示审查结果
    async def _review_session(self, session_id: str, run_id: str, action: str, note: str) -> None:
        if self._client is None or not session_id or not run_id:
            self._append(Static(safe_text("review requires a session and run"), classes="log-line"))
            return
        if not note:
            self._append(
                Static(
                    safe_text("pause or abandon requires a human explanation"),
                    classes="log-line",
                )
            )
            return
        try:
            result = await self._client.send_command(
                "session.review",
                {"session_id": session_id, "run_id": run_id, "action": action, "note": note},
            )
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(safe_text(f"review error: {exc}"), classes="log-line"))
            return
        self._busy = False
        self._append(
            Static(
                safe_text(
                    f"{action} {result.get('status', 'unknown')}: "
                    f"{result.get('reason') or 'recorded'}"
                ),
                classes="log-line",
            )
        )
        prompt = self._prompt()
        if prompt is not None:
            prompt.disabled = True
            prompt.border_title = "session abandoned" if action == "abandon" else "session paused"
        self._update_header("ready" if action == "pause" else "disconnected")

    # 退出前尽力关闭当前 session，失败也不阻塞 TUI 退出
    async def action_quit(self) -> None:
        if self._client is not None and self._session_id is not None:
            try:
                await self._client.send_command("session.close", {"session_id": self._session_id})
            except (IpcError, RuntimeError, OSError):
                self._append(Static("[yellow]warning: failed to close session[/yellow]"))
        self.exit()

    # 将输入框提交内容发送给当前 chat session；用 worker 发送，避免 await 阻塞 App 消息泵
    async def on_chat_text_area_submitted(self, event: ChatTextArea.Submitted) -> None:
        content = event.value.strip()
        if not content:
            return
        # 检测会话选择、状态和恢复斜杠命令
        if content == "/sessions":
            event.text_area.text = ""
            self.run_worker(self._open_session_list(), name="session_list", exclusive=False)
            return
        if content == "/status" or content.startswith("/status "):
            event.text_area.text = ""
            session_id = content.partition(" ")[2].strip() or None
            self.run_worker(
                self._show_session_status(session_id),
                name="session_status",
                exclusive=False,
            )
            return
        if content == "/resume" or content.startswith("/resume "):
            event.text_area.text = ""
            requested_run_id = content.partition(" ")[2].strip() or None
            self.run_worker(
                self._resume_from_command(requested_run_id),
                name="session_resume",
                exclusive=False,
            )
            return
        # 检测 /compact 指令
        if content == "/compact":
            event.text_area.text = ""
            if self._client is not None and self._session_id is not None and not self._busy:
                self.run_worker(self._do_compact(), name="compact", exclusive=False)
            return
        if self._client is None or self._session_id is None or self._busy:
            self._append(Static("[yellow]agent busy or disconnected[/yellow]", classes="log-line"))
            return
        self._selected_run_id = None
        self._selected_child_run_ids.clear()
        self._pending_run_ids.clear()
        self._terminal_run_ids.clear()
        self._busy = True
        prompt = event.text_area
        prompt.text = ""
        prompt.disabled = True
        prompt.read_only = False
        prompt.border_title = "agent is working..."
        self._append(
            Static(
                f"[bold]>[/bold] {escape(content)}",
                classes="user-turn",
            )
        )
        self._update_header("running")
        request_id = self._request_id_for_content(content)
        self.run_worker(
            self._do_send_message(content, request_id),
            name="send_message",
            exclusive=False,
        )

    # 根据内容复用传输失败的业务请求 ID，否则创建新的稳定 UUID
    def _request_id_for_content(self, content: str) -> str:
        if (
            self._retry_session_id == self._session_id
            and self._retry_content == content
            and self._retry_request_id
        ):
            return self._retry_request_id
        request_id = str(uuid.uuid4())
        self._retry_content = content
        self._retry_request_id = request_id
        self._retry_session_id = self._session_id
        return request_id

    # 解析 /resume 命令，缺省使用当前摘要指向的中断运行
    async def _resume_from_command(self, requested_run_id: str | None = None) -> None:
        session_id = self._session_id
        run_id = requested_run_id or self._current_run_id(session_id)
        if not session_id or not run_id:
            await self._show_session_status(session_id, requested_run_id)
            return
        await self._resume_session(session_id, run_id)

    # 在 worker 中执行手动压缩命令，完成后显示结果横幅
    async def _do_compact(self) -> None:
        if self._client is None or self._session_id is None:
            return
        self._append(Static("[dim]⚡ compacting context...[/dim]", classes="log-line"))
        try:
            result = await self._client.send_command(
                "session.compact",
                {"session_id": self._session_id, "focus": ""},
            )
            summary_tokens = result.get("summary_tokens", 0)
            saved_tokens = result.get("saved_tokens", 0)
            self._last_context_pct = 0.0
            self._append(Static(
                f"[bold cyan]⚡ Context compacted[/bold cyan]"
                f"  [dim]summary={summary_tokens} tokens  saved≈{saved_tokens} tokens[/dim]",
                classes="log-line",
            ))
        except (IpcError, RuntimeError, OSError) as e:
            self._append(Static(f"[red]compact error: {e}[/red]", classes="log-line"))

    # 在 worker 中执行 IPC 发送，使 App 消息泵在 agent 运行期间仍能处理键盘/焦点等消息
    async def _do_send_message(self, content: str, request_id: str | None = None) -> None:
        if self._client is None:
            return
        request_session_id = self._session_id
        request_id = request_id or self._request_id_for_content(content)
        try:
            result = await self._client.send_command(
                "session.send_message",
                {
                    "session_id": request_session_id,
                    "content": content,
                    "request_id": request_id,
                },
            )
            if self._session_id == request_session_id:
                self._clear_retry_state()
            result_run_id = str(result.get("run_id") or "")
            if result_run_id and self._session_id == request_session_id:
                self._pending_run_ids.add(result_run_id)
                if self._selected_run_id is None:
                    self._selected_run_id = result_run_id
        except (IpcError, RuntimeError, OSError, asyncio.CancelledError) as e:
            if self._session_id == request_session_id:
                self._busy = False
                self._retry_content = content
                self._retry_request_id = request_id
                self._retry_session_id = request_session_id
                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = False
                    prompt.read_only = False
                    prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                self._update_header("ready")
                self._append(Static(f"[red]send error: {e}[/red]", classes="log-line"))

    # 清理审批选择控件；优先使用产生决策的控件，避免移除同 tool 的新审批控件
    def _remove_permission_select(
        self,
        tool_use_id: str,
        approval_id: str | None = None,
        preferred: PermissionSelect | None = None,
    ) -> None:
        if preferred is not None:
            try:
                preferred.remove()
                return
            except Exception:
                pass
        try:
            for select in self.query(PermissionSelect):
                if select._tool_use_id != tool_use_id:
                    continue
                if approval_id is not None and select._approval_id != approval_id:
                    continue
                select.remove()
                return
        except Exception:
            pass

    # 审批完成后恢复输入框；响应或状态不确定时保持禁用
    def _restore_prompt_after_permission(self) -> None:
        if self._pending_permission_blocks or self._permission_response_pending:
            return
        p = self._prompt()
        if p is not None:
            p.disabled = False
            p.read_only = False
            p.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
            p.focus()

    # 将已确认的审批从待处理集合移除并更新控件
    def _complete_permission(
        self,
        permission_key: str,
        tool_use_id: str,
        approval_id: str | None,
        decision: str,
        preferred: PermissionSelect | None = None,
    ) -> None:
        perm_block = self._pending_permission_blocks.pop(permission_key, None)
        if perm_block is not None:
            perm_block._resolve(decision)
        self._remove_permission_select(tool_use_id, approval_id, preferred)
        self._restore_prompt_after_permission()

    # 标记审批响应过期或失败，并刷新会话状态以避免继续猜测执行结果
    def _fail_permission(
        self,
        permission_key: str,
        tool_use_id: str,
        approval_id: str | None,
        reason: str,
        preferred: PermissionSelect | None = None,
    ) -> None:
        reason_text = reason.strip() or "response failed"
        lowered = reason_text.lower()
        outcome = "expired" if any(
            marker in lowered for marker in ("expire", "stale", "not found", "unknown approval")
        ) else "failed"
        perm_block = self._pending_permission_blocks.pop(permission_key, None)
        if perm_block is not None:
            perm_block._resolve(outcome)
        self._remove_permission_select(tool_use_id, approval_id, preferred)
        self._set_prompt_state(
            True,
            "permission expired; refreshing status"
            if outcome == "expired"
            else "permission response failed; refreshing status",
        )
        self._append(
            Static(
                safe_text(f"permission {outcome}: {reason_text}; refreshing session status"),
                classes="log-line",
            )
        )
        if (
            self._client is not None
            and self._session_id is not None
            and getattr(self, "_running", False)
        ):
            self.run_worker(
                self._show_session_status(self._session_id),
                name="session_status",
                exclusive=False,
            )

    # 处理内联审批控件的用户决策：后端确认成功后才 resolve/pop 控件
    async def on_permission_select_decided(self, msg: PermissionSelect.Decided) -> None:
        tool_use_id = msg.tool_use_id
        decision = msg.decision
        approval_id = msg.approval_id
        daemon_epoch = msg.daemon_epoch
        permission_key = self._permission_key(tool_use_id, approval_id)
        log.info(
            "permission decided tool_use_id=%s approval_id=%s daemon_epoch=%s decision=%s",
            tool_use_id,
            approval_id,
            daemon_epoch,
            decision,
        )
        if permission_key is None:
            self._append(
                Static(
                    safe_text("permission request expired; no response was sent"),
                    classes="log-line",
                )
            )
            return

        self._permission_response_pending.add(permission_key)
        try:
            if self._client is None:
                raise RuntimeError("daemon disconnected")
            params: dict[str, Any] = {
                "tool_use_id": tool_use_id,
                "decision": decision,
            }
            if approval_id:
                params["approval_id"] = approval_id
            if daemon_epoch:
                params["daemon_epoch"] = daemon_epoch
            result = await self._client.send_command("permission.respond", params)
            ok = bool(result.get("ok", True)) if isinstance(result, dict) else False
            if not ok:
                reason = str(result.get("reason") or result.get("error") or "response failed")
                self._permission_response_pending.discard(permission_key)
                self._permission_granted_events.pop(permission_key, None)
                self._fail_permission(
                    permission_key,
                    tool_use_id,
                    approval_id,
                    reason,
                    msg.widget,
                )
                return

            granted_decision = self._permission_granted_events.pop(permission_key, None)
            self._permission_response_pending.discard(permission_key)
            self._complete_permission(
                permission_key,
                tool_use_id,
                approval_id,
                granted_decision or decision,
                msg.widget,
            )
        except (IpcError, RuntimeError, OSError) as exc:
            self._permission_response_pending.discard(permission_key)
            self._permission_granted_events.pop(permission_key, None)
            self._fail_permission(
                permission_key,
                tool_use_id,
                approval_id,
                str(exc),
                msg.widget,
            )

    # 向日志视图追加一个 widget 并滚动到底部
    def _append(self, widget: Widget) -> None:
        log_view = self.query_one("#log-view", VerticalScroll)
        log_view.mount(widget)
        log_view.scroll_end(animate=False)

    # 结束当前 LLM 流式块（下一个 token 将开启新块）
    def _break_llm(self) -> None:
        if self._current_llm is not None:
            self._current_llm.finalize_markdown()
        self._current_llm = None

    # 将选择控件挂载到 Screen 顶层（#prompt 之前），避免 VerticalScroll 争抢焦点
    def _mount_permission_select(self, select: PermissionSelect) -> None:
        self.mount(select, before="#prompt")

    # 安全获取输入框，便于组件测试中未挂载时跳过 UI 操作
    def _prompt(self) -> ChatTextArea | None:
        try:
            return self.query_one("#prompt", ChatTextArea)
        except Exception:
            return None

    # 生成 context 占用率的彩色进度条字符串
    def _render_ctx_bar(self, pct: float) -> str:
        filled = int(pct * 20)
        bar = "█" * filled + "░" * (20 - filled)
        label = f"ctx:{pct * 100:.1f}%"
        if pct >= 0.85:
            color = "bold red"
        elif pct >= 0.70:
            color = "yellow"
        else:
            color = "dim"
        return f"[{color}]{label} {bar}[/{color}]"

    # 根据连接和运行状态刷新顶部标题
    def _update_header(self, state: str) -> None:
        try:
            header = self.query_one("#header", Label)
        except NoMatches:
            return
        session = f"  [dim]{self._session_id}[/dim]" if self._session_id else ""
        color = {
            "ready": "green",
            "running": "yellow",
            "disconnected": "red",
            "connecting": "dim",
        }.get(state, "dim")
        header.update(
            f"[bold]KamaClaude[/bold]  [dim]{self._host}:{self._port}[/dim]"
            f"{session}  [{color}]{state}[/{color}]"
        )

    # 连接后复用当前 session，只有首次启动才创建新 session
    async def _restore_or_create_session(self, client: SocketClient) -> None:
        if self._session_id is not None:
            await self._load_session(self._session_id)
            return
        created = await client.send_command(
            "session.create",
            {"mode": "chat", "workspace": str(Path.cwd().resolve())},
        )
        self._session_id = str(created["session_id"])
        self._session_summaries[self._session_id] = {
            "session_id": self._session_id,
            "status": str(created.get("status") or "active"),
            "workspace": str(Path.cwd().resolve()),
        }
        self._selected_run_id = None
        self._selected_child_run_ids.clear()
        log.info("session created session_id=%s", self._session_id)
        self._set_prompt_state(
            False,
            "type a message — enter to send, ⌘/⇧/⌥+enter for newline",
            focus=True,
        )
        self._update_header("ready")

    # 管理 SocketClient 生命周期：连接、订阅事件、断线重连
    async def _socket_loop(self) -> None:
        header = self.query_one("#header", Label)

        while True:
            client = SocketClient(self._host, self._port)
            self._client = None
            try:
                await client.connect()
            except (ConnectionRefusedError, OSError):
                log.warning("connection refused %s:%s, retrying", self._host, self._port)
                self._update_header("disconnected")
                await asyncio.sleep(2)
                continue

            log.info("connected to %s:%s", self._host, self._port)
            self._client = client
            self._update_header("connecting")
            loop_task = asyncio.create_task(client.run_event_loop())

            async def on_event(event: dict[str, Any]) -> None:
                self._handle_event(event)

            client.on_event(on_event)

            try:
                loop_task.add_done_callback(
                    lambda t: log.error("loop_task failed: %s", t.exception())
                    if not t.cancelled() and t.exception() is not None
                    else None
                )
                params: dict[str, Any] = {
                    "topics": [
                        "session.*",
                        "run.*",
                        "step.*",
                        "tool.*",
                        "llm.token",
                        "llm.usage",
                        "log.*",
                        "permission.*",
                        "context.*",
                        "subagent.*",
                        "skill.*",
                    ],
                    "scope": "global",
                }
                if self._replay_run_id is not None:
                    params["replay_from_run"] = self._replay_run_id
                await client.send_command("event.subscribe", params)
                await self._restore_or_create_session(client)
                await loop_task
            except IpcError as e:
                header.update(f"[bold]KamaClaude[/bold]  [red]subscribe error: {e}[/red]")
            finally:
                if not loop_task.done():
                    loop_task.cancel()
                self._client = None
                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = True
                    prompt.read_only = False
                    prompt.border_title = "disconnected, retrying..."
                self._break_llm()
                await client.close()

            self._update_header("disconnected")
            await asyncio.sleep(2)

    # 根据事件 type 路由到对应渲染逻辑；捕获异常防止 socket loop 因单个事件崩溃
    def _handle_event(self, event: dict[str, Any]) -> None:
        try:
            self._handle_event_inner(event)
        except Exception:
            log.exception("_handle_event crashed  event_type=%s", event.get("type", "?"))

    # 判断事件是否属于当前会话，避免 global 订阅污染已选择的日志
    def _event_belongs_to_selected_session(self, event: dict[str, Any]) -> bool:
        if self._session_id is None:
            return True
        event_session_id = event.get("session_id")
        if event_session_id:
            return str(event_session_id) == self._session_id
        event_type = str(event.get("type") or "")
        if event_type.startswith("session.") or event_type in {
            "context.compacted",
            "permission.requested",
        }:
            return False
        if event_type in {"permission.granted", "permission.denied"}:
            tool_use_id = str(event.get("tool_use_id") or "")
            approval_id_value = event.get("approval_id")
            approval_id = str(approval_id_value) if approval_id_value else None
            return self._permission_key(tool_use_id, approval_id) is not None
        run_id = str(event.get("run_id") or "")
        known_runs = self._selected_child_run_ids | self._pending_run_ids
        if self._selected_run_id:
            known_runs.add(self._selected_run_id)
        if run_id and run_id in known_runs:
            return True
        parent_run_id = str(event.get("parent_run_id") or "")
        if parent_run_id and parent_run_id in known_runs:
            if event_type == "subagent.started" and run_id:
                self._selected_child_run_ids.add(run_id)
            return True
        if self._replay_run_id and run_id == self._replay_run_id:
            if event_type == "run.started":
                self._selected_run_id = run_id
            if event_type == "run.finished":
                self._terminal_run_ids.add(run_id)
            return True
        return False

    # 按审批身份或旧事件的 tool_use_id 找到待处理控件
    def _permission_key(self, tool_use_id: str, approval_id: str | None = None) -> str | None:
        # A supplied approval token is authoritative. A stale token must not
        # fall back to a newer request sharing the same tool_use_id.
        if approval_id:
            return approval_id if approval_id in self._pending_permission_blocks else None
        for key, block in self._pending_permission_blocks.items():
            if block._tool_use_id == tool_use_id:
                return key
        return None

    # 实际的事件路由逻辑
    def _handle_event_inner(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")

        if not self._event_belongs_to_selected_session(event):
            return

        if t == "llm.token":
            token = event.get("token", "")
            if self._current_llm is None:
                llm_block = LLMStreamBlock()
                self._append(llm_block)
                self._current_llm = llm_block
            self._current_llm.append_token(token)
            return

        self._break_llm()

        if t == "session.waiting_for_input":
            self._busy = False
            last_run_id = str(event.get("last_run_id") or "")
            if last_run_id:
                self._terminal_run_ids.add(last_run_id)
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                prompt.focus()
            self._update_header("ready")

        elif t == "session.closed":
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.read_only = False
                prompt.border_title = "session closed"
            self._update_header("disconnected")

        elif t == "run.started":
            run_id = str(event.get("run_id") or "")
            if event.get("session_id") and self._selected_run_id is None and run_id:
                self._selected_run_id = run_id
                self._pending_run_ids.add(run_id)
            goal = event.get("goal", "")
            self._append(Static(
                f"[dim]run[/dim]  [cyan]{run_id}[/cyan]  [dim]{_preview(goal, 96)}[/dim]",
                classes="run-header",
            ))

        elif t == "skill.invoked":
            skill_name = event.get("skill_name", "")
            arguments = event.get("arguments", "")
            args_preview = _preview(arguments, 80) if arguments else ""
            args_part = f"  [dim]{args_preview}[/dim]" if args_preview else ""
            self._append(Static(
                f"[bold cyan]/{skill_name}[/bold cyan]{args_part}",
                classes="log-line",
            ))

        elif t == "subagent.started":
            run_id = event.get("run_id", "")
            description = event.get("description", "")
            self._subagent_run_ids[run_id] = description
            self._subagent_start_times[run_id] = time.monotonic()
            short_id = run_id[:8] if len(run_id) >= 8 else run_id
            self._append(Static(
                f"[dim]┌─[/dim] [cyan]{_preview(description, 72)}[/cyan]  [dim]{short_id}[/dim]",
                classes="log-line",
            ))

        elif t == "subagent.finished":
            run_id = event.get("run_id", "")
            status = event.get("status", "")
            description = self._subagent_run_ids.pop(run_id, event.get("description", ""))
            start = self._subagent_start_times.pop(run_id, None)
            elapsed = f"  [dim]{time.monotonic() - start:.1f}s[/dim]" if start is not None else ""
            desc_part = f"[cyan]{_preview(description, 72)}[/cyan]{elapsed}"
            if status == "success":
                self._append(Static(
                    f"[dim]└─[/dim] [bold green]✓[/bold green] {desc_part}",
                    classes="log-line",
                ))
            else:
                self._append(Static(
                    f"[dim]└─[/dim] [bold red]✗[/bold red] {desc_part}",
                    classes="log-line",
                ))

        elif t == "step.started":
            run_id = event.get("run_id", "")
            if run_id in self._subagent_run_ids:
                return
            step = event.get("step", "")
            self._append(Static(
                f"[dim]step {step}[/dim]",
                classes="step-divider",
            ))

        elif t == "tool.call_started":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            params = event.get("params") or {}
            run_id = event.get("run_id", "")
            tc_block = ToolCallBlock(tool_name, params)
            if run_id in self._subagent_run_ids:
                tc_block.styles.padding = (0, 2, 0, 6)
            self._pending_tool_blocks[tool_use_id] = tc_block
            self._append(tc_block)

        elif t == "tool.call_finished":
            tool_use_id = str(event.get("tool_use_id", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            output = str(event.get("output") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(output, elapsed_ms)

        elif t == "tool.call_failed":
            tool_use_id = str(event.get("tool_use_id", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            error_msg = str(event.get("error_message") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(error_msg, elapsed_ms, is_error=True)

        elif t == "run.finished":
            run_id = str(event.get("run_id") or "")
            if run_id:
                self._terminal_run_ids.add(run_id)
            is_child = run_id in self._selected_child_run_ids
            if not is_child:
                self._busy = False
            status = event.get("status", "")
            steps = event.get("steps", 0)
            reason = event.get("reason") or ""
            if status == "success":
                self._append(Static(
                    f"[bold green]✓ completed[/bold green]  [dim]{steps} steps[/dim]",
                    classes="run-ok",
                ))
            elif status == "needs_review":
                self._append(Static(
                    "[bold yellow]⚠ needs review[/bold yellow]  "
                    "Tool effects are unconfirmed; execution stopped.",
                    classes="run-err",
                ))
            else:
                detail = f"  [dim]{reason}[/dim]" if reason else ""
                self._append(Static(
                    f"[bold red]✗ failed[/bold red]{detail}  [dim]{steps} steps[/dim]",
                    classes="run-err",
                ))
            if not is_child:
                review = status in {"needs_review", "interrupted", "unknown"}
                self._set_prompt_state(
                    review,
                    "session requires review"
                    if review
                    else "type a message — enter to send, ⌘/⇧/⌥+enter for newline",
                    focus=not review,
                )
                self._update_header("ready")

        elif t == "llm.usage":
            run_id = event.get("run_id", "")
            if run_id in self._subagent_run_ids:
                return
            pct = float(event.get("context_pct") or 0.0)
            self._last_context_pct = pct
            ctx_bar = self._render_ctx_bar(pct)
            self._append(Static(
                f"[dim]  tokens  "
                f"in={event.get('input_tokens')} "
                f"out={event.get('output_tokens')} "
                f"cache={event.get('cache_read_input_tokens')}[/dim]"
                f"  {ctx_bar}",
                classes="usage",
            ))

        elif t == "context.compacted":
            orig = event.get("original_tokens", 0)
            summary = event.get("summary_tokens", 0)
            self._last_context_pct = 0.0
            self._append(Static(
                f"[bold cyan]⚡ Context compacted[/bold cyan]"
                f"  [dim]original≈{orig} tokens → summary={summary} tokens[/dim]",
                classes="log-line",
            ))

        elif t == "permission.requested":
            tool_use_id = str(event.get("tool_use_id", ""))
            approval_id_value = event.get("approval_id")
            approval_id = str(approval_id_value) if approval_id_value else None
            daemon_epoch_value = event.get("daemon_epoch")
            daemon_epoch = str(daemon_epoch_value) if daemon_epoch_value else None
            tool_name = str(event.get("tool_name", ""))
            param_preview = str(event.get("param_preview", ""))
            try:
                _focused_repr = repr(self.focused)
            except Exception:
                _focused_repr = "?"
            log.info(
                "permission.requested tool=%s id=%s approval_id=%s daemon_epoch=%s app.focused=%s",
                tool_name, tool_use_id, approval_id, daemon_epoch, _focused_repr,
            )
            permission_key = (
                self._permission_key(tool_use_id, approval_id) or approval_id or tool_use_id
            )
            perm_block = PermissionBlock(
                tool_use_id,
                tool_name,
                param_preview,
                approval_id,
                daemon_epoch,
            )
            self._pending_permission_blocks[permission_key] = perm_block
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.border_title = "permission required"
            self._append(perm_block)
            select = PermissionSelect(tool_use_id, approval_id, daemon_epoch)
            self._mount_permission_select(select)
            log.debug(
                "PermissionSelect mounted before #prompt  pending=%d",
                len(self._pending_permission_blocks),
            )

        elif t in {"permission.granted", "permission.denied"}:
            # 审批 token 不匹配时 _permission_key 返回 None，旧事件不能清理新控件
            tool_use_id = str(event.get("tool_use_id", ""))
            approval_id_value = event.get("approval_id")
            approval_id = str(approval_id_value) if approval_id_value else None
            event_permission_key = self._permission_key(tool_use_id, approval_id)
            if event_permission_key is None:
                return
            decision = str(
                event.get(
                    "decision",
                    "allow_once" if t == "permission.granted" else "deny_once",
                )
            )
            if (
                t == "permission.granted"
                and event_permission_key in self._permission_response_pending
            ):
                # The event may race the RPC result. Defer the visible allow until
                # permission.respond confirms that the daemon accepted it.
                self._permission_granted_events[event_permission_key] = decision
                return
            self._permission_response_pending.discard(event_permission_key)
            self._permission_granted_events.pop(event_permission_key, None)
            self._complete_permission(
                event_permission_key,
                tool_use_id,
                approval_id,
                decision,
            )

        elif t == "log.line":
            level = event.get("level", "INFO")
            color = "bold red" if level == "ERROR" else ("yellow" if level == "WARNING" else "dim")
            self._append(Static(
                f"[{color}]{level}[/{color}]  "
                f"[dim]{event.get('source', '')}[/dim]  {event.get('message', '')}",
                classes="log-line",
            ))


# TUI 入口：读取配置并启动 KamaTuiApp
def run(config: KamaConfig, replay_run_id: str | None = None) -> None:
    app = KamaTuiApp(config.host, config.port, replay_run_id=replay_run_id)
    app.run()
