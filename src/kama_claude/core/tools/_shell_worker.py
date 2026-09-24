from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING


# 加入受控进程树并握手，收到命令后才启动实际 Shell；父进程断开则不执行
def main() -> int:
    if os.name == "nt":
        if TYPE_CHECKING:
            from kama_claude.core.tools.windows_job import join_job
        else:
            from windows_job import join_job

        join_job(sys.argv[1])
    sys.stdout.buffer.write(b"KAMA_SHELL_READY\n")
    sys.stdout.buffer.flush()
    payload = sys.stdin.buffer.readline()
    if not payload:
        return 125
    command = json.loads(payload)["command"]
    if os.name == "nt":
        shell = os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe")
        # 保留完整命令字符串的 cmd 引号，不把它当作普通 argv 元素转义。
        process = subprocess.Popen(f'"{shell}" /d /s /c "{command}"', stdin=subprocess.DEVNULL)
    else:
        process = subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.DEVNULL)
    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
