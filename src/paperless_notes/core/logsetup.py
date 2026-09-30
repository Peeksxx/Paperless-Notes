"""Logging, crash hooks and the Qt message bridge.

Log records carry states, counts, file names and error codes; note text is never logged. The file
handler also applies a privacy filter: an ``OSError`` or an absolute path passed to a log call is reduced
to its error code and file name, and the user profile, local and roaming app data, temp and OneDrive
folders and the user name are replaced by placeholders, so routine logs hold no full note path and no
local user name.
"""

from __future__ import annotations

import functools
import logging
import ntpath
import os
import re
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any

LOG_FILE_NAME = "paperless-notes.log"
_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
_FOLDER_VARIABLES = ("LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "USERPROFILE")


def redaction_rules(env: Mapping[str, str]) -> list[tuple[str, str]]:
    """(folder, placeholder) pairs for the private folders named in ``env``, longest folder first."""
    rules: dict[str, str] = {}
    for name in _FOLDER_VARIABLES:
        value = env.get(name, "").strip().rstrip("\\/")
        if len(value) > 3:
            rules.setdefault(value.casefold(), f"%{name}%")
    for name, value in env.items():
        folder = value.strip().rstrip("\\/")
        if name.casefold().startswith("onedrive") and len(folder) > 3:
            rules.setdefault(folder.casefold(), "%OneDrive%")
    return sorted(rules.items(), key=lambda rule: -len(rule[0]))


def redact(text: str, rules: list[tuple[str, str]], user: str = "") -> str:
    """``text`` with private folders replaced by placeholders and the user name hidden in paths."""
    for folder, placeholder in rules:
        for variant in {folder, folder.replace("\\", "\\\\"), folder.replace("\\", "/")}:
            text = re.sub(re.escape(variant), placeholder.replace("\\", "\\\\"), text, flags=re.IGNORECASE)
    if len(user) >= 2:
        text = re.sub(rf"(?i)(?<=[\\/]){re.escape(user)}(?=[\\/'\"\s]|$)", "<user>", text)
    return text


def _is_path(value: str) -> bool:
    return ntpath.isabs(value) or value.startswith(("\\\\", "//"))


def describe_os_error(exc: OSError) -> str:
    """Error type, codes, the system's reason and at most the file name; never the folder."""
    parts = [type(exc).__name__]
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        parts.append(f"[WinError {winerror}]")
    elif exc.errno is not None:
        parts.append(f"[Errno {exc.errno}]")
    text = " ".join(parts)
    if exc.strerror:
        text += f" {exc.strerror}"
    names = [ntpath.basename(str(f).rstrip("\\/")) for f in (exc.filename, exc.filename2) if f]
    return text + (f" ({', '.join(names)})" if names else "")


def _safe_arg(value: object) -> object:
    if isinstance(value, OSError):
        return describe_os_error(value)
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if isinstance(value, str) and _is_path(value):
        return ntpath.basename(value.rstrip("\\/")) or "(folder)"
    return value


class PrivacyFilter(logging.Filter):
    """Reduces log call arguments that are errors or absolute paths before the record is formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_safe_arg(a) for a in record.args)
        return True


class PrivacyFormatter(logging.Formatter):
    """Formats a record, then hides private folders and the user name; exception file paths shrink to
    their file names."""

    def __init__(self, fmt: str, rules: list[tuple[str, str]], user: str) -> None:
        super().__init__(fmt)
        self._rules = rules
        self._user = user

    def formatException(self, ei: Any) -> str:  # noqa: N802 - logging API
        text = super().formatException(ei)
        exc: BaseException | None = ei[1] if isinstance(ei, tuple) else None
        seen: set[int] = set()
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            if isinstance(exc, OSError):
                for name in (exc.filename, exc.filename2):
                    if isinstance(name, str) and _is_path(name):
                        short = ntpath.basename(name.rstrip("\\/"))
                        text = text.replace(repr(name), repr(short)).replace(name, short)
            exc = exc.__cause__ or exc.__context__
        return text

    def format(self, record: logging.LogRecord) -> str:
        cached = record.exc_text
        record.exc_text = None
        try:
            text = super().format(record)
        finally:
            record.exc_text = cached
        return redact(text, self._rules, self._user)


logger = logging.getLogger("paperless_notes")
_notifier: Callable[[str, str], None] | None = None
_qt_handler: Callable[..., None] | None = None

type Notifier = Callable[[str, str], None]


class RotatingFileHandler(logging.FileHandler):
    """Size-based rotation without importing ``logging.handlers`` (which pulls in ``socket``)."""

    def __init__(self, path: Path, max_bytes: int = 1_000_000, backups: int = 3) -> None:
        super().__init__(path, mode="a", encoding="utf-8", delay=True)
        self._max_bytes = max_bytes
        self._backups = backups

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._needs_rollover(record):
                self._rollover()
        except OSError:
            self.handleError(record)
        super().emit(record)

    def _needs_rollover(self, record: logging.LogRecord) -> bool:
        if self.stream is not None:
            size = self.stream.tell()
        elif os.path.exists(self.baseFilename):
            size = os.path.getsize(self.baseFilename)
        else:
            return False
        return size + len(self.format(record)) + 2 > self._max_bytes

    def _rollover(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        for i in range(self._backups - 1, 0, -1):
            src = f"{self.baseFilename}.{i}"
            if os.path.exists(src):
                os.replace(src, f"{self.baseFilename}.{i + 1}")
        if os.path.exists(self.baseFilename):
            os.replace(self.baseFilename, f"{self.baseFilename}.1")


def configure_logging(
    logs_dir: Path, level: int = logging.INFO, env: Mapping[str, str] | None = None
) -> Path:
    env = os.environ if env is None else env
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / LOG_FILE_NAME
    handler = RotatingFileHandler(log_path)
    handler.setFormatter(PrivacyFormatter(_FORMAT, redaction_rules(env), env.get("USERNAME", "").strip()))
    handler.addFilter(PrivacyFilter())
    root = logging.getLogger()
    for existing in [h for h in root.handlers if isinstance(h, RotatingFileHandler)]:
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)
    root.setLevel(level)
    logging.captureWarnings(True)
    return log_path


def set_error_notifier(notifier: Notifier | None) -> None:
    """Register the UI callback that shows a crash dialog (title, message)."""
    global _notifier
    _notifier = notifier


def notify_error(title: str, message: str) -> None:
    notifier = _notifier
    if notifier is None:
        return
    try:
        notifier(title, message)
    except Exception:
        logger.exception("Error notifier failed")


def _excepthook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    logger.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
    notify_error("Unexpected error", f"{exc_type.__name__}. Details were written to the log.")


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    if args.exc_value is None:
        return
    logger.critical(
        "Unhandled exception in thread %s",
        args.thread.name if args.thread else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def _unraisable_hook(unraisable: Any) -> None:
    logger.error(
        "Unraisable exception in %r",
        unraisable.object,
        exc_info=(unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback),
    )


def logging_configured() -> bool:
    return any(isinstance(h, RotatingFileHandler) for h in logging.getLogger().handlers)


def install_crash_hooks() -> None:
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    sys.unraisablehook = _unraisable_hook


def install_qt_message_handler() -> None:
    from PySide6.QtCore import QMessageLogContext, QtMsgType, qInstallMessageHandler

    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    qt_log = logging.getLogger("qt")

    def handler(mode: QtMsgType, context: QMessageLogContext, message: str) -> None:
        qt_log.log(levels.get(mode, logging.WARNING), "%s", message)

    global _qt_handler
    _qt_handler = handler
    qInstallMessageHandler(handler)


def guarded[**P, R](fn: Callable[P, R]) -> Callable[P, R | None]:
    """Wrap a Qt slot so an exception is logged and reported instead of escaping into Qt."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | None:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            logger.exception("Error in %s", getattr(fn, "__qualname__", repr(fn)))
            notify_error("Unexpected error", f"{type(exc).__name__}. Details were written to the log.")
            return None

    return wrapper
