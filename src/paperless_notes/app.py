"""Application entry point.

Startup is guarded: anything that fails before the window is shown is logged (when logging is already
set up) and explained in a message box, so the windowed build never fails silently.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication

    from paperless_notes.core.instance import InstanceLock

logger = logging.getLogger("paperless_notes")


def show_startup_error(message: str) -> None:
    """A message box that works with or without a running Qt application."""
    from paperless_notes.branding import PRODUCT_NAME

    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is not None:
            QMessageBox.critical(None, PRODUCT_NAME, message)
            return
    except ImportError as exc:
        logger.error("Qt is unavailable for the startup error: %s", type(exc).__name__)
    import ctypes

    mb_iconerror_setforeground = 0x10 | 0x10000
    ctypes.windll.user32.MessageBoxW(None, message, PRODUCT_NAME, mb_iconerror_setforeground)


def startup_failure_message(exc: BaseException, logged: bool) -> str:
    from paperless_notes.core.paths import DATA_DIR_ENV, UnsafeDataDirError

    if isinstance(exc, UnsafeDataDirError):
        return (
            "Paperless Notes could not start because its data folder would be inside a synced or roaming "
            f"folder. Remove the {DATA_DIR_ENV} setting or point it at a local folder, then start again."
        )
    where = (
        "Details were written to the log in %LOCALAPPDATA%\\Paperless Notes\\logs."
        if logged
        else "No log could be written."
    )
    return f"Paperless Notes could not start ({type(exc).__name__}). {where}"


def main(argv: list[str] | None = None) -> int:
    from paperless_notes.core import logsetup

    logsetup.install_crash_hooks()
    args = list(sys.argv if argv is None else argv)
    try:
        started = _start(args)
    except Exception as exc:
        logged = logsetup.logging_configured()
        if logged:
            logger.critical("Startup failed", exc_info=exc)
        show_startup_error(startup_failure_message(exc, logged))
        return 1
    if isinstance(started, int):
        return started
    app, lock = started
    try:
        return app.exec()
    finally:
        lock.release()
        logger.info("Stopped")


def _start(args: list[str]) -> tuple[QApplication, InstanceLock] | int:
    from paperless_notes.branding import PRODUCT_NAME, VERSION
    from paperless_notes.core import logsetup
    from paperless_notes.core.paths import resolve_paths
    from paperless_notes.core.security.launch import (
        harden_environment,
        note_paths,
        qt_arguments,
        refused_message,
    )

    frozen = bool(getattr(sys, "frozen", False))
    removed = harden_environment(os.environ, frozen)
    paths = resolve_paths()
    paths.ensure()
    logsetup.configure_logging(paths.logs_dir)
    logger.info("Starting %s %s", PRODUCT_NAME, VERSION)
    if removed:
        logger.info("Ignored %d inherited Qt setting(s)", len(removed))

    from PySide6.QtGui import QPixmapCache
    from PySide6.QtWidgets import QApplication

    QApplication.setApplicationName(PRODUCT_NAME)
    QApplication.setApplicationDisplayName(PRODUCT_NAME)
    QApplication.setApplicationVersion(VERSION)
    app = QApplication(qt_arguments(args))
    logsetup.install_qt_message_handler()
    QPixmapCache.setCacheLimit(4 * 1024)

    from paperless_notes.core.instance import InstanceLock, send_open_request

    lock = InstanceLock(paths.lock_file)
    launch = note_paths(args[1:])
    files = list(launch.accepted)
    if launch.refused:
        logger.info("Refused %d launch argument(s)", launch.refused)
    if not lock.acquire():
        send_open_request(paths.ipc_dir, files)
        logger.info("Another instance is running; forwarded %d path(s)", len(files))
        return 0

    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    from paperless_notes.core.drafts import DraftStore
    from paperless_notes.core.evidence import EvidenceStore
    from paperless_notes.core.files import FileService
    from paperless_notes.core.fsops import WindowsFileSystem
    from paperless_notes.core.history import HistoryStore, PruneReport, RetentionPolicy
    from paperless_notes.core.ledger import LedgerIdentity, LedgerStore
    from paperless_notes.core.local_state import LocalStateStore
    from paperless_notes.core.onedrive import roots_from_env
    from paperless_notes.core.oracle import load_model
    from paperless_notes.core.runtime import Outcome, QtIOExecutor, QtScheduler
    from paperless_notes.core.security.startup import StartupRegistration, WinRegRunKey
    from paperless_notes.core.session import SessionDeps
    from paperless_notes.core.settings import (
        LegacyRegistryReader,
        SettingsStore,
        load_settings,
        session_config,
        with_default_library,
    )
    from paperless_notes.core.syncmonitor import SyncServices
    from paperless_notes.core.watcher import ChangeMonitor
    from paperless_notes.ui.error_dialog import show_error_dialog
    from paperless_notes.ui.shell.window import MainWindow, ShellServices, install_style
    from paperless_notes.ui.theme.manager import ThemeManager

    settings_store = SettingsStore(paths.settings_file)
    settings = load_settings(paths.settings_file, LegacyRegistryReader())
    defaulted_settings = with_default_library(settings, paths.notes_dir)
    if defaulted_settings != settings:
        settings = defaulted_settings
        try:
            settings_store.save(settings)
        except OSError as exc:
            logger.warning("Saving the default notes folder failed: %s", exc)
    logsetup.set_error_notifier(lambda title, msg: show_error_dialog(title, msg, paths.logs_dir))
    StartupRegistration(WinRegRunKey(), sys.executable, bool(getattr(sys, "frozen", False))).sync(
        settings.run_on_startup
    )
    fs = WindowsFileSystem(roots_from_env(os.environ))
    executor = QtIOExecutor()
    history = HistoryStore(
        paths.history_dir,
        RetentionPolicy(
            settings.history_keep_all_hours,
            settings.history_hourly_days,
            settings.history_daily_days,
            settings.history_max_mb,
        ),
    )
    hostname = os.environ.get("COMPUTERNAME", "PC")
    history_reader = history if settings.history_enabled else None
    identity = LedgerIdentity(LocalStateStore(paths.state_file).install_id, hostname, uuid.uuid4().hex)
    ledgers = LedgerStore(fs, str(paths.ledger_dir), identity, time.time, history_reader)
    ledgers.enforce_caps()
    evidence = EvidenceStore(fs, str(paths.evidence_dir))
    evidence.enforce_caps()
    deps = SessionDeps(
        fs=fs,
        scheduler=QtScheduler(),
        executor=executor,
        drafts=DraftStore(fs, str(paths.drafts_dir)),
        history=history_reader,
        onedrive_roots=roots_from_env(os.environ),
        hostname=hostname,
        sync=SyncServices(
            ledgers, load_model(paths.calibration_file), history=history_reader, evidence=evidence
        ),
    )

    def pruned(outcome: Outcome[PruneReport]) -> None:
        if outcome.error is not None:
            logger.warning("History prune failed: %s", outcome.error)

    executor.submit(history.prune, pruned)
    monitor = ChangeMonitor(settings.poll_focused_s, settings.poll_unfocused_s, settings.poll_minimized_s)
    install_style(app)
    themes = ThemeManager(app, settings.theme, settings.accent, settings.reduced_motion)
    themes.apply()
    services = ShellServices(
        settings,
        settings_store,
        themes,
        FileService(fs, roots_from_env(os.environ)),
        history,
        evidence,
        opener=lambda url: QDesktopServices.openUrl(QUrl(url)),
    )
    window = MainWindow(paths, deps, session_config(settings), monitor, services=services)
    app.commitDataRequest.connect(lambda _manager: window.shutdown())
    window.restore_workspace()
    for path in files:
        window.open_path(path)
    window.show()
    if launch.refused:
        window.toast_message(refused_message(launch.refused))
    return app, lock
