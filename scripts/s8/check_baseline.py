from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHECKS = {
    "ruff": ["-m", "ruff", "check", "src", "tests", "scripts"],
    "mypy": ["-m", "mypy", "src"],
    "unit": ["-m", "pytest", "tests/unit", "-v", "--tb=short"],
    "integration": [
        "-m", "pytest", "tests/integration", "-v", "--tb=short",
        "--ignore=tests/integration/test_run_e2e.py", "-m", "not integration",
    ],
    "protocol": ["scripts/gen_protocol_doc.py", "--check"],
}
EXTRA_CHECKS = {
    "s85": ["scripts/s8/run_s85_matrix.py"],
    "s84": ["-m", "pytest", "tests/unit/test_s84_compaction.py",
            "tests/integration/test_s84_compaction.py", "-v", "--tb=short"],
    "s83": ["-m", "pytest", "tests/unit/test_s83_store.py",
            "tests/unit/test_s83_execution.py", "tests/unit/test_s83_tui.py",
            "-v", "--tb=short"],
    "s83-daemon": ["scripts/s8/probe_s83.py"],
    "s82-crash": ["scripts/s8/probe_s82.py"],
    "s82-reproduction": ["scripts/s8/reproduce.py"],
    "s82": ["-m", "pytest", "tests/unit/test_s82_store.py",
            "tests/unit/test_s82_legacy.py", "tests/unit/test_s82_execution.py",
            "tests/unit/test_s82_session.py",
            "tests/unit/test_session_store.py", "tests/unit/test_session_manager.py",
            "tests/unit/test_runner.py", "-v", "--tb=short"],
    "s81": ["-m", "pytest", "tests/unit/test_s81_execution.py",
            "tests/unit/test_s81_process.py", "tests/unit/test_tool_retry.py",
            "tests/unit/test_invocation.py", "tests/unit/test_loop.py",
            "tests/unit/test_builtin_tools.py", "tests/unit/test_permission_manager.py",
            "tests/unit/test_tui_app.py", "tests/unit/test_app.py", "-v", "--tb=short"],
}


# 构造仅继承系统运行必需变量的子进程环境，隔离 Windows 用户数据及密钥
def isolated_env(scratch: Path) -> dict[str, str]:
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    profile = scratch / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    env.update({
        "USERPROFILE": str(profile),
        "APPDATA": str(profile / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(profile / "AppData" / "Local"),
        "PYTHONPATH": str(REPO / "src"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "-p pytest_asyncio.plugin",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_AUTH_TOKEN": "",
        "PYTHON_DOTENV_DISABLED": "1",
        "KAMA_CONFIG": str(scratch / "isolated-config.toml"),
    })
    return env


# 记录现有解释器、已安装依赖和本地 Git 基线，不查询远端
def environment() -> dict[str, object]:
    git_info = {}
    for name, args in {
        "head": ["rev-parse", "HEAD"],
        "branch": ["branch", "--show-current"],
        "status": ["status", "--short"],
        "git_version": ["--version"],
    }.items():
        result = subprocess.run(
            ["git", "-c", f"safe.directory={REPO.as_posix()}", *args],
            cwd=REPO, capture_output=True, text=True,
        )
        git_info[name] = result.stdout.strip()
    return {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "git": git_info,
        "dependencies": {
            item.metadata["Name"]: item.version
            for item in importlib.metadata.distributions()
        },
        "scope": "Windows native; isolated profile; source via PYTHONPATH",
        "project_requires_python": tomllib.loads((REPO / "pyproject.toml").read_text(
            encoding="utf-8"))["project"]["requires-python"],
        "excluded": "tests/integration/test_run_e2e.py (real paid API)",
    }


# 在隔离环境中运行检查并逐项保存退出码及完整输出，不隐藏失败
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", choices=["all", *CHECKS, *EXTRA_CHECKS], default="all")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    metadata = environment()
    (output / "environment.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    packages = metadata["dependencies"]
    (output / "requirements-observed.txt").write_text(
        "\n".join(f"{key}=={value}" for key, value in sorted(packages.items())) + "\n",
        encoding="utf-8",
    )
    selected = CHECKS if args.check == "all" else {args.check: (CHECKS | EXTRA_CHECKS)[args.check]}
    results = []
    for name, arguments in selected.items():
        with tempfile.TemporaryDirectory(prefix=f"kama-s8-{name}-") as directory:
            env = isolated_env(Path(directory))
            if name == "integration":
                env.update({
                    "PYTHON_DOTENV_DISABLED": "1",
                    "KAMA_CONFIG": str(Path(directory) / "isolated-config.toml"),
                })
            command = [sys.executable, *arguments]
            if name == "s83-daemon":
                command.extend(["--output", str(output / "probe.json")])
            if name == "s85":
                command.extend(["--output", str(output / "matrix")])
            if arguments[:2] == ["-m", "pytest"]:
                command.extend(["--basetemp", str(Path(directory) / "pytest")])
            started = time.monotonic()
            try:
                result = subprocess.run(
                    command, cwd=REPO, env=env, capture_output=True,
                    encoding="utf-8", errors="replace", timeout=args.timeout,
                )
                code, stdout, stderr = result.returncode, result.stdout, result.stderr
            except subprocess.TimeoutExpired as exc:
                code = 124
                stdout = (exc.stdout or b"").decode("utf-8", errors="replace")
                stderr = (exc.stderr or b"").decode("utf-8", errors="replace")
                stderr += "\nS8 harness timeout; check did not complete.\n"
            entry = {
                "check": name, "command": command, "exit_code": code,
                "seconds": round(time.monotonic() - started, 3),
            }
            (output / f"{name}.txt").write_text(
                json.dumps(entry, ensure_ascii=False) + "\n" + stdout + stderr,
                encoding="utf-8",
            )
            results.append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
    (output / "checks.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return int(any(entry["exit_code"] != 0 for entry in results))


if __name__ == "__main__":
    raise SystemExit(main())
