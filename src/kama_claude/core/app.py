from __future__ import annotations

import asyncio
import datetime
import logging
import signal
import time
from typing import Any

import kama_claude
from kama_claude.core.bus.commands import PongResult
from kama_claude.core.config import get_config
from kama_claude.core.logging_setup import setup_logging
from kama_claude.core.transport.socket_server import SocketServer

logger = logging.getLogger(__name__)


class CoreApp:
    def __init__(self) -> None:
        self._start_time = time.monotonic()

    # 处理 core.ping 请求，返回服务版本、运行时长和接收时间
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        client = params.get("client", "unknown")
        logger.debug("ping from %s", client)
        return PongResult(
            server_version=kama_claude.__version__,
            uptime_ms=int((time.monotonic() - self._start_time) * 1000),
            received_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )

    # 启动守护进程：加载配置、初始化日志、启动 TCP 服务器，并等待退出信号
    async def run(self) -> None:
        self._start_time = time.monotonic()
        config = get_config()
        setup_logging(config)

        server = SocketServer(config.host, config.port)
        server.register("core.ping", self._ping_handler)

        addr = await server.start()
        logger.info("kama-core %s listening addr=%s", kama_claude.__version__, addr)
        logger.info("config: %s", config)

        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        try:
            try:
                loop.add_signal_handler(signal.SIGINT, shutdown.set)
                loop.add_signal_handler(signal.SIGTERM, shutdown.set)
            except NotImplementedError:
                # Windows relies on asyncio.run() cancelling the task on Ctrl+C.
                pass

            await shutdown.wait()
        finally:
            logger.info("shutting down")
            await server.stop()


# 同步入口：启动 CoreApp 事件循环
def run() -> None:
    try:
        asyncio.run(CoreApp().run())
    except KeyboardInterrupt:
        pass
