from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel

Effect = Literal["read_only", "write", "unknown"]
Outcome = Literal["known", "not_started", "unknown"]


@dataclass(frozen=True)
class ToolAttempt:
    attempt_id: str
    number: int
    outcome: Outcome
    error_type: str | None
    cleanup_confirmed: bool | None = None


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    # "runtime_error" | "timeout" | "schema_error" | "permission_denied"
    error_type: str | None = None
    transient: bool = False
    outcome: Outcome = "known"
    cleanup_confirmed: bool | None = None
    call_id: str = ""
    attempts: list[ToolAttempt] = field(default_factory=list)


class BaseTool(ABC):
    name: str
    description: str
    input_schema: dict[str, object]
    params_model: ClassVar[type[BaseModel] | None] = None
    effect: ClassVar[Effect] = "unknown"
    retry_safe: ClassVar[bool] = False

    # 为文件和 Shell 工具绑定工作目录，避免会话间依赖进程 cwd
    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = (workspace or Path.cwd()).resolve()

    # 执行工具调用，返回结果或错误
    @abstractmethod
    async def invoke(self, params: dict[str, object]) -> ToolResult: ...
