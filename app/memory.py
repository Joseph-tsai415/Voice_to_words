"""How much memory is left, and how much the loaded models are allowed to take.

Recognisers are big: Cohere Transcribe is 2.9 GB resident, FireRedASR2 1.2 GB,
and the cache used to hold every one ever loaded. Bounding that is worth doing
on its own - an unbounded cache is a leak whatever the machine has.

Scheduling, though, goes by the **commit limit** (RAM + page file), not physical
RAM. Measured on a 32 GB box with a 49 GB SSD page file while two big jobs ran:
16 pages/sec and 3 MB/s of disk, with 13 cores pegged. It was CPU-bound, not
swapping. Refusing to start work because physical RAM looked low would have
been wrong.
"""
from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path

log = logging.getLogger("scribe.memory")


def _windows_commit() -> tuple[int, int] | None:
    """(commit limit, commit available) - physical RAM plus the page file."""
    class Status(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullTotalPageFile), int(status.ullAvailPageFile)
    except (AttributeError, OSError):
        return None


def commit_available_bytes() -> int:
    """Memory a new allocation can actually draw on, page file included.

    Physical RAM alone is the wrong number to schedule against: a machine with
    a 49 GB page file on an SSD will happily run a 3 GB model with 3 GB of RAM
    free. Measured on exactly that setup while two big jobs ran: 16 pages/sec
    and 3 MB/s of disk - it was CPU-bound, not swapping.
    """
    if os.name == "nt":
        result = _windows_commit()
        if result:
            return result[1]
    swap_free = 0
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():                     # add free swap where we can see it
        try:
            for line in meminfo.read_text().splitlines():
                if line.startswith("SwapFree:"):
                    swap_free = int(line.split()[1]) * 1024
                    break
        except (OSError, ValueError, IndexError):
            swap_free = 0
    return available_bytes() + swap_free


def _windows_memory() -> tuple[int, int] | None:
    class Status(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullTotalPhys), int(status.ullAvailPhys)
    except (AttributeError, OSError):
        return None


def _posix_memory() -> tuple[int, int] | None:
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        values = {}
        try:
            for line in meminfo.read_text().splitlines():
                key, _, rest = line.partition(":")
                values[key] = int(rest.split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            return None
        total = values.get("MemTotal")
        available = values.get("MemAvailable", values.get("MemFree"))
        if total and available:
            return total, available
    try:
        total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        available = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        return int(total), int(available)
    except (ValueError, OSError, AttributeError):
        return None


def memory() -> tuple[int, int]:
    """(total, available) in bytes. Falls back to a conservative guess."""
    result = _windows_memory() if os.name == "nt" else _posix_memory()
    if result and result[0] > 0:
        return result
    return 8 * 1024 ** 3, 2 * 1024 ** 3


def total_bytes() -> int:
    return memory()[0]


def available_bytes() -> int:
    return memory()[1]


def configured_model_cap() -> int | None:
    """An explicit ceiling on loaded models, or None for "let the OS decide".

    Unset by default. Virtual memory already handles an idle model: its pages
    go to the page file and are never read back. Capping only helps on a
    machine with no page file, or one deliberately kept off disk.
    """
    override = os.environ.get("SCRIBE_MODEL_RAM_MB", "").strip()
    if not override:
        return None
    try:
        return max(1, int(override)) * 1024 ** 2
    except ValueError:
        return None


def describe() -> dict[str, float]:
    total, available = memory()
    return {
        "total_mb": round(total / 1e6),
        "available_mb": round(available / 1e6),
        "commit_available_mb": round(commit_available_bytes() / 1e6),
        "model_cap_mb": (round(cap / 1e6) if (cap := configured_model_cap()) else None),
    }
