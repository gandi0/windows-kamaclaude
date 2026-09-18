from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kama_claude.core import app as app_module


# 为 CoreApp 测试替换配置、服务器和 trace 资源，避免访问用户目录或真实 TCP
def _patch_core_resources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[MagicMock, MagicMock, asyncio.Event]:
    config = SimpleNamespace(
        host="127.0.0.1",
        port=17843,
        trace=SimpleNamespace(enabled=True, file=str(tmp_path / "trace.jsonl")),
    )
    server = MagicMock(name="server")
    server_started = asyncio.Event()

    # 记录 fake server 已完成启动，供测试等待初始化完成
    async def start_server() -> tuple[str, int]:
        server_started.set()
        return config.host, config.port

    server.start = AsyncMock(side_effect=start_server)
    server.stop = AsyncMock()

    trace = MagicMock(name="trace")
    trace.start = AsyncMock()
    trace.stop = AsyncMock()

    monkeypatch.setattr(app_module, "get_config", MagicMock(return_value=config))
    monkeypatch.setattr(app_module, "setup_logging", MagicMock())
    monkeypatch.setattr(app_module, "SocketServer", MagicMock(return_value=server))
    monkeypatch.setattr(app_module, "TraceWriter", MagicMock(return_value=trace))
    monkeypatch.setattr(app_module, "SessionStore", MagicMock())
    return server, trace, server_started


# 保持后台任务运行并在取消时记录清理已经完成
async def _background_worker(started: asyncio.Event, finished: asyncio.Event) -> None:
    started.set()
    try:
        await asyncio.Event().wait()
    finally:
        finished.set()


# 功能：验证不支持信号注册时 CoreApp 仍保持运行并在取消时清理资源
# 设计：让两个注册调用都抛出 NotImplementedError，再取消主任务，直接断言后台任务、服务器和 trace 的清理结果
@pytest.mark.asyncio
async def test_run_handles_unsupported_signal_handlers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server, trace, server_started = _patch_core_resources(monkeypatch, tmp_path)
    loop = asyncio.get_running_loop()
    registered: list[signal.Signals] = []

    # 记录 Windows 事件循环尝试注册的信号并模拟不支持
    def reject_signal(signum: signal.Signals, callback: Any) -> None:
        del callback
        registered.append(signum)
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", reject_signal)

    core = app_module.CoreApp()
    worker_started = asyncio.Event()
    worker_finished = asyncio.Event()
    worker = asyncio.create_task(_background_worker(worker_started, worker_finished))
    await worker_started.wait()
    core._running_runs.add(worker)

    run_task = asyncio.create_task(core.run())
    await server_started.wait()
    assert not run_task.done()
    assert registered == [signal.SIGINT, signal.SIGTERM]

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert worker.done()
    assert worker.cancelled()
    assert worker_finished.is_set()
    server.stop.assert_awaited_once_with()
    trace.stop.assert_awaited_once_with()


# 功能：验证支持信号注册时 SIGINT 和 SIGTERM 都能触发正常停机
# 设计：替换当前事件循环的注册方法保存回调，分别调用两个信号回调并断言相同的资源清理路径
@pytest.mark.asyncio
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
async def test_run_stops_on_supported_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, signum: signal.Signals
) -> None:
    server, trace, server_started = _patch_core_resources(monkeypatch, tmp_path)
    loop = asyncio.get_running_loop()
    handlers: dict[signal.Signals, Any] = {}

    # 保存已注册的信号回调，测试可直接触发退出事件
    def register_signal(signum: signal.Signals, callback: Any) -> None:
        handlers[signum] = callback

    monkeypatch.setattr(loop, "add_signal_handler", register_signal)

    run_task = asyncio.create_task(app_module.CoreApp().run())
    await server_started.wait()
    assert set(handlers) == {signal.SIGINT, signal.SIGTERM}

    handlers[signum]()
    await run_task

    server.stop.assert_awaited_once_with()
    trace.stop.assert_awaited_once_with()


# 功能：验证同步入口在 asyncio.run 收到 KeyboardInterrupt 时正常返回
# 设计：替换 CoreApp 和事件循环入口，隔离真实异步资源并直接模拟 Ctrl+C 异常
def test_run_handles_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    core = MagicMock(name="core")
    asyncio_run = MagicMock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr(app_module, "CoreApp", MagicMock(return_value=core))
    monkeypatch.setattr(asyncio, "run", asyncio_run)

    app_module.run()

    asyncio_run.assert_called_once_with(core.run.return_value)
