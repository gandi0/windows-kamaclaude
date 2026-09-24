from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from check_baseline import isolated_env


# 在真实 CoreApp 启动路径验证无 Key 的 IPC 及 Windows 信号关闭
async def probe() -> dict[str, object]:
    from kama_claude.core.app import CoreApp
    from kama_claude.core.config import KamaConfig, LoggingConfig, TraceConfig
    from kama_claude.core.transport.socket_server import SocketServer

    config = KamaConfig(port=0, logging=LoggingConfig(file=""), trace=TraceConfig(enabled=False))
    ready = asyncio.Event()
    ports: list[int] = []
    original = SocketServer.start

    # 真实监听端口建立后发出同步信号，不以固定 sleep 猜启动时序
    async def start(server: SocketServer) -> str:
        result = await original(server)
        assert server._server is not None
        ports.append(server._server.sockets[0].getsockname()[1])
        ready.set()
        return result

    with patch("kama_claude.core.app.get_config", return_value=config), patch.object(
        SocketServer, "start", start
    ):
        daemon = asyncio.create_task(CoreApp().run())
        try:
            await asyncio.wait_for(ready.wait(), timeout=5)
            reader, writer = await asyncio.open_connection("127.0.0.1", ports[0])
            writer.write(b'{"jsonrpc":"2.0","id":"ping","method":"core.ping","params":{}}\n')
            await writer.drain()
            response = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
            assert "result" in response, response
            writer.close()
            await writer.wait_closed()
            signal.raise_signal(signal.SIGINT)
            await asyncio.wait_for(daemon, timeout=5)
            return {"verified": True, "mode": "s81_regression", "ping": "ok",
                    "shutdown": "SIGINT raised inside test process; clean exit",
                    "provider": "real lazy provider, no API Key", "api_calls": 0}
        finally:
            if not daemon.done():
                daemon.cancel()
            await asyncio.gather(daemon, return_exceptions=True)


# 仅在 Windows 上以独立临时用户目录探测启动，不保留测试 daemon
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("This probe targets native Windows only.")
    if args.child:
        print(json.dumps(asyncio.run(probe())))
        return 0
    with tempfile.TemporaryDirectory(prefix="kama-s8-windows-") as directory:
        root = Path(directory)
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child"],
            cwd=root, env=isolated_env(root), capture_output=True, text=True, timeout=15,
        )
        if result.returncode:
            print(result.stdout + result.stderr, file=sys.stderr)
            return result.returncode
        observation = json.loads(result.stdout)
        report = json.dumps(observation, ensure_ascii=False, indent=2) + "\n"
        print(report)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
