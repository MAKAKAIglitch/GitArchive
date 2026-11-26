"""Windows background-process priority helpers for GitArchive."""

from __future__ import annotations

import ctypes
import os
import subprocess
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

PROCESS_MEMORY_PRIORITY_CLASS = 0x27
PROCESS_SET_INFORMATION = 0x0200


def normalize_cpu_priority(value: str | None) -> str:
    value = str(value or "idle").strip().lower()
    return value if value in CPU_PRIORITY_CLASSES else "idle"


def normalize_memory_priority(value: str | None) -> str:
    value = str(value or "very_low").strip().lower()
    return value if value in MEMORY_PRIORITY_VALUES else "very_low"


def memory_priority_value(value: str | None) -> int:
    return MEMORY_PRIORITY_VALUES[normalize_memory_priority(value)]


def process_creation_flags(cpu_priority: str | None, hide_console: bool = True) -> int:
    """Return CreateProcess flags; priority is applied at process creation on Windows."""
    if os.name != "nt":
        return 0
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if hide_console else 0
    flags |= CPU_PRIORITY_CLASSES[normalize_cpu_priority(cpu_priority)]
    return flags


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
        wintypes.INT,
        ctypes.POINTER(MemoryPriorityInformation),
        wintypes.DWORD,
    ]
    setter.restype = wintypes.BOOL
    info = MemoryPriorityInformation(value)
    process_handle = wintypes.HANDLE(getattr(handle, "value", handle))
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
