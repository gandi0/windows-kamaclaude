from __future__ import annotations

import asyncio
import errno
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.loop import AgentLoop
from kama_claude.core.mcp.client import (
    McpClient,
    McpServerUnavailableError,
    McpToolDef,
    McpToolError,
)
from kama_claude.core.mcp.tool import McpTool
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from kama_claude.core.task.manager import TaskManager
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.builtin.bash import BashTool
from kama_claude.core.tools.builtin.read_file import ReadFileTool
from kama_claude.core.tools.builtin.task_create import TaskCreateTool
from kama_claude.core.tools.builtin.task_get import TaskGetTool
from kama_claude.core.tools.builtin.task_update import TaskUpdateTool
from kama_claude.core.tools.errors import ToolCancelledError
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry


# 将真实工具放入新注册表，保证每个用例独立
def registry(tool: BaseTool) -> ToolRegistry:
    result = ToolRegistry()
    result.register(tool)
    return result


# 构造当前系统的 Python 命令，支持空格及非 ASCII 路径
def python_command(*arguments: str) -> str:
    args = [sys.executable, *arguments]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


# 功能：验证 F03 真实 Shell 先产生副作用再失败时一次逻辑调用只执行一次
# 设计：临时计数器外部可见；两次合法相同调用都应执行，不能用参数 hash 错误去重
async def test_f03_shell_failure_is_not_replayed(tmp_path: Path) -> None:
    script = tmp_path / "加一 with spaces.py"
    counter = tmp_path / "counter.txt"
    script.write_text("from pathlib import Path\nimport sys\np=Path(sys.argv[1])\n"
                      "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\n"
                      "raise SystemExit(7)\n", encoding="utf-8")
    call = ToolCallBlock("model-id", "bash", {"command": python_command(str(script), str(counter))})
    first = await invoke_tool(registry(BashTool()), call, EventBus(), "r1")
    assert counter.read_text() == "1"
    assert first.is_error and first.error_type == "runtime_error"
    assert first.outcome == "known" and first.cleanup_confirmed is True
    assert len(first.attempts) == 1
    second = await invoke_tool(registry(BashTool()), call, EventBus(), "r2")
    assert counter.read_text() == "2"
    assert first.call_id and first.call_id != second.call_id
    assert first.attempts[0].attempt_id != second.attempts[0].attempt_id


# 功能：验证真实 read_file 的明确暂时 IO 错误可重试且每次尝试有独立 ID
# 设计：仅在底层读文件首两次注入 EAGAIN，最终读取真实文件，核对返回内容及事件关联
async def test_read_transient_failure_retries_with_distinct_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "data.txt"
    path.write_text("actual data", encoding="utf-8")
    original = Path.read_bytes
    count = 0

    # 注入两次暂时错误，随后恢复真正的磁盘读取
    def read(target: Path) -> bytes:
        nonlocal count
        count += 1
        if count < 3:
            raise BlockingIOError(errno.EAGAIN, "temporarily unavailable")
        return original(target)

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr("kama_claude.core.tools.invocation._RETRY_BASE_S", 0)
    events: list[Any] = []
    bus = EventBus()

    # 收集诊断事件用于交叉检查调用及尝试标识
    async def collect(event: Any) -> None:
        events.append(event)

    bus.subscribe(collect)
    result = await invoke_tool(registry(ReadFileTool()), ToolCallBlock(
        "t1", "read_file", {"path": str(path)}), bus, "run", call_id="reserved-call-id")
    assert result.content == "actual data" and count == 3
    assert result.call_id == "reserved-call-id"
    assert [item.number for item in result.attempts] == [1, 2, 3]
    assert len({item.attempt_id for item in result.attempts}) == 3
    assert {event.call_id for event in events} == {result.call_id}
    assert [event.attempt_id for event in events[1:]] == [
        item.attempt_id for item in result.attempts
    ]
    assert events[-1].outcome == "known"


