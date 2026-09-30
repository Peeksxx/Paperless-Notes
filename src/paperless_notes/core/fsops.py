"""File system access for notes: stamps, atomic replace and transient-error classification."""

from __future__ import annotations

import ctypes
import hashlib
import logging
import msvcrt
import ntpath
import os
import re
import secrets
import time
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from paperless_notes.core import pathid
from paperless_notes.core.onedrive import (
    FILE_ATTRIBUTE_OFFLINE,
    FILE_ATTRIBUTE_PINNED,
    FILE_ATTRIBUTE_READONLY,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
    FILE_ATTRIBUTE_REPARSE_POINT,
    FILE_ATTRIBUTE_UNPINNED,
    TEMP_PREFIX,
)

logger = logging.getLogger(__name__)

TRANSIENT_WINERRORS = frozenset({5, 32, 33, 1175, 1176, 1224})


@dataclass(frozen=True, slots=True)
class StatInfo:
    size: int
    mtime_ns: int
    attributes: int = 0
    reparse_tag: int = 0
    file_id: int = 0

    @property
    def read_only(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_READONLY)


@dataclass(frozen=True, slots=True)
class FileStamp:
    """What we last knew to be on disk. Only the hash decides whether content changed."""

    size: int
    mtime_ns: int
    sha256: str
    file_id: int = 0
    attributes: int = 0
    reparse_tag: int = 0

    @classmethod
    def of(cls, st: StatInfo, sha256: str) -> FileStamp:
        return cls(st.size, st.mtime_ns, sha256, st.file_id, st.attributes, st.reparse_tag)

    def same_stat(self, st: StatInfo) -> bool:
        return st.size == self.size and st.mtime_ns == self.mtime_ns


class Expect(Enum):
    ABSENT = "absent"
    ANY = "any"


type Expected = FileStamp | Expect


class ExternalChangeError(Exception):
    """The file on disk is not the version this write was based on."""


class TooLargeError(OSError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in TRANSIENT_WINERRORS


class FileSystem(Protocol):
    def stat(self, path: str) -> StatInfo | None: ...
    def read_bytes(self, path: str, limit: int) -> bytes: ...
    def write_temp(self, directory: str, target_name: str, data: bytes) -> str: ...
    def replace(self, source: str, target: str) -> None: ...
    def rename_new(self, source: str, target: str) -> None: ...
    def remove(self, path: str) -> None: ...
    def list_dir(self, directory: str, limit: int | None = None) -> list[str]: ...
    def append(self, path: str, data: bytes) -> None: ...
    def make_dirs(self, directory: str) -> None: ...


_GENERIC_READ = 0x80000000
_SHARE_ALL = 0x1 | 0x2 | 0x4
_OPEN_EXISTING = 3
_FLAG_SEQUENTIAL_SCAN = 0x08000000
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_kernel32.CreateFileW.restype = wintypes.HANDLE


_CLOUD_HINTS = (
    FILE_ATTRIBUTE_PINNED
    | FILE_ATTRIBUTE_UNPINNED
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
    | FILE_ATTRIBUTE_OFFLINE
)
_EXPOSE_PLACEHOLDERS = 2


class _FindData(ctypes.Structure):
    _fields_ = (
        ("attributes", wintypes.DWORD),
        ("created", wintypes.FILETIME),
        ("accessed", wintypes.FILETIME),
        ("written", wintypes.FILETIME),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("reserved0", wintypes.DWORD),
        ("reserved1", wintypes.DWORD),
        ("name", wintypes.WCHAR * 260),
        ("short_name", wintypes.WCHAR * 14),
    )


_kernel32.FindFirstFileW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_FindData)]
_kernel32.FindFirstFileW.restype = wintypes.HANDLE
_kernel32.FindClose.argtypes = [wintypes.HANDLE]
_set_thread_placeholder_mode = getattr(
    ctypes.WinDLL("ntdll"), "RtlSetThreadPlaceholderCompatibilityMode", None
)
if _set_thread_placeholder_mode is not None:
    _set_thread_placeholder_mode.argtypes = [ctypes.c_byte]
    _set_thread_placeholder_mode.restype = ctypes.c_byte


def cloud_attributes(os_path: str) -> tuple[int, int] | None:
    """Attributes and reparse tag of one file as the cloud files filter really has them.

    Windows disguises placeholders from ordinary processes, so ``os.stat`` shows neither the reparse point
    nor its tag. This exposes placeholders for the calling thread only, reads the directory entry of this
    single file (no data, no download) and restores the thread's previous mode.
    """
    previous = None
    if _set_thread_placeholder_mode is not None:
        previous = _set_thread_placeholder_mode(_EXPOSE_PLACEHOLDERS)
    try:
        data = _FindData()
        handle = _kernel32.FindFirstFileW(os_path, ctypes.byref(data))
        if handle is None or handle == _INVALID_HANDLE:
            return None
        _kernel32.FindClose(handle)
    finally:
        if _set_thread_placeholder_mode is not None and previous is not None and previous >= 0:
            _set_thread_placeholder_mode(previous)
    attributes = int(data.attributes)
    tag = int(data.reserved0) if attributes & FILE_ATTRIBUTE_REPARSE_POINT else 0
    return attributes, tag


def _open_shared_read(os_path: str) -> int:
    """Open for reading while other processes may still rename or delete the file (a rename made on
    another PC must not fail because we happen to be reading). Replacing it still waits for the handle."""
    handle = _kernel32.CreateFileW(
        os_path, _GENERIC_READ, _SHARE_ALL, None, _OPEN_EXISTING, _FLAG_SEQUENTIAL_SCAN, None
    )
    if handle is None or handle == _INVALID_HANDLE:
        code = ctypes.get_last_error()
        raise OSError(None, ctypes.FormatError(code).strip(), os_path, code)
    return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)


