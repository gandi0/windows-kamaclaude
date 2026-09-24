from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from check_baseline import isolated_env

from kama_claude.core.bus.envelope import HandlerError
from kama_claude.core.config import AgentConfig, CompactionConfig, KamaConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SESSION_NOT_FOUND, SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.subagent.tool import AgentResultTool
from kama_claude.core.tools.builtin.bash import BashTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry


# 按固定响应顺序提供模型输出，并保存模型实际收到的上下文
class FixedProvider:
    # 保存测试定义的响应，不创建真实模型客户端
    def __init__(self, responses: list[LlmResponse]) -> None:
        self.responses = iter(responses)
        self.seen: list[list[dict[str, Any]]] = []

    # 返回下一条预设响应，响应耗尽即失败以避免掩盖循环错误
    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LlmResponse:
        self.seen.append(copy.deepcopy(messages))
        return next(self.responses)


# 构造固定工具调用，工具本体使用上游真实实现
def tool_response(call_id: str, name: str, **params: Any) -> LlmResponse:
    return LlmResponse(
        stop_reason="tool_use", tool_calls=[ToolCallBlock(call_id, name, params)]
    )


# 构造默认关闭自动压缩的测试配置，避免读取用户配置
def config(threshold: float = 0.0) -> KamaConfig:
    return KamaConfig(
        agent=AgentConfig(max_steps=8), compaction=CompactionConfig(auto_threshold=threshold)
    )


# 功能：验证文件会话仍在但新 SessionManager 无法查询或继续该会话
# 设计：先完成一次真实 Runner 对话，再以同一存储构造新管理器，区分磁盘丢失和索引缺失
async def f01(root: Path) -> dict[str, Any]:
    store = SessionStore(root / "sessions")
    provider = FixedProvider([LlmResponse(stop_reason="end_turn", text="saved reply")])

    # 每轮按上游方式创建新 Runner，并始终注入固定 Provider
    def factory() -> AgentRunner:
        return AgentRunner(config(), provider=provider, runs_dir=root / "runs")

    manager = SessionManager(store, factory, EventBus())
    session = await manager.create("chat")
    await manager.send_message(session.id, "hello", run_id="f01-run")
    before = await manager.get_history(session.id)
    assert len(before) == 2
    assert store.read_meta(session.id).status == "waiting_for_input"
    fresh = SessionManager(SessionStore(root / "sessions"), factory, EventBus())
    errors = []
    for operation in (fresh.get_history(session.id), fresh.send_message(session.id, "continue")):
        try:
            await operation
        except HandlerError as exc:
            assert exc.code == SESSION_NOT_FOUND
            errors.append(exc.code)
        else:
            raise AssertionError("F01 no longer reproduces; convert to recovery regression")
    assert store.read_messages(session.id) == before
    return {"disk_messages": len(before), "fresh_manager_errors": errors,
            "scope": "manager reconstruction, not daemon restart"}


# 在真实写文件成功后的下一次模型入口硬退出，不执行 Runner 收尾
class CrashProvider:
    # 保存故障注入开关及独立的实验取证目录
    def __init__(self, root: Path, crash: bool) -> None:
        self.root, self.crash = root, crash

    # 首次请求写 marker，第二次确认内存结果后硬退出或正常返回
    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LlmResponse:
        if kwargs["step"] == 1:
            return tool_response("f02-call", "write_file", path="marker.txt", content="effect")
        assert (self.root / "marker.txt").read_text(encoding="utf-8") == "effect"
        blocks = messages[-1]["content"]
        assert blocks[0]["type"] == "tool_result" and not blocks[0].get("is_error", False)
        (self.root / "memory-proof.json").write_text(
            json.dumps({"tool_result_in_memory": True, "messages": messages}), encoding="utf-8"
        )
        if self.crash:
            os._exit(86)
        return LlmResponse(stop_reason="end_turn", text="normal completion")


