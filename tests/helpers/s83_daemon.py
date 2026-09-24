from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_POLL_SECONDS = 0.05
_READY_TIMEOUT = 20.0
_RPC_TIMEOUT = 20.0


# 返回测试仓库根目录，确保子进程使用当前 worktree 的源码
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# 返回一个尚未占用的本地 TCP 端口
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# 返回当前 UTC 无关的单调时间戳，供超时轮询使用
def _deadline(timeout: float) -> float:
    return time.monotonic() + timeout


# 判断一个路径是否已经出现，避免用固定 sleep 猜测进程状态
async def wait_for_path(path: Path, *, timeout: float = _READY_TIMEOUT) -> None:
    limit = _deadline(timeout)
    while time.monotonic() < limit:
        if path.exists():
            return
        await asyncio.sleep(_POLL_SECONDS)
    raise TimeoutError(f"test barrier was not reached: {path}")


# 读取子进程追加的 JSONL 操作证据
def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


class RpcError(RuntimeError):
    # 表示测试客户端收到 JSON-RPC error
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.data = data


class RpcClient:
    # 为真实 daemon 提供可并发使用的最小 NDJSON JSON-RPC 客户端
    def __init__(
        self,
        host: str,
        port: int,
        label: str = "client",
        evidence_path: Path | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.label = label
        self.evidence_path = evidence_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._receiver: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # 建立真实 TCP 连接并启动响应接收协程
    async def connect(self, *, timeout: float = _READY_TIMEOUT) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=timeout,
        )
        self._receiver = asyncio.create_task(self._receive_loop())

    # 读取服务端响应并分发到请求 Future 或事件队列
    async def _receive_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    return
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "jsonrpc" in message:
                    request_id = message.get("id")
                    future = self._pending.pop(str(request_id), None) if request_id is not None else None
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        error = message["error"]
                        future.set_exception(
                            RpcError(
                                int(error.get("code", -1)),
                                str(error.get("message", "unknown")),
                                error.get("data"),
                            )
                        )
                    else:
                        result = message.get("result")
                        future.set_result(result if isinstance(result, dict) else {})
                elif message.get("kind") == "event":
                    event = message.get("event")
                    if isinstance(event, dict):
                        await self._events.put(event)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("daemon connection closed"))
            self._pending.clear()

    # 发送一条 JSON-RPC 请求并等待服务端响应
    async def call(
        self, method: str, params: dict[str, Any], *, timeout: float = _RPC_TIMEOUT,
    ) -> dict[str, Any]:
        if self._writer is None:
            raise RuntimeError("RPC client is not connected")
        request_id = f"{self.label}-{uuid.uuid4().hex}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        request = {
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
        }
        self._record_evidence(
            "rpc.request",
            request_id=request_id,
            method=method,
            params=params,
        )
        try:
            async with self._write_lock:
                self._writer.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
                await self._writer.drain()
            result = await asyncio.wait_for(future, timeout=timeout)
            self._record_evidence(
                "rpc.response",
                request_id=request_id,
                method=method,
                result=result,
            )
            return result
        except RpcError as exc:
            self._record_evidence(
                "rpc.error",
                request_id=request_id,
                method=method,
                error={"code": exc.code, "message": str(exc), "data": exc.data},
            )
            raise
        except Exception as exc:
            self._record_evidence(
                "rpc.error",
                request_id=request_id,
                method=method,
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            raise
        finally:
            self._pending.pop(request_id, None)

    # 将客户端 RPC 请求、响应或错误追加到本次场景的 JSONL 证据
    def _record_evidence(self, operation: str, **data: Any) -> None:
        if self.evidence_path is None:
            return
        row = {
            "ts": time.time(),
            "pid": os.getpid(),
            "operation": operation,
            "client": self.label,
            "host": self.host,
            "port": self.port,
            **data,
        }
        with self.evidence_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    # 等待服务端推送指定类型的事件并保留其他事件
    async def next_event(self, event_type: str, *, timeout: float = _RPC_TIMEOUT) -> dict[str, Any]:
        deferred: list[dict[str, Any]] = []
        try:
            async with asyncio.timeout(timeout):
                while True:
                    event = await self._events.get()
                    if event.get("type") == event_type:
                        return event
                    deferred.append(event)
        finally:
            for event in deferred:
                self._events.put_nowait(event)

    # 关闭客户端连接并取消接收任务
    async def close(self) -> None:
        if self._receiver is not None:
            self._receiver.cancel()
            await asyncio.gather(self._receiver, return_exceptions=True)
            self._receiver = None
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(TimeoutError, ConnectionError, OSError):
                await asyncio.wait_for(self._writer.wait_closed(), timeout=1.0)
            self._writer = None


@dataclass
class DaemonProcess:
    # 启动、重启和强制结束一个隔离的真实 CoreApp 子进程

    profile: Path
    workspace: Path
    scenario: str
    host: str = "127.0.0.1"
    port: int | None = None
    process: subprocess.Popen[bytes] | None = field(default=None, init=False)
    _log_handle: Any = field(default=None, init=False, repr=False)
    _clients: list[RpcClient] = field(default_factory=list, init=False, repr=False)
    _generation: int = field(default=0, init=False)

    # 创建隔离 profile、配置文件和工作区目录
    def prepare(self) -> None:
        self.profile = self.profile.resolve()
        self.workspace = self.workspace.resolve()
        self.profile.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.profile / "barriers").mkdir(parents=True, exist_ok=True)
        if self.port is None:
            self.port = _free_port()
        (self.profile / "config.toml").write_text(
            "[core]\n"
            f'host = "{self.host}"\n'
            f"port = {self.port}\n"
            "[logging]\nlevel = \"WARNING\"\nfile = \"\"\n",
            encoding="utf-8",
        )

    # 构造不读取真实用户配置和 API key 的子进程环境
    def _environment(self) -> dict[str, str]:
        root = _repo_root()
        env = os.environ.copy()
        python_path = os.pathsep.join([str(root / "src"), str(root)])
        env.update(
            {
                "PYTHONPATH": python_path,
                "PYTHON_DOTENV_DISABLED": "1",
                "ANTHROPIC_API_KEY": "",
                "ANTHROPIC_AUTH_TOKEN": "",
                "KAMA_CONFIG": str(self.profile / "config.toml"),
                "KAMA_HOST": self.host,
                "KAMA_PORT": str(self.port),
                "KAMA_LOG_FILE": "",
                "KAMA_LOG_LEVEL": "WARNING",
                "KAMA_TRACE_ENABLED": "0",
                "S83_PROFILE": str(self.profile),
                "S83_WORKSPACE": str(self.workspace),
                "S83_SCENARIO": self.scenario,
            }
        )
        # 将 HOME/USERPROFILE 等重定向到 profile，隔离 ~/.kama 和默认策略
        env["HOME"] = str(self.profile)
        env["USERPROFILE"] = str(self.profile)
        env["APPDATA"] = str(self.profile / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(self.profile / "AppData" / "Local")
        return env

    # 启动一个真实 CoreApp 子进程并等待 TCP ping 可用
    async def start(self) -> None:
        self.prepare()
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("daemon already running")
        self._generation += 1
        log_path = self.profile / f"daemon-{self._generation}.log"
        self._log_handle = log_path.open("ab")
        command = [
            sys.executable,
            "-m",
            "tests.helpers.s83_daemon",
            "--child",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--profile",
            str(self.profile),
            "--workspace",
            str(self.workspace),
            "--scenario",
            self.scenario,
        ]
        self.process = subprocess.Popen(
            command,
            cwd=str(self.profile),
            env=self._environment(),
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
        )
        self.record(
            "daemon.start",
            pid=self.process.pid,
            scenario=self.scenario,
            host=self.host,
            port=self.port,
            generation=self._generation,
        )
        await self._wait_ready()

    # 轮询 TCP 服务和 core.ping，确认 daemon 已完成启动
    async def _wait_ready(self) -> None:
        assert self.process is not None
        assert self.port is not None
        limit = _deadline(_READY_TIMEOUT)
        last_error: BaseException | None = None
        while time.monotonic() < limit:
            if self.process.poll() is not None:
                log = (self.profile / f"daemon-{self._generation}.log").read_text(
                    encoding="utf-8", errors="replace",
                )
                raise RuntimeError(f"daemon exited during startup: {log}")
            try:
                client = RpcClient(
                    self.host,
                    self.port,
                    label="readiness",
                    evidence_path=self.profile / "operations.jsonl",
                )
                await client.connect(timeout=2.0)
                self._clients.append(client)
                await client.call("core.ping", {"client": "s83-test"}, timeout=2.0)
                await client.close()
                self._clients.remove(client)
                return
            except (ConnectionError, OSError, TimeoutError, RpcError) as exc:
                last_error = exc
                await asyncio.sleep(_POLL_SECONDS)
        raise TimeoutError(f"daemon did not become ready: {last_error}")

    # 建立一个独立 RPC 客户端，支持两个客户端并发恢复
    async def connect(self, label: str = "client") -> RpcClient:
        if self.port is None:
            raise RuntimeError("daemon has no port")
        client = RpcClient(
            self.host,
            self.port,
            label=label,
            evidence_path=self.profile / "operations.jsonl",
        )
        await client.connect()
        self._clients.append(client)
        await client.call(
            "event.subscribe",
            {
                "topics": ["permission.*", "run.*", "tool.*", "subagent.*"],
                "scope": "global",
            },
        )
        return client

    # 记录测试主进程发出的操作，便于 probe 生成证据
    def record(self, operation: str, **data: Any) -> None:
        path = self.profile / "operations.jsonl"
        row = {"ts": time.time(), "operation": operation, **data}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    # 关闭客户端连接但保留 daemon 和持久数据
    async def close_clients(self) -> None:
        clients = list(self._clients)
        self._clients.clear()
        for client in clients:
            await client.close()

    # 只强制结束当前 helper 自己启动的 daemon 子进程
    async def kill(self) -> None:
        await self.close_clients()
        process = self.process
        if process is None or process.poll() is not None:
            return
        pid = process.pid
        process.kill()
        await asyncio.to_thread(process.wait, 10)
        self.record(
            "daemon.kill",
            pid=pid,
            scenario=self.scenario,
            port=self.port,
            exitcode=process.returncode,
        )
        self.process = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    # 停止当前 helper 自己启动的 daemon 子进程
    async def stop(self) -> None:
        await self.close_clients()
        process = self.process
        if process is None:
            return
        pid = process.pid
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, 10)
        self.record("daemon.stop", pid=pid, exitcode=process.returncode)
        self.process = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    # 返回指定测试屏障的到达和释放文件路径
    def barrier_paths(self, name: str) -> tuple[Path, Path]:
        barrier_dir = self.profile / "barriers"
        return barrier_dir / f"{name}.reached", barrier_dir / f"{name}.release"

    # 等待子进程报告指定同步屏障已到达
    async def wait_barrier(self, name: str, *, timeout: float = _READY_TIMEOUT) -> None:
        reached, _release = self.barrier_paths(name)
        await wait_for_path(reached, timeout=timeout)

    # 释放一个已到达的子进程同步屏障
    def release_barrier(self, name: str) -> None:
        _reached, release = self.barrier_paths(name)
        release.write_text("release\n", encoding="utf-8")

    # 返回当前隔离 profile 中唯一的执行数据库路径
    async def execution_db(self, *, timeout: float = _READY_TIMEOUT) -> Path:
        limit = _deadline(timeout)
        while time.monotonic() < limit:
            databases = list(self.profile.rglob("execution.sqlite3"))
            if databases:
                return databases[0]
            await asyncio.sleep(_POLL_SECONDS)
        raise TimeoutError(f"execution database was not created under {self.profile}")