class WindowsFileSystem:
    """Real file system. Replacement is a single ``MoveFileExW(REPLACE_EXISTING)`` via ``os.replace``.

    For files inside ``cloud_roots`` (or carrying cloud attributes) ``stat`` reports the placeholder's real
    attributes and reparse tag, which the sync monitor needs to read OneDrive's in-sync state.
    """

    def __init__(self, cloud_roots: Sequence[str] = ()) -> None:
        self._cloud_roots = tuple(cloud_roots)

    def stat(self, path: str) -> StatInfo | None:
        os_path = pathid.to_os_path(path)
        try:
            st = os.stat(os_path)
        except (FileNotFoundError, NotADirectoryError):
            return None
        attributes = getattr(st, "st_file_attributes", 0)
        tag = getattr(st, "st_reparse_tag", 0)
        if not tag and (attributes & _CLOUD_HINTS or self._in_cloud_root(path)):
            found = cloud_attributes(os_path)
            if found is not None:
                attributes, tag = found
        return StatInfo(st.st_size, st.st_mtime_ns, attributes, tag, st.st_ino)

    def _in_cloud_root(self, path: str) -> bool:
        return any(pathid.is_within(path, root) for root in self._cloud_roots)

    def read_bytes(self, path: str, limit: int) -> bytes:
        with os.fdopen(_open_shared_read(pathid.to_os_path(path)), "rb") as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            raise TooLargeError(f"file exceeds {limit} bytes")
        return data

    def write_temp(self, directory: str, target_name: str, data: bytes) -> str:
        temp = ntpath.join(directory, f"{TEMP_PREFIX}{target_name}.{secrets.token_hex(4)}.tmp")
        os_temp = pathid.to_os_path(temp)
        try:
            with open(os_temp, "xb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            _remove_quietly(self, temp)
            raise
        return temp

    def replace(self, source: str, target: str) -> None:
        os.replace(pathid.to_os_path(source), pathid.to_os_path(target))

    def rename_new(self, source: str, target: str) -> None:
        os.rename(pathid.to_os_path(source), pathid.to_os_path(target))

    def remove(self, path: str) -> None:
        try:
            os.remove(pathid.to_os_path(path))
        except FileNotFoundError:
            return

    def list_dir(self, directory: str, limit: int | None = None) -> list[str]:
        names: list[str] = []
        with os.scandir(pathid.to_os_path(directory)) as entries:
            for entry in entries:
                if limit is not None and len(names) >= limit:
                    break
                names.append(entry.name)
        return names

    def append(self, path: str, data: bytes) -> None:
        with open(pathid.to_os_path(path), "ab") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    def make_dirs(self, directory: str) -> None:
        os.makedirs(pathid.to_os_path(directory), exist_ok=True)


def _remove_quietly(fs: FileSystem, path: str) -> None:
    try:
        fs.remove(path)
    except OSError as exc:
        logger.warning("Could not remove temp file %s: %s", path, exc)


def check_expected(fs: FileSystem, path: str, expected: Expected, limit: int) -> None:
    st = fs.stat(path)
    if expected is Expect.ANY:
        return
    if expected is Expect.ABSENT:
        if st is not None:
            raise ExternalChangeError("a file already exists at the target path")
        return
    if st is None:
        raise FileNotFoundError(2, "file is missing", path)
    if digest(fs.read_bytes(path, limit)) != expected.sha256:
        raise ExternalChangeError("file changed on disk")


def atomic_save(fs: FileSystem, path: str, data: bytes, expected: Expected, limit: int) -> FileStamp:
    """Write ``data`` to ``path`` without ever exposing a partial file.

    The temp file lives in the same folder so the final step is a rename on one volume. The disk
    version is re-checked right before the rename so a concurrent external change is never replaced.
    """
    directory, name = ntpath.split(pathid.normalize(path))
    temp = fs.write_temp(directory, name, data)
    try:
        check_expected(fs, path, expected, limit)
        if expected is Expect.ABSENT:
            fs.rename_new(temp, path)
        else:
            fs.replace(temp, path)
    except BaseException:
        _remove_quietly(fs, temp)
        raise
    st = fs.stat(path)
    if st is None:
        raise FileNotFoundError(2, "file vanished right after it was written", path)
    return FileStamp.of(st, digest(data))


def write_local_file(
    path: str,
    data: bytes,
    fs: FileSystem | None = None,
    attempts: int = 6,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Atomically write a machine-local state file, retrying briefly on sharing violations."""
    fs = fs or WindowsFileSystem()
    fs.make_dirs(ntpath.dirname(pathid.normalize(path)))
    delay = 0.05
    for attempt in range(attempts):
        try:
            atomic_save(fs, path, data, Expect.ANY, limit=len(data) + 1)
            return
        except OSError as exc:
            if not is_transient(exc) or attempt == attempts - 1:
                raise
            sleep(delay)
            delay *= 2


_TEMP_NAME = re.compile(re.escape(TEMP_PREFIX) + r".+\.[0-9a-f]{8}\.tmp\Z")
STALE_TEMP_AGE_NS = 10 * 60 * 1_000_000_000


def cleanup_stale_temps(fs: FileSystem, directory: str, now_ns: int | None = None) -> int:
    """Remove temp files left by a crash. Only our own names, and only once they are old enough
    that no other instance can still be writing them."""
    cutoff = (time.time_ns() if now_ns is None else now_ns) - STALE_TEMP_AGE_NS
    removed = 0
    for name in fs.list_dir(directory):
        if not _TEMP_NAME.match(name):
            continue
        path = ntpath.join(directory, name)
        st = fs.stat(path)
        if st is not None and st.mtime_ns < cutoff:
            _remove_quietly(fs, path)
            removed += 1
    return removed
