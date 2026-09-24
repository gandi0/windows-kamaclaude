from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from kama_claude.core.session.execution import StorageError


# 将锁路径规范化为绝对路径并拒绝 UNC 路径
def _lock_path(path: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise StorageError("daemon lock path must be absolute")
    if str(candidate).startswith(("\\\\", "//")):
        raise StorageError("daemon lock cannot use a UNC path")
    return candidate.resolve(strict=False)


class DaemonLock:
    """持有打开文件句柄的跨进程 daemon 专用锁。"""

    # 初始化锁对象但不抢占锁
    def __init__(self, path: Path) -> None:
        self.path = _lock_path(path)
        self._handle: Any = None
        self._acquired = False

    # 以非阻塞方式抢占锁文件中的固定字节
    def acquire(self) -> None:
        if self._acquired:
            return
        handle: Any = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            handle.seek(0)
            if self.path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
            self._handle = handle
            self._acquired = True
        except (OSError, ValueError) as exc:
            try:
                if handle is not None:
                    handle.close()
            except OSError:
                pass
            raise StorageError(f"daemon lock is already held: {self.path}") from exc

    # 释放文件锁并关闭句柄
    def close(self) -> None:
        if not self._acquired or self._handle is None:
            return
        handle = self._handle
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        except (OSError, ValueError) as exc:
            raise StorageError(f"cannot release daemon lock: {exc}") from exc
        finally:
            try:
                handle.close()
            finally:
                self._handle = None
                self._acquired = False

    # 将锁作为上下文管理器使用
    def __enter__(self) -> DaemonLock:
        self.acquire()
        return self

    # 离开上下文时释放锁
    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


__all__ = ["DaemonLock"]
