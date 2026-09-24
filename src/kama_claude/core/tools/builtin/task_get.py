from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict

from kama_claude.core.task.manager import TaskManager
from kama_claude.core.tools.base import BaseTool, ToolResult


class TaskGetParams(BaseModel):
    model_config = ConfigDict(strict=True)
    task_id: int


class TaskGetTool(BaseTool):
    params_model = TaskGetParams
    effect = "read_only"
    retry_safe = True
    name = "task_get"
    description = "Get full details of a task by its integer ID. Returns the task as JSON."
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "integer",
                "description": "ID of the task to retrieve.",
            },
        },
        "required": ["task_id"],
    }

    # 持有 TaskManager 实例，供 invoke 调用
    def __init__(self, task_manager: TaskManager) -> None:
        self._manager = task_manager

    # 获取任务详情并返回 JSON 字符串
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = TaskGetParams.model_validate(params)
        try:
            task = self._manager.get(p.task_id)
            return ToolResult(content=json.dumps(task.to_dict(), ensure_ascii=False))
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")