# 在独立子进程中执行一次真实 SessionManager 与 Runner 写文件任务
async def crash_child(root: Path, crash: bool) -> None:
    store = SessionStore(root / "sessions")
    provider = CrashProvider(root, crash)
    manager = SessionManager(
        store, lambda: AgentRunner(config(), provider=provider), EventBus()
    )
    session = await manager.create("chat")
    (root / "session-id.txt").write_text(session.id, encoding="utf-8")
    await manager.send_message(session.id, "write a marker", run_id="f02-run")


# 功能：验证 S8.2 在真实副作用完成后的硬退出仍保存工具结果和检查点
# 设计：子进程在确定的第二次模型入口退出，正常完成对照排除工具及存储配置错误
async def f02(root: Path) -> dict[str, Any]:
    observations = {}
    for mode in ("normal", "crash"):
        folder = root / mode
        folder.mkdir()
        env = isolated_env(folder)
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child", mode, "--root", str(folder)],
            cwd=folder, env=env, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == (86 if mode == "crash" else 0), result.stderr
        sid = (folder / "session-id.txt").read_text(encoding="utf-8")
        store = SessionStore(folder / "sessions")
        history = store.read_messages(sid)
        rows = store.execution.messages(sid)
        events = [json.loads(line) for line in
                  (store.runs_dir(sid) / "f02-run" / "events.jsonl").read_text(
                      encoding="utf-8").splitlines()]
        assert (folder / "marker.txt").read_text(encoding="utf-8") == "effect"
        assert json.loads((folder / "memory-proof.json").read_text(
            encoding="utf-8"))["tool_result_in_memory"]
        assert any(event["type"] == "tool.call_finished" for event in events)
        blocks = [block for row in rows if isinstance(row["content"], list)
                  for block in row["content"]]
        uses = [block for block in blocks if block["type"] == "tool_use"]
        results = [block for block in blocks if block["type"] == "tool_result"]
        has_result = bool(results)
        finished = any(event["type"] == "run.finished" for event in events)
        assert has_result
        assert finished == (mode == "normal")
        assert len(history) == (4 if mode == "normal" else 3)
        assert len(uses) == len(results) == 1
        assert uses[0]["id"] == results[0]["tool_use_id"] == "f02-call"
        assert not results[0].get("is_error", False)
        checkpoint = store.execution.get_checkpoint("f02-run")
        assert checkpoint is not None and checkpoint["message_seq"] == rows[-1]["seq"]
        status = store.read_meta(sid).status
        assert status == ("waiting_for_input" if mode == "normal" else "active")
        observations[mode] = {
            "child_exit": result.returncode, "marker": "effect", "thread_messages": len(history),
            "tool_result_persisted": has_result, "tool_finished_event": True,
            "run_finished_event": finished, "session_status": status,
            "checkpoint_message_seq": checkpoint["message_seq"],
        }
    return {"observations": observations, "scope": "hard-exit Runner process, not daemon E2E"}


# 功能：验证 S8.1 修复后一次 invoke_tool 不重放先产生副作用再退出失败的命令
# 设计：计数文件记录外部可观察副作用，先直接执行一次作为对照，仅将重试等待缩短为零
async def f03(root: Path) -> dict[str, Any]:
    counter = root / "counter.txt"
    program = root / "increment.py"
    program.write_text(
        "from pathlib import Path\nimport sys\np = Path(sys.argv[1])\n"
        "n = int(p.read_text()) if p.exists() else 0\n"
        "p.write_text(str(n + 1))\nraise SystemExit(7)\n", encoding="utf-8"
    )
    arguments = [sys.executable, str(program), str(counter)]
    command = subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)
    tool = BashTool()
    direct = await tool.invoke({"command": command})
    assert direct.is_error and direct.error_type == "runtime_error"
    assert counter.read_text() == "1"
    counter.write_text("0")
    registry, bus, events = ToolRegistry(), EventBus(), []
    registry.register(tool)

    # 收集真实执行层产生的重试事件及尝试序号
    async def collect(event: Any) -> None:
        events.append(event)

    bus.subscribe(collect)
    with patch("kama_claude.core.tools.invocation._RETRY_BASE_S", 0.0):
        result = await invoke_tool(
            registry, ToolCallBlock("f03-call", "bash", {"command": command}), bus, "f03-run"
        )
    attempts = [event.attempt for event in events if event.type == "tool.call_failed"]
    assert result.is_error and "[exit 7]" in result.content
    assert counter.read_text() == "1" and attempts == [1]
    assert len(result.attempts) == 1 and result.cleanup_confirmed is True
    return {"direct_count": 1, "invoke_count": 1, "attempts": attempts,
            "shell": "cmd.exe" if os.name == "nt" else "/bin/sh"}