# 功能：验证永久读取错误及未知副作用工具的暂时错误都不自动重跑
# 设计：分别缺失文件和声明 transient 的默认未知工具，排除仅依据错误类别重试
async def test_permanent_and_unknown_errors_do_not_retry(tmp_path: Path) -> None:
    missing = await invoke_tool(registry(ReadFileTool()), ToolCallBlock(
        "t", "read_file", {"path": str(tmp_path / "absent")}), EventBus(), "r")
    assert missing.is_error and len(missing.attempts) == 1
    tool = _ControlledTool()
    tool.response = ToolResult("temporary", True, "rate_limited", transient=True)
    result = await invoke_tool(registry(tool), ToolCallBlock("t", tool.name, {}), EventBus(), "r")
    assert result.is_error and tool.calls == 1 and len(result.attempts) == 1
    assert McpTool.retry_safe is False and McpTool.effect == "unknown"


class _Params(BaseModel):
    required: str


class _ControlledTool(BaseTool):
    name = "controlled"
    description = "controlled test tool"
    input_schema: dict[str, object] = {}

    # 初始化调用计数及可选的同步屏障
    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.block = False
        self.response = ToolResult("ok")

    # 记录实际执行，在屏障后等待取消或返回预设结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self.calls += 1
        self.entered.set()
        if self.block:
            await asyncio.Future()
        return self.response


@pytest.mark.parametrize("cleanup_confirmed", [True, False])
# 功能：验证外层超时在 Python 3.12/3.13 都保留清理结论并区别清理失败
# 设计：取消只由截止时间触发，工具抛结构化取消子类，覆盖两个解释器的 timeout 转换差异
async def test_deadline_preserves_structured_cleanup(cleanup_confirmed: bool) -> None:
    class Tool(_ControlledTool):
        # 等待截止取消后返回明确的清理结论，不执行真实外部命令
        async def invoke(self, params: dict[str, object]) -> ToolResult:
            self.calls += 1
            try:
                await asyncio.Future()
            except asyncio.CancelledError as exc:
                raise ToolCancelledError(ToolResult(
                    "stopped", True, "cancelled" if cleanup_confirmed else "cleanup_failed",
                    outcome="unknown", cleanup_confirmed=cleanup_confirmed,
                )) from exc
            raise AssertionError("test must be cancelled by deadline")

    tool = Tool()
    result = await invoke_tool(registry(tool), ToolCallBlock("t", tool.name, {}),
                               EventBus(), "r", timeout=0.01)
    assert result.error_type == ("timeout" if cleanup_confirmed else "cleanup_failed")
    assert result.is_error and result.outcome == "unknown"
    assert result.cleanup_confirmed is cleanup_confirmed
    assert tool.calls == 1 and len(result.attempts) == 1


# 功能：验证没有执行时间预算时不会启动工具本体
# 设计：使用可计数工具和零截止时间，核对 not_started 及没有实际 attempt
async def test_expired_deadline_does_not_execute() -> None:
    tool = _ControlledTool()
    result = await invoke_tool(registry(tool), ToolCallBlock("t", tool.name, {}),
                               EventBus(), "r", timeout=0)
    assert result.error_type == "timeout" and result.outcome == "not_started"
    assert tool.calls == 0 and result.attempts == []


# 功能：验证参数错误与权限拒绝都在工具执行前结束，且不伪造 attempt
# 设计：以本体计数为零及结构化 not_started 共同判断前置阻断
async def test_validation_and_denial_do_not_execute() -> None:
    tool = _ControlledTool()
    tool.params_model = _Params
    call = ToolCallBlock("t", tool.name, {})
    result = await invoke_tool(registry(tool), call, EventBus(), "r")
    assert result.error_type == "schema_error" and result.outcome == "not_started"
    assert tool.calls == 0 and result.attempts == []
    manager = PermissionManager({tool.name: ToolPolicy(default=PermissionDecision.DENY)})
    call.input = {"required": "yes"}
    result = await invoke_tool(registry(tool), call, EventBus(), "r", permission_manager=manager)
    assert result.error_type == "permission_denied" and result.outcome == "not_started"
    assert tool.calls == 0 and result.attempts == []


