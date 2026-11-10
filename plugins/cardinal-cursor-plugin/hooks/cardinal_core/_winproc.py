"""Process facts on Windows, from kernel32 through ctypes.

Windows has no `ps`, and `os.kill(pid, 0)` is not a probe there: signal 0 is
CTRL_C_EVENT, so the call raises or sends Ctrl+C to the console's processes.
Nothing here signals or changes another process. Call only on Windows.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Optional

SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x102
ERROR_ACCESS_DENIED = 5
TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k.CloseHandle.argtypes = (wintypes.HANDLE,)
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    for walk in (k.Process32FirstW, k.Process32NextW):
        walk.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    return k


def alive(pid: int) -> bool:
    """Whether process `pid` exists and has not exited. A process that
    cannot be opened for lack of access exists."""
    k = _kernel32()
    handle = k.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        return k.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        k.CloseHandle(handle)


def parent(pid: int) -> Optional[tuple]:
    """(parent pid, executable name) of `pid`, or None when it is not running."""
    k = _kernel32()
    snapshot = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE:
        return None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        found = k.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32ProcessID == pid:
                return int(entry.th32ParentProcessID), entry.szExeFile
            found = k.Process32NextW(snapshot, ctypes.byref(entry))
        return None
    finally:
        k.CloseHandle(snapshot)
