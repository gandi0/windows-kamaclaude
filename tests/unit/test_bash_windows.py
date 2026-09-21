from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest

from kama_claude.core.tools.builtin.bash import BashTool

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Git Bash compatibility on Windows")


@pytest.mark.asyncio
async def test_find_head_pipeline_uses_unix_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "中文 project"
    directory.mkdir()
    (directory / "a.py").write_text("", encoding="utf-8")
    (directory / "b.py").write_text("", encoding="utf-8")
    (directory / "ignore.txt").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await BashTool().invoke(
        {"command": "find './中文 project' -name '*.py' -type f | sort | head -1"}
    )

    assert not result.is_error, result.content
    assert result.content.strip() == "./中文 project/a.py"


@pytest.mark.asyncio
async def test_chinese_stdout_and_stderr_are_readable() -> None:
    result = await BashTool().invoke(
        {"command": "printf '%s\\n' '中文输出'; printf '%s\\n' '中文错误' >&2; exit 2"}
    )

    assert result.is_error
    assert "[exit 2]" in result.content
    assert "中文输出" in result.content
    assert "中文错误" in result.content
    assert "\ufffd" not in result.content


@pytest.mark.asyncio
async def test_native_windows_output_uses_local_encoding() -> None:
    script = "import locale, sys; sys.stdout.buffer.write('中文输出'.encode(locale.getencoding()))"
    command = f"{shlex.quote(Path(sys.executable).as_posix())} -c {shlex.quote(script)}"

    result = await BashTool().invoke({"command": command})

    assert not result.is_error, result.content
    assert result.content == "中文输出"


@pytest.mark.asyncio
async def test_invalid_bash_path_reports_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAMA_BASH_PATH", str(tmp_path / "missing-bash.exe"))

    result = await BashTool().invoke({"command": "echo hello"})

    assert result.is_error
    assert "KAMA_BASH_PATH does not exist" in result.content
