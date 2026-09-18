from __future__ import annotations

import asyncio
import signal
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from kama_claude.core import app as app_module


class _FakeTrace:
    # 构造不写入磁盘的 trace 替身
    def __init__(self, _path: Any) -> None:
        self.started = False
        self.stopped = False

    # 记录 trace 启动
    async def start(self) -> None:
        self.started = True

    # 记录 trace 关闭
    async def stop(self) -> None:
        self.stopped = True

    # 接收事件但不产生外部写入
    def emit(self, _record: Any) -> None:
        return


class _FakeServer:
    # 初始化可控的 server 替身
    def __init__(self, _host: str, _port: int, _broadcaster: Any, *, trace: Any) -> None:
        self.started = asyncio.Event()
        self.stopped = False
        self.handlers: dict[str, Any] = {}

    # 记录 server 启动并让测试继续控制退出
    async def start(self) -> tuple[str, int]:
        self.started.set()
        return ("127.0.0.1", 0)

    # 记录 server 关闭
    async def stop(self) -> None:
        self.stopped = True

    # 保存注册的 handler 以模拟 server API
    def register(self, method: str, handler: Any) -> None:
        self.handlers[method] = handler


class _FakeMcp:
    # 构造不连接外部 MCP 服务的管理器替身
    def __init__(self) -> None:
        self.stopped = False

    # 接受 MCP 启动请求但不创建外部进程
    async def start_all(self, _servers: Any) -> None:
        return

    # 记录 MCP 清理请求
    async def stop_all(self) -> None:
        self.stopped = True


class _FakeSessions:
    # 接受 CoreApp 的 session 依赖但不启动模型调用
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        return


class _FakePermission:
    # 接受权限配置但不读写用户策略
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        return


# 构造不读取用户配置且覆盖各阶段字段的测试配置
def _config() -> Any:
    return SimpleNamespace(
        host="127.0.0.1",
        port=0,
        llm=SimpleNamespace(default_model="fake-model"),
        mcp=SimpleNamespace(servers=[]),
        trace=SimpleNamespace(enabled=True, file="unused-trace.jsonl"),
        permission=SimpleNamespace(timeout_s=1.0),
    )


# 替换 CoreApp 启动依赖并返回可观测的 server、trace 替身
def _patch_startup(monkeypatch: pytest.MonkeyPatch) -> tuple[list[_FakeServer], list[_FakeTrace]]:
    servers: list[_FakeServer] = []
    traces: list[_FakeTrace] = []

    # 构造并记录 trace 替身
    def make_trace(path: Any) -> _FakeTrace:
        trace = _FakeTrace(path)
        traces.append(trace)
        return trace

    # 构造并记录 server 替身
    def make_server(*args: Any, **kwargs: Any) -> _FakeServer:
        server = _FakeServer(*args, **kwargs)
        servers.append(server)
        return server

    monkeypatch.setattr(app_module, "get_config", _config)
    monkeypatch.setattr(app_module, "setup_logging", Mock())
    monkeypatch.setattr(app_module, "TraceWriter", make_trace)
    monkeypatch.setattr(app_module, "PermissionManager", _FakePermission)
    monkeypatch.setattr(app_module, "load_policy_file", lambda _path: {})
    monkeypatch.setattr(app_module, "IpcEventBroadcaster", lambda **_kwargs: Mock(handle=Mock()))
    monkeypatch.setattr(app_module, "SessionStore", Mock())
    monkeypatch.setattr(app_module, "SessionManager", _FakeSessions)
    monkeypatch.setattr(app_module, "AgentRunner", Mock())
    monkeypatch.setattr(app_module, "SocketServer", make_server)
    if hasattr(app_module, "AnthropicProvider"):
        monkeypatch.setattr(app_module, "AnthropicProvider", Mock())
    if hasattr(app_module, "McpServerManager"):
        monkeypatch.setattr(app_module, "McpServerManager", _FakeMcp)
    return servers, traces


# 等待异步启动流程创建伪 server
async def _wait_for_server(servers: list[_FakeServer]) -> _FakeServer:
    while not servers:
        await asyncio.sleep(0)
    return servers[0]


