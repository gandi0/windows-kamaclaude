from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from check_baseline import isolated_env

from kama_claude.core.config import KamaConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore

MODES = {"intent_before": 81, "dispatch_before": 82, "result_before": 83,
         "result_after": 84, "final_before": 85}


class CrashProvider:
    # 保存硬退出故障点，模型响应全部固定
    def __init__(self, mode: str) -> None:
        self.mode = mode

    # 工具成功后的下一次模型入口是结果提交后的明确屏障
    async def chat(self, **kwargs: Any) -> LlmResponse:
        if kwargs["step"] == 1:
            return LlmResponse("tool_use", tool_calls=[ToolCallBlock(
                "model-tool-id", "write_file", {"path": "marker.txt", "content": "effect"},
            )])
        if self.mode == "result_after":
            os._exit(MODES[self.mode])
        return LlmResponse("end_turn", text="done")


# 在独立临时进程中执行真实会话和文件工具，精确终止于事务内部
async def child(root: Path, mode: str) -> None:
    store = SessionStore(root / "sessions")
    provider = CrashProvider(mode)
    manager = SessionManager(
        store, lambda: AgentRunner(KamaConfig(), provider=provider), EventBus(),
    )
    session = await manager.create("chat")
    (root / "session.txt").write_text(session.id, encoding="utf-8")

    # 只在实验进程内安装私有注入点，不向正常 daemon 增加故障配置
    def before_commit() -> None:
        conn = store.execution._conn
        call = conn.execute("SELECT * FROM tool_calls LIMIT 1").fetchone()
        attempt = conn.execute("SELECT * FROM attempts LIMIT 1").fetchone()
        run = conn.execute("SELECT * FROM runs LIMIT 1").fetchone()
        should_exit = (
            (mode == "intent_before" and call is not None)
            or (mode == "dispatch_before" and attempt is not None)
            or (mode == "result_before" and call is not None and call["result_json"] is not None)
            or (mode == "final_before" and run is not None and run["status"] == "succeeded")
        )
        if should_exit:
            os._exit(MODES[mode])

    store.execution._before_commit = before_commit
    await manager.send_message(session.id, "write marker", run_id="probe-run")
    raise AssertionError("hard-exit fault point was not reached")


# 重开故障进程留下的真实数据库，验证文件副作用及原子状态一致性
def observe(root: Path, mode: str) -> dict[str, Any]:
    store = SessionStore(root / "sessions")
    try:
        sid = (root / "session.txt").read_text(encoding="utf-8")
        messages = store.execution.messages(sid)
        ids = store.execution._conn.execute("SELECT call_id FROM tool_calls").fetchall()
        call = store.execution.get_call(ids[0][0]) if ids else None
        attempts = store.execution.get_attempts(ids[0][0]) if ids else []
        checkpoint = store.execution.get_checkpoint("probe-run")
        run = store.execution.get_run("probe-run")
        marker = (root / "marker.txt").read_text() if (root / "marker.txt").exists() else None
        expected_messages = {"intent_before": 1, "dispatch_before": 2,
                             "result_before": 2, "result_after": 3, "final_before": 3}
        assert len(messages) == expected_messages[mode]
        assert checkpoint is not None and checkpoint["message_seq"] == messages[-1]["seq"]
        assert run is not None and run["status"] == "running"
        if mode in {"intent_before", "dispatch_before"}:
            assert marker is None and not attempts
            assert call is None if mode == "intent_before" else call["status"] == "planned"
        else:
            assert marker == "effect" and len(attempts) == 1
            assert call is not None
            if mode == "result_before":
                assert call["status"] == "dispatching" and call["result"] is None
                assert attempts[0]["phase"] == "dispatching"
            else:
                assert call["status"] == "succeeded" and call["result"] is not None
                assert attempts[0]["phase"] == "result_committed"
                assert messages[-1]["content"][0]["type"] == "tool_result"
        return {"mode": mode, "exit_code": MODES[mode], "marker": marker,
                "messages": messages, "call": call, "attempts": attempts,
                "checkpoint": checkpoint, "run": run, "verified": True}
    finally:
        store.close()


# 驱动五个确定性硬退出实验并输出可审阅 JSON 证据
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.child:
        asyncio.run(child(args.root, args.mode))
        return
    observations = []
    with tempfile.TemporaryDirectory(prefix="kama-s82-crash-") as folder:
        for mode, code in MODES.items():
            root = Path(folder) / mode
            root.mkdir()
            env = isolated_env(root)
            env["PYTHON_DOTENV_DISABLED"] = "1"
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--child", "--mode", mode,
                 "--root", str(root)], cwd=root, env=env, capture_output=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
            assert result.returncode == code, result.stderr
            observations.append(observe(root, mode))
    evidence = {"python": sys.version, "executable": sys.executable,
                "scope": "hard-exit transaction probes; no daemon recovery or model API",
                "observations": observations}
    text = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
