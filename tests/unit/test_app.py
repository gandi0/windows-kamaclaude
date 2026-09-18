from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from unittest.mock import AsyncMock, Mock

import pytest

from kama_claude.core import app
from kama_claude.core.config import KamaConfig


# 构造隔离的异步服务器替身并注入应用依赖
@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Mock:
    server = Mock()
    server.start = AsyncMock(return_value="127.0.0.1:7437")
    server.stop = AsyncMock()
    monkeypatch.setattr(app, "get_config", KamaConfig)
    monkeypatch.setattr(app, "setup_logging", Mock())
    monkeypatch.setattr(app, "SocketServer", Mock(return_value=server))
    return server


# 功能：验证不支持信号处理的事件循环仍可启动并在取消时清理。
# 设计：替换信号注册和服务器依赖，隔离平台差异与真实网络资源。
async def test_unsupported_signal_handlers_allow_startup_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, server: Mock
) -> None:
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", Mock(side_effect=NotImplementedError))

    task = asyncio.create_task(app.CoreApp().run())
    try:
        await asyncio.sleep(0)
        if task.done():
            await task
        server.start.assert_awaited_once()
        server.stop.assert_not_awaited()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    server.stop.assert_awaited_once()


# 功能：验证 Unix SIGINT 和 SIGTERM 回调都能唤醒关闭事件并停止服务器。
# 设计：直接调用已注册回调，隔离测试进程并避免发送真实系统信号。
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
async def test_supported_signal_stops_server(
    monkeypatch: pytest.MonkeyPatch, server: Mock, signum: signal.Signals
) -> None:
    callbacks: dict[int, Callable[[], None]] = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", callbacks.__setitem__)

    task = asyncio.create_task(app.CoreApp().run())
    try:
        await asyncio.sleep(0)
        callbacks[signum]()
        await asyncio.wait_for(task, timeout=1)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    server.stop.assert_awaited_once()


# 功能：验证同步入口吞掉 asyncio.run 转换出的 KeyboardInterrupt。
# 设计：替换入口依赖，避免启动真实事件循环或创建未等待的协程。
def test_entrypoint_exits_cleanly_on_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    # 避免替换 asyncio.run 后创建未等待的协程
    monkeypatch.setattr(app.CoreApp, "run", Mock())
    monkeypatch.setattr(asyncio, "run", Mock(side_effect=KeyboardInterrupt))

    app.run()