# 功能：验证 Windows 不支持信号注册时仍能完成 daemon 启动并进入等待状态
# 设计：让两个注册调用都抛 NotImplementedError，再取消主任务并检查 finally 清理，覆盖 Windows 事件循环路径
@pytest.mark.asyncio
async def test_run_works_without_signal_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    servers, traces = _patch_startup(monkeypatch)
    loop = asyncio.get_running_loop()
    registrations: list[signal.Signals] = []

    # 模拟不支持信号处理器的 Windows 事件循环
    def unsupported(sig: signal.Signals, _callback: Any) -> None:
        registrations.append(sig)
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", unsupported)
    core = app_module.CoreApp()
    main_task = asyncio.create_task(core.run())
    server = await asyncio.wait_for(_wait_for_server(servers), timeout=1)
    await asyncio.wait_for(server.started.wait(), timeout=1)
    main_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await main_task

    assert registrations == [signal.SIGINT, signal.SIGTERM]
    assert server.stopped
    assert traces[0].started
    assert traces[0].stopped


# 功能：验证 Unix 信号回调能触发正常退出并执行资源清理
# 设计：保存 SIGINT/SIGTERM 回调后分别主动调用，直接覆盖 signal handler 与 shutdown Event 的连接
@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown_signal", [signal.SIGINT, signal.SIGTERM])
async def test_run_exits_from_signal_callback(
    monkeypatch: pytest.MonkeyPatch, shutdown_signal: signal.Signals
) -> None:
    servers, traces = _patch_startup(monkeypatch)
    loop = asyncio.get_running_loop()
    callbacks: dict[signal.Signals, Any] = {}

    # 保存 Unix 信号回调供测试主动触发
    def register(sig: signal.Signals, callback: Any) -> None:
        callbacks[sig] = callback

    monkeypatch.setattr(loop, "add_signal_handler", register)
    core = app_module.CoreApp()
    main_task = asyncio.create_task(core.run())
    server = await asyncio.wait_for(_wait_for_server(servers), timeout=1)
    await asyncio.wait_for(server.started.wait(), timeout=1)
    callbacks[shutdown_signal]()
    await asyncio.wait_for(main_task, timeout=1)

    assert set(callbacks) == {signal.SIGINT, signal.SIGTERM}
    assert server.stopped
    assert traces[0].stopped


# 功能：验证主任务取消时后台 run task 会被取消并等待完成
# 设计：注入一个长生命周期 asyncio task，检查取消与 gather 都发生，避免遗留后台任务污染事件循环
@pytest.mark.asyncio
async def test_run_cancels_running_runs_on_task_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    servers, _traces = _patch_startup(monkeypatch)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda _sig, _callback: None)
    core = app_module.CoreApp()
    main_task = asyncio.create_task(core.run())
    server = await asyncio.wait_for(_wait_for_server(servers), timeout=1)
    await asyncio.wait_for(server.started.wait(), timeout=1)
    run_task = asyncio.create_task(asyncio.sleep(60))
    core._running_runs.add(run_task)
    main_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await main_task

    assert run_task.cancelled()
    assert server.stopped


# 功能：验证主任务取消时 MCP 管理器会执行关闭
# 设计：使用无外部连接的 MCP 替身并取消等待任务，确认 MCP 清理位于 finally 路径
@pytest.mark.asyncio
async def test_run_stops_mcp_on_task_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    if not hasattr(app_module, "McpServerManager"):
        pytest.skip("MCP manager is introduced after stage 6")
    servers, _traces = _patch_startup(monkeypatch)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda _sig, _callback: None)
    core = app_module.CoreApp()
    main_task = asyncio.create_task(core.run())
    server = await asyncio.wait_for(_wait_for_server(servers), timeout=1)
    await asyncio.wait_for(server.started.wait(), timeout=1)
    mcp_manager = core._mcp_manager
    assert mcp_manager is not None
    main_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await main_task

    assert mcp_manager.stopped


# 功能：验证同步入口吞掉 asyncio.run 产生的 KeyboardInterrupt
# 设计：替换 asyncio.run 为立即抛出 KeyboardInterrupt 的桩，隔离入口行为且不启动真实 daemon
def test_run_swallows_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    # 模拟 asyncio.run 在 Ctrl+C 后抛出 KeyboardInterrupt
    def raise_keyboard_interrupt(_coroutine: Any) -> None:
        _coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "run", raise_keyboard_interrupt)
    app_module.run()