# 执行自动压缩实验或关闭压缩的对照，并检查下一轮实际模型上下文
async def compact_run(root: Path, threshold: float) -> dict[str, Any]:
    store = SessionStore(root / "sessions")
    first = tool_response("before-call", "write_file", path=str(root / "before.txt"), content="a")
    first.usage = UsageStats(input_tokens=180000, output_tokens=20, context_pct=0.9)
    responses = [first]
    if threshold:
        responses.append(LlmResponse(stop_reason="end_turn", text="S8_FIXED_SUMMARY"))
    responses.extend([
        tool_response("after-call", "write_file", path=str(root / "after.txt"), content="b"),
        LlmResponse(stop_reason="end_turn", text="S8_FINAL_REPLY"),
        LlmResponse(stop_reason="end_turn", text="next turn"),
    ])
    provider = FixedProvider(responses)
    bus, events = EventBus(), []

    # 保留自动压缩成功事件作为真实路径已被触发的证据
    async def collect(event: Any) -> None:
        events.append(event.type)

    bus.subscribe(collect)
    manager = SessionManager(
        store, lambda: AgentRunner(config(threshold), provider=provider, bus=bus), bus
    )
    session = await manager.create("chat")
    for index in range(4):
        store.append_message(session.id, "user", f"question {index}")
        store.append_message(session.id, "assistant", f"answer {index}")
    await manager.send_message(session.id, "do the work", run_id="f04-run")
    history = store.read_messages(session.id)
    assert (root / "before.txt").read_text() == "a"
    assert (root / "after.txt").read_text() == "b"
    summaries = list(store.session_dir(session.id).glob("summary_*.md"))
    assert ("context.compacted" in events) == bool(threshold)
    assert len(history) == 14
    assert len(summaries) == (1 if threshold else 0)
    if threshold:
        assert provider.seen[2][0]["content"] == "S8_FIXED_SUMMARY"
        assert summaries[0].read_text(encoding="utf-8") == "S8_FIXED_SUMMARY"
    await manager.send_message(session.id, "continue", run_id="f04-next")
    next_context = json.dumps(provider.seen[-1])
    assert "S8_FINAL_REPLY" in next_context
    assert "S8_FIXED_SUMMARY" not in next_context
    return {"prefill_len": 9, "saved_messages_after_run": len(history),
            "compacted": bool(threshold), "summary_files": len(summaries),
            "next_turn_has_final": "S8_FINAL_REPLY" in next_context,
            "next_turn_has_summary": "S8_FIXED_SUMMARY" in next_context,
            "post_compaction_file_written": True}


# 功能：验证 S8.2 自动压缩后原始消息与最终回复不再因旧切片漏存
# 设计：九条预填历史大于压缩后五条工作消息；下一轮 Provider 截获恢复上下文并与关闭压缩对照
async def f04(root: Path) -> dict[str, Any]:
    return {"enabled": await compact_run(root / "enabled", 0.8),
            "disabled_control": await compact_run(root / "disabled", 0.0),
            "default_auto_threshold": config().compaction.auto_threshold,
            "scope": "raw history fixed; durable summary restoration remains S8.4"}