# 功能：验证审批超时和拒绝授权具有不同错误类型，超时前没有执行工具
# 设计：真实审批管理器不收到响应，断言 attempt 为空及本体计数为零
async def test_approval_timeout_is_not_a_denial() -> None:
    tool = _ControlledTool()
    manager = PermissionManager(timeout_s=0.01)
    result = await invoke_tool(registry(tool), ToolCallBlock("t", tool.name, {}), EventBus(),
                               "r", permission_manager=manager)
    assert result.error_type == "approval_timeout" and result.outcome == "not_started"
    assert result.attempts == [] and tool.calls == 0 and manager._pending == {}


# 功能：验证工具吞掉超时取消后返回成功也不能被报告为成功
# 设计：仅在收到取消后才返回，确保输出来自超时路径而非正常完成
async def test_tool_suppressing_timeout_cannot_report_success() -> None:
    class Tool(_ControlledTool):
        # 模拟第三方工具吞掉 CancelledError 并返回成功的错误行为
        async def invoke(self, params: dict[str, object]) -> ToolResult:
            self.calls += 1
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return ToolResult("misleading success")

    tool = Tool()
    result = await invoke_tool(registry(tool), ToolCallBlock("t", tool.name, {}), EventBus(),
                               "r", timeout=0.01)
    assert result.is_error and result.error_type == "timeout" and result.outcome == "unknown"
    assert tool.calls == 1


# 功能：验证审批等待取消后移除待决请求，迟到批准不会调用工具或扩大授权
# 设计：使用 permission.requested 事件作为同步点，不靠 sleep 猜审批时序
async def test_cancelled_approval_is_removed() -> None:
    tool, bus, manager = _ControlledTool(), EventBus(), PermissionManager()
    requested = asyncio.Event()

    # 在真实审批请求生成后通知测试方取消
    async def collect(event: Any) -> None:
        if event.type == "permission.requested":
            requested.set()

    bus.subscribe(collect)
    task = asyncio.create_task(invoke_tool(registry(tool), ToolCallBlock(
        "approval", tool.name, {}), bus, "r", permission_manager=manager))
    await asyncio.wait_for(requested.wait(), 2)
    task.cancel()
    with pytest.raises(ToolCancelledError) as caught:
        await task
    assert caught.value.result.outcome == "not_started"
    assert caught.value.result.attempts == [] and tool.calls == 0
    assert manager._pending == {}
    manager.respond("approval", "always_allow")
    assert manager._persistent_always == {}


# 功能：验证退避期间取消不会开始第二次尝试
# 设计：第一次真实失败事件中调度取消，保留非零退避，直接检查工具计数
async def test_cancel_during_backoff_stops_retry() -> None:
    tool = _ControlledTool()
    tool.retry_safe = True
    tool.effect = "read_only"
    tool.response = ToolResult("retry later", True, "runtime_error", transient=True)
    bus = EventBus()

    # 在失败发布后下一个事件循环周期取消正在退避的任务
    async def collect(event: Any) -> None:
        if event.type == "tool.call_failed" and event.error_class != "cancelled":
            task = asyncio.current_task()
            assert task is not None
            asyncio.get_running_loop().call_soon(task.cancel)

    bus.subscribe(collect)
    task = asyncio.create_task(invoke_tool(registry(tool), ToolCallBlock("t", tool.name, {}), bus, "r"))
    with pytest.raises(ToolCancelledError):
        await task
    assert tool.calls == 1


