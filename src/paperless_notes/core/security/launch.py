"""The release boundary at process start: which paths from the command line or from a second instance
may be opened, and which inherited environment variables a frozen build refuses to honour.

Only local, absolute paths to ``.md``, ``.markdown`` and ``.txt`` files open. Network, device, ``subst``
and name-surrogate reparse paths are refused before the file system is touched, so a crafted shortcut or
file association cannot redirect the app to a network share. Folders, temporary save files and other
file types are refused.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
from collections.abc import Callable, MutableMapping, Sequence
from dataclasses import dataclass

from paperless_notes.core import pathid
from paperless_notes.core.security.names import has_control_chars, is_device_name

NOTE_SUFFIXES = (".md", ".markdown", ".txt")
MAX_LAUNCH_PATHS = 64
MAX_PATH_CHARS = 32_767
IGNORED_WHEN_FROZEN = (
    "QT_QPA_PLATFORM_PLUGIN_PATH",
    "QT_QPA_PLATFORM",
    "QT_QPA_GENERIC_PLUGINS",
    "QT_STYLE_OVERRIDE",
    "QT_DEBUG_PLUGINS",
)
_LONG = "\\\\?\\"
_LONG_UNC = "\\\\?\\UNC\\"
_DRIVE_REMOTE = 4
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ATTRIBUTE_TAG_INFO = 9
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_OPEN_EXISTING = 3
_SHARE_ALL = 1 | 2 | 4
_NAME_SURROGATE = 0x20000000

_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_GET_DRIVE_TYPE = _KERNEL32.GetDriveTypeW
_GET_DRIVE_TYPE.argtypes = [ctypes.c_wchar_p]
_GET_DRIVE_TYPE.restype = ctypes.c_uint32
_QUERY_DOS_DEVICE = _KERNEL32.QueryDosDeviceW
_QUERY_DOS_DEVICE.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
_QUERY_DOS_DEVICE.restype = ctypes.c_uint32
_GET_FILE_ATTRIBUTES = _KERNEL32.GetFileAttributesW
_GET_FILE_ATTRIBUTES.argtypes = [ctypes.c_wchar_p]
_GET_FILE_ATTRIBUTES.restype = ctypes.c_uint32
_CREATE_FILE = _KERNEL32.CreateFileW
_CREATE_FILE.argtypes = [
    ctypes.c_wchar_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
]
_CREATE_FILE.restype = ctypes.c_void_p
_GET_FILE_INFORMATION = _KERNEL32.GetFileInformationByHandleEx
_GET_FILE_INFORMATION.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
_GET_FILE_INFORMATION.restype = ctypes.c_int
_CLOSE_HANDLE = _KERNEL32.CloseHandle
_CLOSE_HANDLE.argtypes = [ctypes.c_void_p]
_CLOSE_HANDLE.restype = ctypes.c_int


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("attributes", ctypes.c_uint32), ("reparse_tag", ctypes.c_uint32)]


@dataclass(frozen=True, slots=True)
class LaunchPaths:
    accepted: tuple[str, ...] = ()
    refused: int = 0


def is_local_absolute(path: str) -> bool:
    """A drive-letter path such as ``C:\\Notes\\a.md`` (also in long-path form), never UNC or a device."""
    candidate = path.replace("/", "\\")
    if candidate.upper().startswith(_LONG_UNC):
        return False
    if candidate.startswith(_LONG):
        candidate = candidate[len(_LONG) :]
    elif candidate.startswith("\\\\"):
        return False
    drive, rest = ntpath.splitdrive(candidate)
    return len(drive) == 2 and drive[0].isalpha() and drive[1] == ":" and rest.startswith("\\")


def is_remote_drive(path: str) -> bool:
    """Whether the drive-letter path is backed by a mapped network drive, without opening the path."""
    drive = ntpath.splitdrive(pathid.normalize(path))[0]
    if len(drive) != 2:
        return False
    return int(_GET_DRIVE_TYPE(drive + "\\")) == _DRIVE_REMOTE


def is_subst_drive(path: str) -> bool:
    """Whether the drive is redirected by ``subst``, without resolving or opening its target."""
    drive = ntpath.splitdrive(pathid.normalize(path))[0]
    if len(drive) != 2:
        return False
    target = ctypes.create_unicode_buffer(32_768)
    size = _QUERY_DOS_DEVICE(drive, target, len(target))
    return bool(size) and target.value.startswith("\\??\\")


def has_name_surrogate_reparse(path: str) -> bool:
    """Whether an existing path component is a junction or symbolic link.

    Components are inspected without following the reparse point. Cloud placeholders are not name
    surrogates and remain usable; a failure to inspect a reparse point is refused closed.
    """
    normalized = pathid.normalize(path)
    drive, rest = ntpath.splitdrive(normalized)
    current = drive + "\\"
    invalid_handle = ctypes.c_void_p(-1).value
    for component in (part for part in rest.split("\\") if part):
        current = ntpath.join(current, component)
        os_path = pathid.to_os_path(current)
        attributes = int(_GET_FILE_ATTRIBUTES(os_path))
        if attributes == 0xFFFFFFFF:
            return False
        if not attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            continue
        handle = _CREATE_FILE(
            os_path,
            0,
            _SHARE_ALL,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if handle == invalid_handle:
            return True
        try:
            info = _FileAttributeTagInfo()
            ok = _GET_FILE_INFORMATION(
                handle,
                _FILE_ATTRIBUTE_TAG_INFO,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok or info.reparse_tag & _NAME_SURROGATE:
                return True
        finally:
            _CLOSE_HANDLE(handle)
    return False


def qt_arguments(args: Sequence[str]) -> list[str]:
    """Only the executable name reaches Qt; all other arguments are app data, never Qt options."""
    return [args[0]] if args else []


def note_paths(
    candidates: Sequence[str],
    is_dir: Callable[[str], bool] = os.path.isdir,
    cwd: str | None = None,
    is_remote: Callable[[str], bool] = is_remote_drive,
    is_subst: Callable[[str], bool] = is_subst_drive,
    has_name_surrogate: Callable[[str], bool] = has_name_surrogate_reparse,
) -> LaunchPaths:
    """The openable notes among command-line or forwarded arguments; options (``-x``) are skipped."""
    accepted: list[str] = []
    seen: set[str] = set()
    refused = 0
    for raw in candidates:
        if not raw or raw.startswith("-"):
            continue
        if len(accepted) + refused >= MAX_LAUNCH_PATHS:
            refused += 1
            continue
        if has_control_chars(raw) or len(raw) > MAX_PATH_CHARS:
            refused += 1
            continue
        path = (
            raw
            if ntpath.isabs(raw) or raw.startswith(("\\\\", "//"))
            else ntpath.join(cwd or os.getcwd(), raw)
        )
        if not is_local_absolute(path):
            refused += 1
            continue
        normalized = pathid.normalize(path)
        name = ntpath.basename(normalized)
        _drive, rest = ntpath.splitdrive(normalized)
        components = [part for part in rest.split("\\") if part]
        unsafe_name = ":" in rest or any(is_device_name(part) for part in components)
        if (
            unsafe_name
            or is_remote(normalized)
            or is_subst(normalized)
            or has_name_surrogate(normalized)
            or not name.casefold().endswith(NOTE_SUFFIXES)
            or name.startswith("~$")
            or is_dir(normalized)
        ):
            refused += 1
            continue
        key = pathid.identity(normalized)
        if key not in seen:
            seen.add(key)
            accepted.append(normalized)
    return LaunchPaths(tuple(accepted), refused)


def refused_message(count: int) -> str:
    items = "item was" if count == 1 else "items were"
    return f"{count} {items} not opened. Paperless Notes opens local .md, .markdown and .txt files."


def harden_environment(environ: MutableMapping[str, str], frozen: bool) -> list[str]:
    """In the built app, drop inherited Qt variables that could load plugins from outside the install
    folder or change the platform. Returns the names removed (never their values)."""
    if not frozen:
        return []
    removed = [name for name in IGNORED_WHEN_FROZEN if name in environ]
    for name in removed:
        environ.pop(name, None)
    return removed
