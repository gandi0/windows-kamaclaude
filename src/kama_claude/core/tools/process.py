from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.errors import ToolCancelledError
from kama_claude.core.tools.windows_job import WindowsJob

_CLEANUP_TIMEOUT = 5.0
_MAX_OUTPUT_BYTES = 64 * 1024


# 屏蔽重复取消直至资源回收结束，并将收到的取消信号交回调用方
async def _settle[T](task: asyncio.Task[T]) -> tuple[T, bool]:
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.done():
                return task.result(), True
            cancelled = True


# 终止整个受控进程树并排空管道，只有观察到退出才返回已确认清理
async def _cleanup(proc: asyncio.subprocess.Process, job: WindowsJob | None) -> bool:
    try:
        if job is not None:
            job.terminate()
        elif sys.platform != "win32":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await asyncio.wait_for(proc.communicate(), timeout=_CLEANUP_TIMEOUT)
        deadline = time.monotonic() + _CLEANUP_TIMEOUT
        if job is not None:
            while not job.exits_confirmed():
                if time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(0.01)
        elif sys.platform != "win32":
            # POSIX 仅确认受控进程组，不保证恶意 setsid 后代；Windows 由 Job 约束。
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return True
            return False
        return proc.returncode is not None
    except (OSError, TimeoutError):
        return False


# 以握手引导进程执行命令，取消和超时均等待进程树清理后再返回结论
async def run_shell(command: str, timeout: float, *, cwd: Path | None = None) -> ToolResult:
    try:
        job = WindowsJob() if os.name == "nt" else None
    except OSError as exc:
        return ToolResult(str(exc), True, "runtime_error", outcome="not_started",
                          cleanup_confirmed=True)
    arguments = [sys.executable, str(Path(__file__).with_name("_shell_worker.py"))]
    if job is not None:
        arguments.append(job.name)
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *arguments, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=os.name != "nt",
        cwd=cwd,
    ))
    proc: asyncio.subprocess.Process | None = None
    exchange: asyncio.Task[tuple[bytes, bytes | None]] | None = None
    cancelled = False
    dispatched = False
    result: ToolResult | None = None

    # 握手成功意味着引导进程已被 Job 接管，随后才发送用户命令
    async def communicate() -> tuple[bytes, bytes | None]:
        nonlocal dispatched
        assert proc is not None and proc.stdout is not None
        ready = await proc.stdout.readline()
        if ready != b"KAMA_SHELL_READY\n":
            raise OSError("Shell supervisor did not establish process containment: "
                          + ready.decode("utf-8", errors="replace"))
        dispatched = True
        return await proc.communicate((json.dumps({"command": command}) + "\n").encode())

    try:
        proc = await asyncio.shield(spawn)
        exchange = asyncio.create_task(communicate())
        output, _ = await asyncio.wait_for(asyncio.shield(exchange), timeout=timeout)
        code = proc.returncode
        text = output[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        if len(output) > _MAX_OUTPUT_BYTES:
            text += "\n[truncated]"
        result = ToolResult(
            content=(f"[exit {code}]\n{text}" if code else text or "[no output]"),
            is_error=bool(code), error_type="runtime_error" if code else None,
        )
    except asyncio.CancelledError:
        cancelled = True
        result = ToolResult("Shell execution cancelled; partial effects may remain.",
                            True, "cancelled", outcome="unknown" if dispatched else "not_started")
    except TimeoutError:
        result = ToolResult(f"[timeout after {timeout}s]; partial effects may remain.", True,
                            "timeout", outcome="unknown" if dispatched else "not_started")
    except Exception as exc:
        result = ToolResult(str(exc), True, "runtime_error",
                            outcome="unknown" if dispatched else "not_started")
    finally:
        try:
            if proc is None:
                try:
                    proc, extra_cancel = await _settle(spawn)
                    cancelled |= extra_cancel
                except Exception:
                    pass
            if exchange is not None:
                if not exchange.done():
                    exchange.cancel()
                # 收回原管道读者，再由清理协程接管排空，避免并发读取同一 StreamReader。
                async def collect_exchange() -> None:
                    await asyncio.gather(exchange, return_exceptions=True)

                _, extra_cancel = await _settle(asyncio.create_task(collect_exchange()))
                cancelled |= extra_cancel
            confirmed = proc is None
            if proc is not None:
                confirmed, extra_cancel = await _settle(asyncio.create_task(_cleanup(proc, job)))
                cancelled |= extra_cancel
            assert result is not None
            result.cleanup_confirmed = confirmed
            if not confirmed:
                result = ToolResult("Process tree cleanup could not be confirmed; review required.",
                                    True, "cleanup_failed", outcome="unknown",
                                    cleanup_confirmed=False)
        finally:
            if job is not None:
                job.close()
    if cancelled:
        if result.error_type != "cleanup_failed":
            result.is_error, result.error_type = True, "cancelled"
        raise ToolCancelledError(result)
    return result
