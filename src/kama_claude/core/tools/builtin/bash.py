from __future__ import annotations

import asyncio
import locale
import os
import shutil
from pathlib import Path

from kama_claude.core.tools.base import BaseTool, ToolResult

_MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB
_DEFAULT_TIMEOUT = 60


def _find_git_bash() -> Path:
    """Locate Git for Windows Bash without selecting the Windows WSL launcher."""
    configured = os.environ.get("KAMA_BASH_PATH")
    if configured:
        bash = Path(configured).expanduser()
        if not bash.is_file():
            raise FileNotFoundError(f"KAMA_BASH_PATH does not exist: {bash}")
        return bash

    roots: list[Path] = []
    git = shutil.which("git")
    if git:
        roots.append(Path(git).resolve().parent.parent)
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        if directory := os.environ.get(variable):
            roots.append(Path(directory) / "Git")
    if directory := os.environ.get("LOCALAPPDATA"):
        roots.append(Path(directory) / "Programs" / "Git")
    for root in roots:
        for relative in ("bin/bash.exe", "usr/bin/bash.exe"):
            bash = root / relative
            if bash.is_file():
                return bash
    raise FileNotFoundError(
        "Git Bash was not found. Install Git for Windows or set KAMA_BASH_PATH "
        "to its bin/bash.exe. This tool does not use CMD, PowerShell, or WSL."
    )


def _git_bash_env(bash: Path) -> dict[str, str]:
    env = os.environ.copy()
    usr_bin = bash.parent
    if usr_bin.parent.name.lower() != "usr":
        usr_bin = bash.parent.parent / "usr" / "bin"
    # GNU find/head must take precedence over Windows find.exe.
    env["PATH"] = os.pathsep.join((str(usr_bin), str(bash.parent), env.get("PATH", "")))
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _decode_output(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # Native Windows programs may still emit the local encoding (e.g. CP936).
        encoding = locale.getencoding() if os.name == "nt" else "utf-8"
        return raw.decode(encoding, errors="replace")


class BashTool(BaseTool):
    name = "bash"
    description = (
        "Execute a shell command and return its output (stdout + stderr combined). "
        + (
            "The host is Windows, but commands run in Git Bash, NOT CMD or PowerShell. "
            "Use Bash/POSIX syntax and Unix commands such as find and head. "
            "Do not use Get-ChildItem or other PowerShell cmdlets. "
            "Use forward slashes in paths (for example F:/project or /f/project). "
            if os.name == "nt"
            else "Commands run in the system POSIX shell. "
        )
        + "Non-interactive only — commands requiring user input will hang and time out. "
        "Prefer short, focused commands. Output is truncated at 64 KB."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Maximum seconds to wait (default {_DEFAULT_TIMEOUT}, max 120).",
            },
        },
        "required": ["command"],
    }

    # 在子进程中执行 shell 命令，合并 stdout/stderr，超时或非零退出码时返回错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        command = str(params["command"])
        timeout = min(int(str(params.get("timeout") or _DEFAULT_TIMEOUT)), 120)

        try:
            if os.name == "nt":
                bash = _find_git_bash()
                proc = await asyncio.create_subprocess_exec(
                    str(bash),
                    "--noprofile",
                    "--norc",
                    "-c",
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=_git_bash_env(bash),
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            try:
                stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return ToolResult(
                    content=f"[timeout after {timeout}s]",
                    is_error=True,
                    error_type="timeout",
                )
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")

        output = _decode_output(stdout_bytes)
        truncated = len(stdout_bytes) > _MAX_OUTPUT_BYTES
        if truncated:
            output = output[:_MAX_OUTPUT_BYTES] + "\n[truncated]"

        returncode = proc.returncode or 0
        if returncode != 0:
            return ToolResult(
                content=f"[exit {returncode}]\n{output}",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=output or "[no output]")