# 追加一条子进程 JSONL 证据，不依赖生产日志格式
def _append_child_evidence(profile: Path, operation: str, **data: Any) -> None:
    row = {"ts": time.time(), "pid": os.getpid(), "operation": operation, **data}
    with (profile / "child-operations.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


# 读取跨 daemon 保留的固定 provider 调用次数
def _provider_count(profile: Path) -> int:
    path = profile / "provider-count.json"
    if not path.exists():
        return 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    return int(raw.get("count", 0)) if isinstance(raw, dict) else 0


# 递增固定 provider 的持久调用次数并返回新值
def _next_provider_count(profile: Path, run_id: str, step: int) -> int:
    count = _provider_count(profile) + 1
    (profile / "provider-count.json").write_text(
        json.dumps({"count": count, "last_run_id": run_id, "last_step": step}),
        encoding="utf-8",
    )
    _append_child_evidence(profile, "provider.chat", count=count, run_id=run_id, step=step)
    return count


# 子进程内同步等待测试释放文件，强杀时由父测试结束等待
def _wait_release_sync(profile: Path, name: str, *, timeout: float = 300.0) -> None:
    _reached, release = (
        profile / "barriers" / f"{name}.reached",
        profile / "barriers" / f"{name}.release",
    )
    _reached.write_text(json.dumps({"pid": os.getpid(), "name": name}), encoding="utf-8")
    _append_child_evidence(profile, "barrier.reached", name=name)
    limit = _deadline(timeout)
    while time.monotonic() < limit:
        if release.exists():
            _append_child_evidence(profile, "barrier.released", name=name)
            return
        time.sleep(_POLL_SECONDS)
    raise TimeoutError(f"test barrier release timed out: {name}")


# 从模型消息中提取可区分固定场景的用户文本
def _message_text(messages: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    for key in ("type", "text", "content", "name", "id"):
                        value = block.get(key)
                        if isinstance(value, str):
                            chunks.append(value)
    return "\n".join(chunks)


# 构造不调用真实模型的固定响应 provider
def _make_provider(profile: Path, scenario: str) -> Any:
    from kama_claude.core.llm.types import LlmResponse, ToolCallBlock

    python_exe = sys.executable.replace('"', "")
    counter_command = f'"{python_exe}" counter.py'
    query_run_ids: set[str] = set()

    class FixedProvider:
        # 根据场景和持久调用次数返回稳定的工具或最终响应
        async def chat(
            self,
            messages: list[dict[str, Any]],
            tool_schemas: list[dict[str, Any]],
            bus: Any,
            run_id: str,
            *,
            step: int = 0,
            system: str | None = None,
        ) -> LlmResponse:
            count = _next_provider_count(profile, run_id, step)
            text = _message_text(messages)
            has_tool_result = "tool_result" in text
            if scenario == "s84-compaction":
                if run_id == "compact":
                    return LlmResponse(stop_reason="end_turn", text="S84_DURABLE_SUMMARY")
                _append_child_evidence(
                    profile,
                    "s84.context",
                    run_id=run_id,
                    summary_seen="S84_DURABLE_SUMMARY" in text,
                    original_seen="S84_ORIGINAL_REPLY" in text,
                    message_count=len(messages),
                )
                reply = "S84_ORIGINAL_REPLY" if count == 1 else "S84_RESTARTED_REPLY"
                return LlmResponse(stop_reason="end_turn", text=reply)
            if scenario in {"child", "child-complete"} and "query child" in text:
                parts = text.split("query child", 1)[1].strip().split()
                child_id = parts[0] if parts else ""
                if not child_id:
                    child_id_path = profile / "query-child-id.txt"
                    if child_id_path.exists():
                        child_id = child_id_path.read_text(encoding="utf-8").strip()
                if not child_id:
                    return LlmResponse(stop_reason="end_turn", text="child id missing")
                if run_id in query_run_ids:
                    return LlmResponse(stop_reason="end_turn", text="child result verified")
                query_run_ids.add(run_id)
                return LlmResponse(
                    stop_reason="tool_use",
                    tool_calls=[ToolCallBlock(
                        "model-query-child", "agent_result", {"run_id": child_id},
                    )],
                )
            if scenario in {"child", "child-complete"} and "child task" in text:
                if not (profile / "child-complete.fast").exists():
                    _wait_release_sync(profile, "child-model-entry")
                return LlmResponse(stop_reason="end_turn", text="child complete")
            if scenario in {"child", "child-complete"} and not has_tool_result:
                return LlmResponse(
                    stop_reason="tool_use",
                    tool_calls=[ToolCallBlock(
                        "model-spawn-child", "spawn_agent", {
                            "description": "child task",
                            "prompt": "child task",
                            "run_in_background": True,
                        },
                    )],
                )
            if scenario in {"child", "child-complete"} and has_tool_result:
                return LlmResponse(stop_reason="end_turn", text="parent complete")
            if scenario == "committed-result" and count == 2:
                _wait_release_sync(profile, "second-model-entry")
            if not has_tool_result:
                if scenario == "workspace":
                    return LlmResponse(
                        stop_reason="tool_use",
                        tool_calls=[ToolCallBlock(
                            "model-write-file", "write_file", {
                                "path": "target.txt", "content": "agent-write",
                            },
                        )],
                    )
                return LlmResponse(
                    stop_reason="tool_use",
                    tool_calls=[ToolCallBlock(
                        "model-increment", "bash", {"command": counter_command},
                    )],
                )
            return LlmResponse(stop_reason="end_turn", text="operation complete")

    return FixedProvider()


# 安装测试专用提交屏障，不向生产配置暴露故障开关
def _install_commit_barriers(profile: Path, scenario: str) -> None:
    from kama_claude.core.session.execution import ExecutionStore

    original_commit_result = ExecutionStore.commit_result
    original_start_attempt = ExecutionStore.start_attempt
    commit_seen = False
    attempt_seen = False

    # 在真实结果提交前暂停，保留工具已经产生副作用的窗口
    def commit_result(self: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal commit_seen
        reached = profile / "barriers" / "before-result-commit.reached"
        if (
            scenario == "unknown-result"
            and not kwargs.get("retry", False)
            and not commit_seen
            and not reached.exists()
        ):
            commit_seen = True
            _wait_release_sync(profile, "before-result-commit")
        original_commit_result(self, *args, **kwargs)

    # 在真实 attempt 提交前暂停，覆盖审批通过但尚未跨执行边界的窗口
    def start_attempt(self: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal attempt_seen
        reached = profile / "barriers" / "before-start-attempt.reached"
        if scenario in {"approval", "workspace"} and not attempt_seen and not reached.exists():
            attempt_seen = True
            _wait_release_sync(profile, "before-start-attempt")
        original_start_attempt(self, *args, **kwargs)

    ExecutionStore.commit_result = commit_result
    ExecutionStore.start_attempt = start_attempt


# 启动注入固定 provider 和屏障后的 CoreApp 子进程
async def _run_child(args: argparse.Namespace) -> None:
    from kama_claude.core.app import CoreApp
    from kama_claude.core.config import KamaConfig
    from kama_claude.core.permissions.manager import PermissionManager

    profile = Path(args.profile).resolve()
    workspace = Path(args.workspace).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    config = KamaConfig(host=args.host, port=int(args.port))
    config.logging.level = "WARNING"
    config.logging.file = ""
    config.trace.enabled = False
    config.mcp.servers = []
    provider = _make_provider(profile, args.scenario)
    _install_commit_barriers(profile, args.scenario)
    permission_manager = PermissionManager(timeout_s=120.0)
    app = CoreApp(
        config=config,
        provider=provider,
        sessions_root=profile / "sessions",
        permission_manager=permission_manager,
    )
    _append_child_evidence(profile, "daemon.start", scenario=args.scenario)
    await app.run()


# 解析子进程参数并进入真实 CoreApp 事件循环
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--scenario", required=True)
    args = parser.parse_args()
    if not args.child:
        parser.error("this module is launched by DaemonProcess")
    asyncio.run(_run_child(args))


if __name__ == "__main__":
    main()
