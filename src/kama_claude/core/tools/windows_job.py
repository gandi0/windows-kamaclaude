from __future__ import annotations

import ctypes
import uuid
from ctypes import wintypes
from functools import lru_cache
from typing import Any


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
        ("max_working", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io", ctypes.c_ulonglong * 6),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("times", ctypes.c_longlong * 4), ("page_faults", wintypes.DWORD),
        ("total", wintypes.DWORD), ("active", wintypes.DWORD),
        ("terminated", wintypes.DWORD),
    ]


@lru_cache(maxsize=1)
# 声明 Win32 函数签名，避免 64 位 HANDLE 被截断
def _kernel() -> Any:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "OpenJobObjectW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                     wintypes.DWORD], wintypes.BOOL),
        "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "GetCurrentProcess": ([], wintypes.HANDLE),
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "IsProcessInJob": ([wintypes.HANDLE, wintypes.HANDLE,
                            ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


# 检查 Win32 返回值并保留系统错误码
def _checked(value: Any) -> Any:
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


class WindowsJob:
    # 创建独立 Job，父进程失去句柄时由系统终止所属进程树
    def __init__(self) -> None:
        self.name = "Local\\KamaClaude-" + uuid.uuid4().hex
        self.handle = _checked(_kernel().CreateJobObjectW(None, self.name))
        self._process_handles: list[Any] = []
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            _checked(_kernel().SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ))
        except BaseException:
            self.close()
            raise

    # 获取内核确认仍属于该 Job 的活跃进程数量
    def active_processes(self) -> int:
        accounting = _Accounting()
        _checked(_kernel().QueryInformationJobObject(
            self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
        ))
        return int(accounting.active)

    # 请求终止 Job 内全部进程，不按可复用 PID 枚举或误杀其他进程
    def terminate(self) -> None:
        capacity = 64
        while True:
            buffer = ctypes.create_string_buffer(8 + ctypes.sizeof(ctypes.c_size_t) * capacity)
            if _kernel().QueryInformationJobObject(
                self.handle, 3, buffer, len(buffer), None
            ):
                break
            error = ctypes.get_last_error()
            if error != 234 or capacity >= 65536:  # ERROR_MORE_DATA
                raise ctypes.WinError(error)
            capacity *= 2
        count = wintypes.DWORD.from_buffer(buffer, 4).value
        pids = (ctypes.c_size_t * count).from_buffer(buffer, 8)
        for pid in pids:
            handle = _kernel().OpenProcess(0x00101000, False, pid)
            if not handle:
                if ctypes.get_last_error() == 87:  # 进程已经退出并不存在
                    continue
                raise ctypes.WinError(ctypes.get_last_error())
            member = wintypes.BOOL()
            try:
                _checked(_kernel().IsProcessInJob(handle, self.handle, ctypes.byref(member)))
                if member.value:
                    self._process_handles.append(handle)
                    handle = None
            finally:
                if handle is not None:
                    _kernel().CloseHandle(handle)
        _checked(_kernel().TerminateJobObject(self.handle, 1))

    # 活跃计数归零可能早于进程句柄置位，额外等待被终止成员的内核退出状态
    def exits_confirmed(self) -> bool:
        return self.active_processes() == 0 and all(
            _kernel().WaitForSingleObject(handle, 0) == 0 for handle in self._process_handles
        )

    # 关闭本进程持有的 Job 句柄
    def close(self) -> None:
        for handle in self._process_handles:
            _kernel().CloseHandle(handle)
        self._process_handles.clear()
        if self.handle is not None:
            _kernel().CloseHandle(self.handle)
            self.handle = None


# 由受控引导进程在启动用户命令前加入父进程创建的 Job，消除派生前的竞态
def join_job(name: str) -> None:
    kernel = _kernel()
    handle = _checked(kernel.OpenJobObjectW(0x0001, False, name))
    try:
        _checked(kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()))
    finally:
        kernel.CloseHandle(handle)
