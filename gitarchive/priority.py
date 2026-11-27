"""Windows background-process priority helpers for GitArchive."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


CPU_PRIORITY_CLASSES = {
    "idle": 0x00000040,          # IDLE_PRIORITY_CLASS
    "below_normal": 0x00004000,  # BELOW_NORMAL_PRIORITY_CLASS
    "normal": 0x00000020,        # NORMAL_PRIORITY_CLASS
}

MEMORY_PRIORITY_VALUES = {
    "very_low": 1,
    "low": 2,
    "normal": 5,
}

PROCESS_MEMORY_PRIORITY_CLASS = 0  # ProcessMemoryPriority
PROCESS_SET_INFORMATION = 0x0200
CREATE_NO_WINDOW_FLAG = 0x08000000


def normalize_cpu_priority(value: str | None) -> str:
    value = str(value or "idle").strip().lower()
    return value if value in CPU_PRIORITY_CLASSES else "idle"


def normalize_memory_priority(value: str | None) -> str:
    value = str(value or "very_low").strip().lower()
    return value if value in MEMORY_PRIORITY_VALUES else "very_low"


def memory_priority_value(value: str | None) -> int:
    return MEMORY_PRIORITY_VALUES[normalize_memory_priority(value)]


def process_creation_flags(cpu_priority: str | None, hide_console: bool = True) -> int:
    """Return Windows CreateProcess flags for hidden, priority-adjusted background tasks."""
    if os.name != "nt":
        return 0
    flags = CREATE_NO_WINDOW_FLAG if hide_console else 0
    flags |= CPU_PRIORITY_CLASSES[normalize_cpu_priority(cpu_priority)]
    return flags


def apply_no_console_flag(arguments) -> None:
    """Add CREATE_NO_WINDOW to Qt's Windows CreateProcess arguments."""
    arguments.flags = int(arguments.flags) | CREATE_NO_WINDOW_FLAG


def hide_qprocess_console(process) -> bool:
    """Hide the console of a Qt-launched child process on Windows."""
    if os.name != "nt":
        return False
    setter = getattr(process, "setCreateProcessArgumentsModifier", None)
    if not callable(setter):
        return False
    setter(apply_no_console_flag)
    return True


def apply_memory_priority_to_handle(handle, priority: str | None) -> bool:
    """Lower a Windows process' working-set trimming priority (not its RAM limit)."""
    if os.name != "nt":
        return True
    value = memory_priority_value(priority)
    if value >= MEMORY_PRIORITY_VALUES["normal"]:
        return True

    class MemoryPriorityInformation(ctypes.Structure):
        _fields_ = [("MemoryPriority", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    setter = kernel32.SetProcessInformation
    setter.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.POINTER(MemoryPriorityInformation),
        wintypes.DWORD,
    ]
    setter.restype = wintypes.BOOL
    info = MemoryPriorityInformation(value)
    raw_handle = getattr(handle, "value", None)
    if raw_handle is None:
        raw_handle = int(handle)
    process_handle = wintypes.HANDLE(raw_handle)
    return bool(setter(
        process_handle,
        PROCESS_MEMORY_PRIORITY_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ))


def apply_memory_priority_to_pid(pid: int, priority: str | None) -> bool:
    """Open a child process briefly to set its memory trimming priority."""
    if os.name != "nt":
        return True
    if memory_priority_value(priority) >= MEMORY_PRIORITY_VALUES["normal"]:
        return True

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    opener = kernel32.OpenProcess
    opener.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    opener.restype = wintypes.HANDLE
    closer = kernel32.CloseHandle
    closer.argtypes = [wintypes.HANDLE]
    closer.restype = wintypes.BOOL

    handle = opener(PROCESS_SET_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        return apply_memory_priority_to_handle(handle, priority)
    finally:
        closer(handle)
