from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.task.manager import TaskManager
from kama_claude.core.tools.base import BaseTool, ToolResult


class TaskCreateParams(BaseModel):
    model_config = ConfigDict(strict=True)
    subject: str
    description: str = ""
    blocked_by: list[int] = Field(default_factory=list)


class TaskCreateTool(BaseTool):
    params_model = TaskCreateParams
    effect = "write"
    name = "task_create"
    description = (
        "Create a new task to track a unit of work. "
        "Use this to break down a complex goal into smaller, trackable steps. "
        "Returns the created task as JSON."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "Short title for the task.",
            },
            "description": {
                "type": "string",
                "description": "Optional longer description of what needs to be done.",
            },
            "blocked_by": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "IDs of tasks that must be completed before this one.",
            },
        },
        "required": ["subject"],
    }

    # 持有 TaskManager 实例，供 invoke 调用
    def __init__(self, task_manager: TaskManager) -> None:
        self._manager = task_manager

    # 创建任务并返回 JSON 字符串
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = TaskCreateParams.model_validate(params)
        try:
            task = self._manager.create(p.subject, p.description, p.blocked_by)
            return ToolResult(content=json.dumps(task.to_dict(), ensure_ascii=False))
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")
