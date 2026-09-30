"""Single-instance lock and a file-based mailbox for forwarding "open this note" requests.

No sockets or named pipes: the second process drops a small JSON request into the local state folder
and exits; the running instance watches that folder.
"""

from __future__ import annotations

import ctypes
import json
import logging
import msvcrt
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from paperless_notes.core.fsops import write_local_file

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
MAX_PATHS = 64
MAX_PATH_CHARS = 32_767
MAX_REQUEST_BYTES = 256 * 1024
MAX_AGE_S = 120.0
_PID_OFFSET = 16
_ASFW_ANY = 0xFFFFFFFF


class InstanceLock:
    """Held for the life of the primary instance; released by the OS if the process dies."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_BINARY, 0o600)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            return False
        os.lseek(fd, _PID_OFFSET, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode("ascii").ljust(12))
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        except OSError as exc:
            logger.warning("Unlocking the instance lock failed: %s", exc)
        os.close(self._fd)
        self._fd = None


@dataclass(frozen=True, slots=True)
class OpenRequest:
    command: str
    paths: tuple[str, ...]
    sent_at: float


def _allow_foreground() -> None:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.AllowSetForegroundWindow(ctypes.c_uint32(_ASFW_ANY))
    except OSError as exc:
        logger.debug("AllowSetForegroundWindow unavailable: %s", exc)


def send_open_request(ipc_dir: Path, paths: list[str], now: Callable[[], float] = time.time) -> Path:
    clean = [os.path.abspath(p) for p in paths[:MAX_PATHS] if p and len(p) <= MAX_PATH_CHARS]
    payload = {
        "v": FORMAT_VERSION,
        "cmd": "open" if clean else "activate",
        "paths": clean,
        "ts": now(),
    }
    name = f"{int(now() * 1000)}-{os.getpid()}-{secrets.token_hex(4)}.json"
    target = ipc_dir / name
    write_local_file(str(target), json.dumps(payload).encode("utf-8"))
    _allow_foreground()
    return target


def parse_request(data: bytes, now: float) -> OpenRequest | None:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != FORMAT_VERSION:
        return None
    command = payload.get("cmd")
    paths = payload.get("paths")
    sent_at = payload.get("ts")
    if command not in ("open", "activate") or not isinstance(paths, list):
        return None
    if not isinstance(sent_at, int | float) or not (now - MAX_AGE_S <= sent_at <= now + 5):
        return None
    if len(paths) > MAX_PATHS:
        return None
    valid: list[str] = []
    for p in paths:
        if not isinstance(p, str) or not p or len(p) > MAX_PATH_CHARS or "\x00" in p:
            return None
        if not os.path.isabs(p):
            return None
        valid.append(p)
    return OpenRequest(command, tuple(valid), float(sent_at))


def collect_requests(ipc_dir: Path, now: float) -> list[OpenRequest]:
    requests: list[OpenRequest] = []
    try:
        entries = sorted(ipc_dir.glob("*.json"))
    except OSError as exc:
        logger.warning("Reading the request folder failed: %s", exc)
        return requests
    for entry in entries:
        try:
            if entry.stat().st_size <= MAX_REQUEST_BYTES:
                request = parse_request(entry.read_bytes(), now)
                if request is not None:
                    requests.append(request)
                else:
                    logger.warning("Ignoring invalid or stale request %s", entry.name)
            entry.unlink()
        except OSError as exc:
            logger.warning("Handling request %s failed: %s", entry.name, exc)
    return requests


class RequestInbox(QObject):
    """Primary-side listener. Emits ``received`` with each valid request."""

    received = Signal(object)

    def __init__(self, ipc_dir: Path, poll_ms: int = 2000) -> None:
        super().__init__()
        self._dir = ipc_dir
        ipc_dir.mkdir(parents=True, exist_ok=True)
        self._watcher = QFileSystemWatcher([str(ipc_dir)])
        self._watcher.directoryChanged.connect(self.check)
        self._timer = QTimer(self)
        self._timer.setInterval(poll_ms)
        self._timer.timeout.connect(self.check)
        self._timer.start()

    def check(self, *_: object) -> None:
        for request in collect_requests(self._dir, time.time()):
            self.received.emit(request)

    def set_poll_interval(self, ms: int) -> None:
        self._timer.setInterval(ms)
