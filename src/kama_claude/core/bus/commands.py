from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field

from kama_claude.core.session.model import SessionMode, SessionStatus


class PingCommand(BaseModel):
    type: Literal["core.ping"] = "core.ping"
    client: str


class PongResult(BaseModel):
    server_version: str
    uptime_ms: int
    received_at: str  # ISO 8601


class AgentRunCommand(BaseModel):
    type: Literal["agent.run"] = "agent.run"
    goal: str


class AgentRunResult(BaseModel):
    run_id: str


class EventSubscribeCommand(BaseModel):
    type: Literal["event.subscribe"] = "event.subscribe"
    topics: list[str]          # fnmatch 模式，如 ["step.*", "tool.*"]
    scope: str = "global"      # "global" | "run:<run_id>"
    replay_from_run: str | None = None  # 设置则先从 events.jsonl 回放历史再接实时流


class EventSubscribeResult(BaseModel):
    subscription_id: str
    replayed_count: int = 0


class SessionCreateCommand(BaseModel):
    type: Literal["session.create"] = "session.create"
    mode: SessionMode = "chat"
    title: str = ""
    workspace: str | None = None


class SessionCreateResult(BaseModel):
    session_id: str
    status: SessionStatus


class SessionSendMessageCommand(BaseModel):
    type: Literal["session.send_message"] = "session.send_message"
    session_id: str
    content: str
    request_id: str | None = Field(default=None, min_length=1, max_length=200)


class SessionSendMessageResult(BaseModel):
    run_id: str


class SessionGetHistoryCommand(BaseModel):
    type: Literal["session.get_history"] = "session.get_history"
    session_id: str


class SessionGetHistoryResult(BaseModel):
    messages: list[dict[str, Any]]


class SessionCloseCommand(BaseModel):
    type: Literal["session.close"] = "session.close"
    session_id: str


class SessionCloseResult(BaseModel):
    status: SessionStatus


class PermissionRespondCommand(BaseModel):
    type: Literal["permission.respond"] = "permission.respond"
    tool_use_id: str
    # "allow_once" | "always_allow" | "deny_once" | "always_deny"
    decision: str
    approval_id: str | None = None


class PermissionRespondResult(BaseModel):
    ok: bool = True


class SessionCompactCommand(BaseModel):
    type: Literal["session.compact"] = "session.compact"
    session_id: str
    focus: str = ""


class SessionCompactResult(BaseModel):
    summary_tokens: int
    saved_tokens: int
    summary_id: str
    summary_version: int
    summary_from: int
    summary_to: int


class SessionListCommand(BaseModel):
    type: Literal["session.list"] = "session.list"


class SessionListResult(BaseModel):
    sessions: list[dict[str, Any]]


class SessionStatusCommand(BaseModel):
    type: Literal["session.status"] = "session.status"
    session_id: str


class SessionStatusResult(BaseModel):
    session: dict[str, Any]
    runs: list[dict[str, Any]]
    calls: list[dict[str, Any]]
    reviews: list[dict[str, Any]]
    children: list[dict[str, Any]]


class SessionResumeCommand(BaseModel):
    type: Literal["session.resume"] = "session.resume"
    session_id: str
    run_id: str
    workspace: str


class SessionResumeResult(BaseModel):
    run_id: str
    status: str
    started: bool = False
    reason: str | None = None


class SessionReviewCommand(BaseModel):
    type: Literal["session.review"] = "session.review"
    session_id: str
    run_id: str
    action: Literal["pause", "abandon"]
    note: str = Field(min_length=1)


# 根据 type 字段决定命令类型的判别联合
Command = Annotated[
    PingCommand
    | AgentRunCommand
    | EventSubscribeCommand
    | SessionCreateCommand
    | SessionSendMessageCommand
    | SessionGetHistoryCommand
    | SessionCloseCommand
    | PermissionRespondCommand
    | SessionCompactCommand
    | SessionListCommand
    | SessionStatusCommand
    | SessionResumeCommand
    | SessionReviewCommand,
    Discriminator("type"),
]
