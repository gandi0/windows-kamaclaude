from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

_SYSTEM_ENV_KEYS = (
    "COMSPEC",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
)


# 返回本次 probe 所在的 worktree 根目录
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# 构造仅保留系统基础变量且屏蔽真实用户配置、密钥和 MCP 的 pytest 环境
def _probe_environment(root: Path, temp_root: Path, artifacts_root: Path) -> dict[str, str]:
    env = {
        key: os.environ[key]
        for key in _SYSTEM_ENV_KEYS
        if os.environ.get(key) is not None
    }
    profile = temp_root / "pytest-profile"
    profile.mkdir(parents=True, exist_ok=True)
    artifacts_root.mkdir(parents=True, exist_ok=True)
    config = profile / "config.toml"
    config.write_text(
        '[logging]\nlevel = "WARNING"\nfile = ""\n',
        encoding="utf-8",
    )
    env.update(
        {
            "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root)]),
            "PYTHON_DOTENV_DISABLED": "1",
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_AUTH_TOKEN": "",
            "HOME": str(profile),
            "USERPROFILE": str(profile),
            "APPDATA": str(profile / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(profile / "AppData" / "Local"),
            "KAMA_CONFIG": str(config),
            "KAMA_HOST": "127.0.0.1",
            "KAMA_PORT": "0",
            "KAMA_TRACE_ENABLED": "0",
            "KAMA_LOG_FILE": "",
            "KAMA_LOG_LEVEL": "WARNING",
            "KAMA_S83_ARTIFACTS": str(artifacts_root),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
    )
    return env


# 收集 pytest 临时 profile 中的 JSONL daemon 操作证据
def _collect_operation_logs(temp_root: Path) -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []
    for path in sorted(temp_root.rglob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        rows: list[Any] = []
        for line in lines:
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        logs.append({"path": str(path), "rows": rows})
    return logs


# 收集 TUI 产生的 SVG 和操作 JSONL，避免把 daemon 数据库复制进证据
def _collect_artifacts(artifacts_root: Path) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    if not artifacts_root.exists():
        return artifacts
    for path in sorted(artifacts_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".svg", ".jsonl"}:
            continue
        item: dict[str, Any] = {
            "path": str(path),
            "kind": path.suffix.lower().lstrip("."),
            "bytes": path.stat().st_size,
        }
        if path.suffix.lower() == ".svg":
            item["text"] = path.read_text(encoding="utf-8", errors="replace")
        artifacts.append(item)
    return artifacts


# 将超时异常中的文本统一转换为可写入 JSON 证据的字符串
def _output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


# 运行全部 S8.3 真实 daemon pytest 场景并保存完整 JSON 证据
def main() -> int:
    parser = argparse.ArgumentParser(description="Run isolated S8.3 daemon recovery probes")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("s83-probe-evidence.json"),
        help="JSON evidence output path",
    )
    parser.add_argument(
        "--pytest-arg",
        action="append",
        default=[],
        help="additional pytest argument; repeatable",
    )
    args = parser.parse_args()
    root = _repo_root()
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="s83-pytest-"))
    artifacts_root = output.parent / f"s83-artifacts-{uuid.uuid4().hex[:12]}"
    artifacts_root.mkdir()
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/integration/test_s83_recovery.py",
        "tests/integration/test_s83_tui_live.py",
        "-q",
        "-s",
        "-p",
        "pytest_asyncio.plugin",
        "--basetemp",
        str(temp_root / "pytest"),
        *args.pytest_arg,
    ]
    started = time.time()
    timed_out = False
    timeout_error: str | None = None
    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            env=_probe_environment(root, temp_root, artifacts_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=600,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        timeout_error = str(exc)
        returncode = 124
        stdout = _output_text(exc.stdout)
        stderr = _output_text(exc.stderr)
    finished = time.time()
    operation_logs = _collect_operation_logs(temp_root)
    artifacts = _collect_artifacts(artifacts_root)
    daemon_logs = [
        {"path": str(path), "text": path.read_text(encoding="utf-8", errors="replace")}
        for path in sorted(temp_root.rglob("daemon-*.log"))
    ]
    evidence = {
        "scope": "S8.3 isolated real daemon; fake provider; local tools only",
        "command": command,
        "cwd": str(root),
        "returncode": returncode,
        "timed_out": timed_out,
        "timeout_s": 600,
        "timeout_error": timeout_error,
        "started_at": started,
        "finished_at": finished,
        "duration_s": finished - started,
        "stdout": stdout,
        "stderr": stderr,
        "operation_logs": operation_logs,
        "daemon_logs": daemon_logs,
        "artifacts": artifacts,
        "pytest_temp_root": str(temp_root),
        "artifact_root": str(artifacts_root),
    }
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "returncode": returncode,
        "duration_s": round(finished - started, 3),
        "operation_log_count": len(operation_logs),
        "artifact_count": len(artifacts),
        "timed_out": timed_out,
    }, ensure_ascii=False))
    return int(returncode)


if __name__ == "__main__":
    raise SystemExit(main())
