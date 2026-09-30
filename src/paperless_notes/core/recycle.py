"""Delete to the Recycle Bin with ``SHFileOperationW``; no dependency.

``FOF_WANTNUKEWARNING`` makes Windows ask before a permanent delete (for example on a volume without a
Recycle Bin), so a note is never silently destroyed.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from ctypes import wintypes

from paperless_notes.core import pathid

FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000
RECYCLE_FLAGS = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


class RecycleError(OSError):
    pass


def build_operation(path: str, hwnd: int | None = None) -> SHFILEOPSTRUCTW:
    target = pathid.normalize(path)
    if "\x00" in target or not target:
        raise ValueError("invalid path")
    op = SHFILEOPSTRUCTW()
    op.hwnd = hwnd
    op.wFunc = FO_DELETE
    op.pFrom = target + "\x00"
    op.pTo = None
    op.fFlags = RECYCLE_FLAGS
    return op


def _shell_call(op: SHFILEOPSTRUCTW) -> int:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    return int(shell32.SHFileOperationW(ctypes.byref(op)))


def recycle(path: str, hwnd: int | None = None, call: Callable[[SHFILEOPSTRUCTW], int] = _shell_call) -> None:
    """Move ``path`` to the Recycle Bin. Raises :class:`RecycleError` on failure or cancellation."""
    op = build_operation(path, hwnd)
    code = call(op)
    if code != 0:
        raise RecycleError(code, f"the file could not be moved to the Recycle Bin (code {code:#x})")
    if op.fAnyOperationsAborted:
        raise RecycleError(1223, "the delete was cancelled")
