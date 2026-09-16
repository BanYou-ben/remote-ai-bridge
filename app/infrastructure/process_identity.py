from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import signal
import time

from app.domain.profile import RuntimeState


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    creation_time: float
    executable_path: str


class ProcessInspector:
    def get_identity(self, pid: int) -> ProcessIdentity | None:
        if os.name == "nt":
            return _get_windows_identity(pid)
        return _get_proc_identity(pid)

    def matches(self, state: RuntimeState) -> bool:
        identity = self.get_identity(state.pid)
        if identity is None:
            return False
        same_time = abs(identity.creation_time - state.process_creation_time) < 0.1
        same_executable = _normalise_path(identity.executable_path) == _normalise_path(state.executable_path)
        return same_time and same_executable

    def is_alive(self, pid: int) -> bool:
        return self.get_identity(pid) is not None

    def terminate_owned(self, state: RuntimeState, timeout: float) -> bool:
        if not self.matches(state):
            return False
        if os.name == "nt":
            if not _terminate_windows_process(state.pid):
                return False
        else:
            os.kill(state.pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.get_identity(state.pid) is None:
                return True
            time.sleep(0.05)
        return False


def _normalise_path(value: str) -> str:
    return os.path.normcase(os.path.abspath(value))


def _get_windows_identity(pid: int) -> ProcessIdentity | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process_query_limited_information = 0x1000
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        unix_time = ticks / 10_000_000 - 11_644_473_600
        return ProcessIdentity(pid, unix_time, buffer.value)
    finally:
        kernel32.CloseHandle(handle)


def _terminate_windows_process(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process_terminate = 0x0001
    handle = kernel32.OpenProcess(process_terminate, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def _get_proc_identity(pid: int) -> ProcessIdentity | None:
    proc = Path("/proc") / str(pid)
    try:
        executable = str((proc / "exe").resolve(strict=True))
        stat_fields = (proc / "stat").read_text(encoding="utf-8").split()
        start_ticks = int(stat_fields[21])
        clock_ticks = os.sysconf("SC_CLK_TCK")
        boot_time = 0.0
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                boot_time = float(line.split()[1])
                break
        return ProcessIdentity(pid, boot_time + start_ticks / clock_ticks, executable)
    except (FileNotFoundError, PermissionError, OSError, ValueError, IndexError):
        return None