@pytest.mark.parametrize("cancel", [True, False])
# 功能：验证工具取消或结果未知时停止同批后续工具和下一轮模型请求
# 设计：一个响应包含两次调用，通过首个工具的屏障或未知结果触发停止
async def test_loop_stops_after_cancel_or_unknown(cancel: bool) -> None:
    tool = _ControlledTool()
    tool.block = cancel
    tool.response = ToolResult("uncertain", True, "timeout", outcome="unknown")
    provider = AsyncMock()
    provider.chat.return_value = LlmResponse(stop_reason="tool_use", tool_calls=[
        ToolCallBlock("one", tool.name, {}), ToolCallBlock("two", tool.name, {})
    ])
    context = ExecutionContext("r", "goal", 5)
    task = asyncio.create_task(AgentLoop(provider, registry(tool), EventBus()).run(context))
    await asyncio.wait_for(tool.entered.wait(), 2)
    if cancel:
        task.cancel()
        with pytest.raises(ToolCancelledError):
            await task
    else:
        await task
    assert tool.calls == 1 and provider.chat.call_count == 1
    assert context.status == "needs_review"


@pytest.mark.parametrize(("tool_type", "params"), [
    (TaskCreateTool, {}),
    (TaskCreateTool, {"subject": 123}),
    (TaskCreateTool, {"subject": "work", "blocked_by": ["bad"]}),
    (TaskGetTool, {"task_id": "bad"}),
    (TaskUpdateTool, {"task_id": 1, "status": "invalid"}),
    (TaskUpdateTool, {"task_id": 1, "add_blocked_by": ["bad"]}),
])
# 功能：验证内置任务工具的非法参数在本体执行前被拒绝
# 设计：监视真实 invoke 并检查磁盘无新增文件，覆盖必填字段、类型、状态与依赖列表
async def test_task_schema_blocks_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_type: Any, params: dict[str, Any],
) -> None:
    tool = tool_type(TaskManager(tmp_path))
    body = AsyncMock(wraps=tool.invoke)
    monkeypatch.setattr(tool, "invoke", body)
    result = await invoke_tool(registry(tool), ToolCallBlock("t", tool.name, params), EventBus(), "r")
    body.assert_not_awaited()
    assert result.error_type == "schema_error" and result.outcome == "not_started"
    assert result.attempts == [] and list(tmp_path.iterdir()) == []


# 功能：验证参数模型没有阻断合法的任务创建、更新和查询
# 设计：通过执行层操作真实临时任务文件，核对最终持久内容与读取结果
async def test_valid_task_tools_still_operate(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path)
    for tool, params in [
        (TaskCreateTool(manager), {"subject": "本地任务"}),
        (TaskUpdateTool(manager), {"task_id": 1, "status": "completed"}),
        (TaskGetTool(manager), {"task_id": 1}),
    ]:
        result = await invoke_tool(registry(tool), ToolCallBlock("t", tool.name, params),
                                   EventBus(), "r")
        assert not result.is_error and len(result.attempts) == 1
    assert json.loads(result.content)["status"] == "completed"
    assert manager.get(1).subject == "本地任务"


@pytest.mark.parametrize("failure", ["is_error", "rpc_error", "disconnected"])
# 功能：验证 MCP 工具失败默认视为副作用未知，不自动重试
# 设计：真实客户端解析固定 RPC 返回或异常，覆盖 isError、协议错误及失联，不接触远端
async def test_mcp_failure_remains_unknown(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    client = McpClient()
    request = AsyncMock(return_value={
        "isError": True, "content": [{"type": "text", "text": "partial write failed"}],
    })
    if failure == "rpc_error":
        request.side_effect = McpToolError("server error")
    elif failure == "disconnected":
        request.side_effect = McpServerUnavailableError("disconnected")
    monkeypatch.setattr(client, "_call", request)
    tool = McpTool(client, "fake", McpToolDef("write", "fake write"))
    result = await invoke_tool(registry(tool), ToolCallBlock("t", tool.name, {}), EventBus(), "r")
    request.assert_awaited_once()
    assert result.is_error and result.outcome == "unknown"
    assert len(result.attempts) == 1
