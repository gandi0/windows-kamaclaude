from __future__ import annotations

import os

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.provider import AnthropicProvider
from kama_claude.core.llm.types import LlmResponse


class LazyAnthropicProvider:
    # 延迟到真实模型请求时创建客户端，使 daemon 启动与查询不依赖 API Key
    # base_url 用于配置兼容端点（如阿里 DashScope），留空则使用 SDK 默认
    def __init__(self, model: str, base_url: str | None = None) -> None:
        self._model = model
        self._base_url = base_url
        self._provider: AnthropicProvider | None = None

    # 缺少配置时作为本次模型错误返回控制权，不以 SystemExit 终止整个 daemon
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        if self._provider is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            self._provider = AnthropicProvider(self._model, base_url=self._base_url)
        return await self._provider.chat(
            messages, tool_schemas, bus, run_id, step=step, system=system
        )
