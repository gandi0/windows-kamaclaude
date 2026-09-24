from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast


class WorkspaceError(Exception):
    """Workspace identity or file guard failure."""


# 将工作区路径规范化为可比较的绝对路径
def resolve_workspace(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise WorkspaceError("workspace must be an absolute path")
    if str(candidate).startswith(("\\\\", "//")):
        raise WorkspaceError("workspace cannot use a UNC path")
    return candidate.resolve(strict=False)


# 计算文件内容的稳定 SHA-256
def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise WorkspaceError(f"cannot hash workspace file {path}: {exc}") from exc
    return digest.hexdigest()


# 对目录内容生成稳定快照摘要
def _directory_hash(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        entries = sorted(path.iterdir(), key=lambda item: item.name.casefold())
        for item in entries:
            try:
                stat = item.stat()
                kind = "d" if item.is_dir() else "f" if item.is_file() else "o"
                line = (
                    f"{item.name}\0{kind}\0{stat.st_dev}\0{stat.st_ino}\0"
                    f"{stat.st_size}\0{stat.st_mtime_ns}\n"
                )
            except OSError as exc:
                raise WorkspaceError(f"cannot stat workspace entry {item}: {exc}") from exc
            digest.update(line.encode("utf-8", "surrogatepass"))
    except OSError as exc:
        raise WorkspaceError(f"cannot list workspace directory {path}: {exc}") from exc
    return digest.hexdigest()


# 读取路径的存在性、身份、类型和内容摘要
def snapshot_path(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve(strict=False)
    snapshot: dict[str, Any] = {
        "path": str(resolved),
        "exists": False,
        "kind": "missing",
        "dev": None,
        "ino": None,
        "size": None,
        "mtime_ns": None,
        "sha256": None,
    }
    try:
        stat = resolved.stat()
    except FileNotFoundError:
        return snapshot
    except OSError as exc:
        raise WorkspaceError(f"cannot stat workspace path {resolved}: {exc}") from exc
    snapshot.update(
        {
            "exists": True,
            "kind": "dir" if resolved.is_dir() else "file" if resolved.is_file() else "other",
            "dev": int(stat.st_dev),
            "ino": int(stat.st_ino),
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        }
    )
    if snapshot["kind"] == "file":
        snapshot["sha256"] = _file_hash(resolved)
    elif snapshot["kind"] == "dir":
        snapshot["sha256"] = _directory_hash(resolved)
    return snapshot


# 记录工作区根目录的规范路径与文件系统身份
def workspace_identity(workspace: str | Path) -> dict[str, Any]:
    root = resolve_workspace(workspace)
    source = Path(workspace).expanduser()
    if not source.is_absolute():
        raise WorkspaceError("workspace must be an absolute path")
    try:
        stat = root.stat()
    except OSError as exc:
        raise WorkspaceError(f"cannot stat workspace directory {root}: {exc}") from exc
    if not root.is_dir():
        raise WorkspaceError(f"workspace is not an existing directory: {root}")
    return {
        "path": str(root),
        "source_path": str(source.absolute()),
        "exists": True,
        "dev": int(stat.st_dev),
        "ino": int(stat.st_ino),
        "kind": "dir",
    }


# 判断两个工作区身份是否代表同一个目录
def same_workspace_identity(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    return (
        str(expected.get("path", "")) == str(actual.get("path", ""))
        and bool(expected.get("exists")) == bool(actual.get("exists"))
        and expected.get("dev") == actual.get("dev")
        and expected.get("ino") == actual.get("ino")
    )


# 将工具调用参数转换成待检查的相关文件路径
def _related_paths(
    root: Path, name: str, input_value: dict[str, Any]
) -> list[tuple[str, str, str]]:
    lower_name = name.casefold()
    if lower_name not in {"read_file", "write_file", "list_dir"}:
        return []
    raw = input_value.get("path", input_value.get("file_path", input_value.get("directory")))
    if not isinstance(raw, (str, Path)) or not str(raw):
        return []
    path = Path(raw).expanduser()
    source = path if path.is_absolute() else root / path
    source = Path(source.absolute())
    resolved = source.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WorkspaceError(f"workspace target is outside root: {resolved}") from exc
    mode = (
        "read" if lower_name == "read_file" else "write" if lower_name == "write_file" else "list"
    )
    return [(mode, str(source), str(resolved))]


# 创建一次工具调用的根目录和相关文件快照
def make_call_guard(
    workspace: str | Path,
    name: str,
    input_value: dict[str, Any],
    supplied: Any = None,
) -> dict[str, Any]:
    root = resolve_workspace(workspace)
    guard: dict[str, Any] = {
        "version": 1,
        "root": workspace_identity(root),
        "files": [],
        "unknown_file_set": name.casefold() in {"bash", "shell", "run_shell", "exec"},
        "captured_at": None,
    }
    if isinstance(supplied, dict):
        custom = json.loads(json.dumps(supplied, ensure_ascii=False))
        guard.update(custom)
        guard["root"] = workspace_identity(root)
    for mode, source_path, raw_path in _related_paths(root, str(name), input_value):
        entry = snapshot_path(raw_path)
        entry["mode"] = mode
        entry["source_path"] = source_path
        guard["files"].append(entry)
    return guard


# 比较 guard 中保存的工作区与文件状态
def check_call_guard(guard: dict[str, Any] | None, workspace: str | Path) -> str | None:
    if not guard:
        return None
    root = workspace_identity(workspace)
    expected_root = guard.get("root")
    if not isinstance(expected_root, dict) or not same_workspace_identity(expected_root, root):
        return "workspace_identity_changed"
    source_root = expected_root.get("source_path")
    if isinstance(source_root, str):
        try:
            source_identity = workspace_identity(source_root)
        except WorkspaceError:
            return "workspace_identity_changed"
        if not same_workspace_identity(expected_root, source_identity):
            return "workspace_identity_changed"
    files = guard.get("files", [])
    if not isinstance(files, list):
        return "invalid_workspace_guard"
    for expected in files:
        if not isinstance(expected, dict) or not isinstance(expected.get("path"), str):
            return "invalid_workspace_guard"
        source_path = expected.get("source_path", expected["path"])
        if not isinstance(source_path, str):
            return "invalid_workspace_guard"
        source = Path(source_path).absolute()
        current_path = source.resolve(strict=False)
        if str(current_path) != str(expected["path"]):
            return f"workspace_path_changed:{source}"
        try:
            current_path.relative_to(resolve_workspace(workspace))
        except ValueError:
            return f"workspace_target_outside_root:{current_path}"
        actual = snapshot_path(current_path)
        keys = ("exists", "kind", "dev", "ino", "size", "mtime_ns", "sha256")
        if any(expected.get(key) != actual.get(key) for key in keys):
            return f"workspace_file_changed:{expected['path']}"
    return None


# 刷新调用相关文件的累计预期状态
def refresh_call_guard(guard: dict[str, Any] | None) -> dict[str, Any] | None:
    if guard is None:
        return None
    refreshed = cast(dict[str, Any], json.loads(json.dumps(guard, ensure_ascii=False)))
    for item in refreshed.get("files", []):
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            source_path = item.get("source_path", item["path"])
            if not isinstance(source_path, str):
                raise WorkspaceError("invalid workspace source path")
            source = Path(source_path).absolute()
            current_path = source.resolve(strict=False)
            if str(current_path) != str(item["path"]):
                raise WorkspaceError(f"workspace path changed: {source}")
            current = snapshot_path(current_path)
            mode = item.get("mode")
            original_source = source_path
            item.clear()
            item.update(current)
            if mode is not None:
                item["mode"] = mode
            item["source_path"] = original_source
    return refreshed


__all__ = [
    "WorkspaceError",
    "check_call_guard",
    "make_call_guard",
    "refresh_call_guard",
    "resolve_workspace",
    "same_workspace_identity",
    "snapshot_path",
    "workspace_identity",
]