# 功能：验证同一会话第二轮新建 Runner 后无法查询第一轮已完成的真实后台子代理
# 设计：以 subagent.finished 事件同步完成，用原 Runner 成功查询作为对照，不依赖固定 sleep
async def f05(root: Path) -> dict[str, Any]:
    store, bus = SessionStore(root / "sessions"), EventBus()
    child_finished = asyncio.Event()
    child_ids, runners, queries = [], [], []

    # 记录真实子任务 ID 并在后台任务发布完成事件时解除屏障
    async def collect(event: Any) -> None:
        if event.type == "subagent.started":
            child_ids.append(event.run_id)
        if event.type == "subagent.finished":
            child_finished.set()

    bus.subscribe(collect)

    class Provider:
        # 根据父子 run 和步骤返回固定响应，第二轮读取真实工具错误
        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LlmResponse:
            run_id, step = kwargs["run_id"], kwargs["step"]
            if run_id == "parent-one" and step == 1:
                return tool_response(
                    "spawn-call", "spawn_agent", description="controlled child",
                    prompt="child prompt", run_in_background=True,
                )
            if run_id == "parent-one":
                await asyncio.wait_for(child_finished.wait(), timeout=5)
            elif run_id == "parent-two" and step == 1:
                return tool_response("query-call", "agent_result", run_id=child_ids[0])
            elif run_id == "parent-two":
                queries.append(copy.deepcopy(messages[-1]))
            return LlmResponse(stop_reason="end_turn", text="child done" if run_id not in
                               ("parent-one", "parent-two") else "parent done")

    provider = Provider()

    # 保留管理器实际创建的每个 Runner 供对照查询和任务清理
    def factory() -> AgentRunner:
        runner = AgentRunner(config(), provider=provider, bus=bus)
        runners.append(runner)
        return runner

    manager = SessionManager(store, factory, bus)
    session = await manager.create("chat")
    try:
        await manager.send_message(session.id, "start child", run_id="parent-one")
        assert len(child_ids) == 1 and len(runners) == 1
        entries = runners[0]._task_registry.all()
        await asyncio.wait_for(asyncio.gather(*(entry[0] for entry in entries)), timeout=5)
        original = await AgentResultTool(runners[0]._task_registry).invoke({"run_id": child_ids[0]})
        assert not original.is_error and original.content == "child done"
        with patch("kama_claude.core.tools.invocation._RETRY_BASE_S", 0.0):
            await manager.send_message(session.id, "get child", run_id="parent-two")
        assert len(runners) == 2
        block = queries[0]["content"][0]
        assert block["is_error"] and "Unknown run_id" in block["content"]
        return {"runner_count": len(runners), "first_runner_result": original.content,
                "second_turn_result": "Unknown run_id", "child_completed": True,
                "scope": "same-process cross-turn; restart interruption display not tested"}
    finally:
        tasks = [task for runner in runners for task, _ in runner._task_registry.all()]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# 在 Windows 原生路径下启动隔离的实验子进程，输出可审查的实际观察值
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=["all", "F01", "F02", "F03", "F04", "F05"], default="all")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--child", choices=["normal", "crash", "case"])
    args = parser.parse_args()
    if args.child:
        assert args.root is not None
        os.chdir(args.root)
        if args.child == "case":
            result = asyncio.run(globals()[args.case.lower()](args.root))
            print(json.dumps({"case": args.case, "verified": True, "mode": "regression" if args.case in {"F02", "F03", "F04"} else "baseline_reproduction", "actual": result}))
        else:
            asyncio.run(crash_child(args.root, args.child == "crash"))
        return 0
    results = []
    cases = [f"F{number:02}" for number in range(1, 6)] if args.case == "all" else [args.case]
    for case in cases:
        with tempfile.TemporaryDirectory(prefix=f"kama-s8-{case}-") as directory:
            root = Path(directory)
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--case", case,
                 "--child", "case", "--root", str(root)], cwd=root,
                env=isolated_env(root), capture_output=True, text=True, timeout=60,
            )
            if result.returncode:
                print(result.stdout + result.stderr, file=sys.stderr)
                return result.returncode
            observation = json.loads(result.stdout)
            results.append(observation)
            print(json.dumps(observation, ensure_ascii=False), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"python": sys.version, "os": sys.platform,
                                         "results": results}, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
