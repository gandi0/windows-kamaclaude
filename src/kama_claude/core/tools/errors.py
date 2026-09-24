from __future__ import annotations

import asyncio

from kama_claude.core.tools.base import ToolResult


class RateLimitedError(Exception):
    """Raised by a tool when the upstream service is rate-limiting the request."""


class ToolCancelledError(asyncio.CancelledError):
    # 携带清理结论传播取消，不能把取消作为普通工具失败吞掉
    def __init__(self, result: ToolResult) -> None:
        super().__init__(result.content)
        self.result = result
