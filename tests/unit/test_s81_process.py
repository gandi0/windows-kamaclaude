from __future__ import annotations

import asyncio
import ctypes
import json
import sys
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.builtin.bash import BashTool
from kama_claude.core.tools.errors import ToolCancelledError
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry

from .test_s81_execution import python_command

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object contract")


# 打开具体进程的等待句柄，退出判断不受 PID 复用影响
def process_handle(pid: int) -> Any:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    handle = kernel.OpenProcess(0x00100000, False, pid)
    assert handle, ctypes.get_last_error()
    return handle


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
# 功能：验证取消或外层超时返回时真实 Windows 子进程及孙进程均已退出
# 设计：两个后代通过 TCP 同步报告就绪，保留内核句柄验证退出；不用固定 sleep 推测时序
async def test_windows_process_tree_is_gone_before_return(tmp_path: Path, mode: str) -> None:
    ready: asyncio.Queue[tuple[int, asyncio.StreamWriter]] = asyncio.Queue()

    # 保存测试后代的连接及 PID，连接握手即表示它已实际启动
    async def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        message = await reader.readline()
        await ready.put((json.loads(message)["pid"], writer))

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    script = tmp_path / "tree.py"
    script.write_text(
        "import json, os, socket, subprocess, sys\n"
        "if sys.argv[1] == 'parent':\n"
        "    subprocess.Popen([sys.executable, __file__, 'child', sys.argv[2]])\n"
        "s=socket.create_connection(('127.0.0.1',int(sys.argv[2])))\n"
        "s.sendall((json.dumps({'pid':os.getpid()})+'\\n').encode())\n"
        "s.recv(1)\n", encoding="utf-8",
    )
    registry = ToolRegistry()
    registry.register(BashTool())
    task = asyncio.create_task(invoke_tool(registry, ToolCallBlock(
        "tree", "bash", {"command": python_command(str(script), "parent", str(port))}),
        EventBus(), "run", timeout=5 if mode == "timeout" else 30))
    handles, writers = [], []
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        for _ in range(2):
            pid, writer = await asyncio.wait_for(ready.get(), timeout=4)
            writers.append(writer)
            handles.append(process_handle(pid))
        if mode == "cancel":
            task.cancel()
            with pytest.raises(ToolCancelledError) as caught:
                await task
            result = caught.value.result
            assert result.error_type == "cancelled"
        else:
            result = await asyncio.wait_for(task, timeout=10)
            assert result.error_type == "timeout"
        assert result.is_error and result.outcome == "unknown"
        assert result.cleanup_confirmed is True
        assert len(result.attempts) == 1
        assert all(kernel.WaitForSingleObject(handle, 0) == 0 for handle in handles)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for handle in handles:
            kernel.CloseHandle(handle)
        for writer in writers:
            writer.close()
            with suppress(ConnectionResetError):
                await writer.wait_closed()
        server.close()
        await server.wait_closed()


# 功能：验证无法确认清理时不能返回成功
# 设计：真实短命令执行后注入清理结论失败，最终 Job 关闭仍负责回收资源
async def test_unconfirmed_cleanup_returns_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    # 模拟无法取得进程树退出确认，不虚构清理成功
    async def unknown(*args: Any) -> bool:
        return False

    monkeypatch.setattr("kama_claude.core.tools.process._cleanup", unknown)
    result = await BashTool().invoke({"command": "echo completed"})
    assert result.is_error and result.error_type == "cleanup_failed"
    assert result.outcome == "unknown" and result.cleanup_confirmed is False


# 功能：验证重复取消不能截断正在进行的清理
# 设计：清理屏障明确控制完成时间，第二次 cancel 后任务仍须等待屏障释放
async def test_repeated_cancellation_waits_for_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    from kama_claude.core.tools import process

    entered, release = asyncio.Event(), asyncio.Event()
    original = process._cleanup

    # 把真实清理暂停在屏障前，允许确定地注入第二次取消
    async def cleanup(*args: Any) -> bool:
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(process, "_cleanup", cleanup)
    task = asyncio.create_task(BashTool().invoke({"command": "echo done"}))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(ToolCancelledError) as caught:
        await asyncio.wait_for(task, 5)
    result: ToolResult = caught.value.result
    assert result.cleanup_confirmed is True and result.is_error


@pytest.mark.parametrize("failure", ["create", "join"])
# 功能：验证 Job 创建或加入失败时用户命令不产生副作用
# 设计：分别注入创建拒绝和不可打开的 Job 名称，真实子进程握手失败后 marker 仍不存在
async def test_job_setup_failure_never_dispatches_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    from kama_claude.core.tools import process

    class BrokenJob(process.WindowsJob):
        # 在用户命令派发前注入 Job 创建或引导进程加入失败
        def __init__(self) -> None:
            if failure == "create":
                raise OSError("injected Job creation failure")
            super().__init__()
            self.name += "-missing"

    marker = tmp_path / "must-not-exist.txt"
    script = tmp_path / "write.py"
    script.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('changed')\n")
    monkeypatch.setattr(process, "WindowsJob", BrokenJob)
    result = await BashTool().invoke({"command": python_command(str(script), str(marker))})
    assert result.is_error and result.outcome == "not_started"
    assert result.cleanup_confirmed is True and not marker.exists()
