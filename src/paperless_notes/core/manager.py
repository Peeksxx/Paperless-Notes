"""One session per note, shared by every view of it (split view), with idle housekeeping."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from paperless_notes.core import pathid
from paperless_notes.core.editor import EditorAdapter
from paperless_notes.core.local_state import LocalStateStore
from paperless_notes.core.session import (
    CloseResult,
    FlushReason,
    NoteSession,
    SessionConfig,
    SessionDeps,
    SessionState,
)
from paperless_notes.core.watcher import ChangeMonitor

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    session: NoteSession
    adapter: EditorAdapter
    views: int
    last_used: float


class SessionManager(QObject):
    session_opened = Signal(object)
    session_closed = Signal(str)

    def __init__(
        self,
        deps: SessionDeps,
        config: SessionConfig,
        adapter_factory: Callable[[], EditorAdapter],
        monitor: ChangeMonitor | None = None,
        local_state: LocalStateStore | None = None,
    ) -> None:
        super().__init__()
        self._deps = deps
        self._config = config
        self._factory = adapter_factory
        self._monitor = monitor
        self._local = local_state
        self._entries: dict[str, _Entry] = {}

    def acquire(self, path: str) -> NoteSession:
        """The session for ``path``, created and opened on first use. Pair every call with release."""
        key = pathid.identity(path)
        entry = self._entries.get(key)
        if entry is not None:
            entry.views += 1
            entry.last_used = self._deps.scheduler.now()
            return entry.session
        adapter = self._factory()
        session = NoteSession(path, adapter, self._deps, self._config)
        entry = _Entry(session, adapter, 1, self._deps.scheduler.now())
        self._entries[key] = entry
        session.path_changed.connect(lambda _new, s=session: self._rekey(s))
        session.state_changed.connect(lambda _state, e=entry: self._touch(e))
        if self._local is not None and self._local.is_locked(path):
            session.set_soft_lock(True)
        session.open()
        if self._monitor is not None:
            self._monitor.add(session)
        self.session_opened.emit(session)
        return session

    def adapter_for(self, session: NoteSession) -> EditorAdapter:
        return self._entry(session).adapter

    def release(self, session: NoteSession, force: bool = False) -> CloseResult:
        """Drop one view. The session closes when its last view goes and nothing is left unsaved."""
        entry = self._entry(session)
        if entry.views > 1:
            entry.views -= 1
            return CloseResult.CLOSED
        result = CloseResult.CLOSED
        if force:
            session.close()
        else:
            result = session.request_close()
            if result is not CloseResult.CLOSED:
                return result
        entry.views = 0
        self._forget(session)
        return result

    def get(self, path: str) -> NoteSession | None:
        entry = self._entries.get(pathid.identity(path))
        return entry.session if entry else None

    def sessions(self) -> list[NoteSession]:
        return [e.session for e in self._entries.values()]

    def view_count(self, session: NoteSession) -> int:
        return self._entry(session).views

    def set_soft_lock(self, path: str, locked: bool) -> None:
        if self._local is not None:
            self._local.set_locked(path, locked)
        session = self.get(path)
        if session is not None:
            if locked and session.dirty:
                session.flush_blocking()
            session.set_soft_lock(locked)

    def flush_all(self, reason: FlushReason) -> None:
        for session in self.sessions():
            session.flush(reason)

    def shutdown(self, timeout_s: float = 5.0) -> list[str]:
        """Save everything synchronously. Returns notes whose text is only in the draft journal."""
        sessions = self.sessions()
        unsaved = [s.path for s in sessions if not s.flush_blocking(timeout_s)]
        for session in sessions:
            try:
                session.close()
            except Exception:
                logger.exception("Closing a note failed at shutdown")
            self._forget(session)
        return unsaved

    def drop_idle_undo(self, idle_s: float) -> int:
        """Free the undo history of clean notes that have not been used for ``idle_s`` seconds."""
        now = self._deps.scheduler.now()
        dropped = 0
        for entry in self._entries.values():
            if entry.session.state is SessionState.READY and now - entry.last_used >= idle_s:
                drop = getattr(entry.adapter, "drop_undo_history", None)
                if callable(drop):
                    drop()
                    dropped += 1
        return dropped

    def _touch(self, entry: _Entry) -> None:
        entry.last_used = self._deps.scheduler.now()

    def _entry(self, session: NoteSession) -> _Entry:
        for entry in self._entries.values():
            if entry.session is session:
                return entry
        raise KeyError("session is not managed here")

    def _forget(self, session: NoteSession) -> None:
        for key, entry in list(self._entries.items()):
            if entry.session is session:
                del self._entries[key]
        if self._monitor is not None:
            self._monitor.remove(session)
        self.session_closed.emit(session.path)

    def _rekey(self, session: NoteSession) -> None:
        for key, entry in list(self._entries.items()):
            if entry.session is session:
                del self._entries[key]
                self._entries[pathid.identity(session.path)] = entry
        if self._monitor is not None:
            self._monitor.path_changed(session)
