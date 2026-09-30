"""One open note: load, autosave, crash journal, external-change detection and reconciliation.

All file I/O for a note is serialised: at most one load, check or write is in flight, so writes are
strictly ordered and a check never races our own write. Results arrive on the owning thread through
the injected executor, which keeps this class deterministic under the simulation.
"""

from __future__ import annotations

import logging
import ntpath
import time
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from PySide6.QtCore import QMetaMethod, QObject, Signal

from paperless_notes.core import onedrive, pathid, textformat
from paperless_notes.core.diffing import DiffModel
from paperless_notes.core.drafts import Draft, DraftStore
from paperless_notes.core.editor import EditorAdapter
from paperless_notes.core.fsops import (
    Expect,
    Expected,
    ExternalChangeError,
    FileStamp,
    FileSystem,
    StatInfo,
    TooLargeError,
    atomic_save,
    digest,
    is_transient,
)
from paperless_notes.core.journal import DraftJournal
from paperless_notes.core.ledger import LedgerEntry
from paperless_notes.core.merge import merge3
from paperless_notes.core.oracle import Action, Compare, Diagnosis
from paperless_notes.core.runtime import IOExecutor, Outcome, Scheduler, TimerHandle, run_job
from paperless_notes.core.security.filenames import sync_name_warning
from paperless_notes.core.syncmonitor import ActionResult, Explanation, MonitorBase, SyncServices, SyncStatus

logger = logging.getLogger(__name__)


class SessionState(Enum):
    LOADING = "loading"
    READY = "ready"
    DIRTY = "dirty"
    SAVING = "saving"
    SAVE_FAILED = "save_failed"
    CONFLICT = "conflict"
    MISSING = "missing"
    LOAD_FAILED = "load_failed"
    CLOSED = "closed"


class FlushReason(Enum):
    AUTOSAVE = "autosave"
    MAX_WAIT = "max_wait"
    USER = "user"
    TAB_SWITCH = "tab_switch"
    DEACTIVATE = "deactivate"
    CLOSE = "close"
    SESSION_END = "session_end"
    RETRY = "retry"
    MERGE = "merge"
    RESOLVE = "resolve"


class Resolution(Enum):
    KEEP_MINE = "keep_mine"
    KEEP_THEIRS = "keep_theirs"
    KEEP_BOTH = "keep_both"
    MANUAL = "manual"


class LoadProblem(Enum):
    MISSING = "missing"
    TOO_LARGE = "too_large"
    BINARY = "binary"
    NOT_UTF8 = "not_utf8"
    UNREPRESENTABLE = "unrepresentable"
    HYDRATION_TIMEOUT = "hydration_timeout"
    IO_ERROR = "io_error"


class CloseResult(Enum):
    CLOSED = "closed"
    SAVING = "saving"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ConflictInfo:
    """A local edit and an external change touch the same lines. Nothing has been overwritten."""

    base: str
    ours: str
    theirs: str | None
    theirs_stamp: FileStamp
    detected_at: float


@dataclass(frozen=True, slots=True)
class ReloadInfo:
    kind: str
    path: str
    previous_sha: str | None = None
    sha: str | None = None


class SnapshotSink(Protocol):
    def snapshot(self, path: str, data: bytes, reason: str) -> None: ...


@runtime_checkable
class SnapshotReader(Protocol):
    def read(self, snapshot: object) -> bytes: ...


@dataclass(frozen=True, slots=True)
class SessionConfig:
    debounce_s: float = 1.5
    onedrive_debounce_s: float = 3.0
    max_wait_s: float = 15.0
    draft_interval_s: float = 0.5
    settle_s: float = 0.5
    retry_initial_s: float = 0.25
    retry_max_s: float = 30.0
    retry_quiet_s: float = 10.0
    load_retries: int = 6
    hydrate_timeout_s: float = 120.0
    max_bytes: int = textformat.DEFAULT_MAX_BYTES
    history_min_interval_s: float = 300.0


@dataclass
class SessionDeps:
    fs: FileSystem
    scheduler: Scheduler
    executor: IOExecutor
    drafts: DraftStore | None = None
    history: SnapshotSink | None = None
    onedrive_roots: Sequence[str] = ()
    hostname: str = "PC"
    sync: SyncServices | None = None


@dataclass(frozen=True, slots=True)
class _Missing:
    pass


@dataclass(frozen=True, slots=True)
class _Unchanged:
    stat: StatInfo


@dataclass(frozen=True, slots=True)
class _Observed:
    stat: StatInfo
    data: bytes
    sha: str


type _CheckResult = _Missing | _Unchanged | _Observed | None


@dataclass(frozen=True, slots=True)
class _Loaded:
    stat: StatInfo
    data: bytes
    siblings: list[str]


def _decompress(blob: bytes) -> str:
    return zlib.decompress(blob).decode("utf-8")


def _compress(text: str) -> bytes:
    return zlib.compress(text.encode("utf-8"), 1)


class NoteSession(QObject):
    state_changed = Signal(object)
    dirty_changed = Signal(bool)
    loaded = Signal()
    saved = Signal(object)
    reloaded = Signal(object)
    conflict_detected = Signal(object)
    conflict_copies_found = Signal(list)
    draft_available = Signal(object)
    path_changed = Signal(str)
    problem = Signal(str, str)
    diagnosis_changed = Signal(object)
    sync_status_changed = Signal(object)

    def __init__(
        self,
        path: str,
        adapter: EditorAdapter,
        deps: SessionDeps,
        config: SessionConfig | None = None,
    ) -> None:
        super().__init__()
        self._path = pathid.normalize(path)
        self._adapter = adapter
        self._deps = deps
        self._cfg = config or SessionConfig()
        self._fmt = textformat.NEW_FILE_FORMAT
        self._base_blob = _compress("")
        self._base_plain_sha = digest(b"")
        self._disk: FileStamp | None = None
        self._loaded = False
        self._load_problem: LoadProblem | None = None
        self._closed = False
        self._dirty = False
        self._first_unsaved_at: float | None = None
        self._read_only_attr = False
        self._soft_locked = False
        self._lossy = False
        self._hydrating = False
        self._missing = False
        self._missing_candidate = False
        self._conflict: ConflictInfo | None = None
        self._candidate: tuple[str, float] | None = None
        self._suspect_external = False
        self._resolving = False
        self._conflict_meta: tuple[str, textformat.TextFormat] = ("", textformat.NEW_FILE_FORMAT)
        self._io: str | None = None
        self._op = 0
        self._pending_check: bool | None = None
        self._pending_save: FlushReason | None = None
        self._save_failing_since: float | None = None
        self._retry_delay = self._cfg.retry_initial_s
        self._save_error: str | None = None
        self._last_snapshot_at = -1e18
        self._load_attempts = 0
        self._siblings: list[str] = []
        self._timers: dict[str, TimerHandle] = {}
        self._state = SessionState.LOADING
        self._in_sync_root = onedrive.in_sync_root(self._path, deps.onedrive_roots)
        self._drafts = DraftJournal(
            deps.drafts, deps.executor, self._draft_snapshot, lambda: self._dirty, deps.scheduler.wall_time
        )
        self._sync: MonitorBase = deps.sync.attach(self, deps) if deps.sync is not None else MonitorBase()
        adapter.set_change_listener(self.notify_user_edit)

    @property
    def path(self) -> str:
        return self._path

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def read_only(self) -> bool:
        return self._read_only_attr or self._soft_locked or self._lossy or self._resolving

    @property
    def hydrating(self) -> bool:
        return self._hydrating

    @property
    def conflict(self) -> ConflictInfo | None:
        return self._conflict

    @property
    def load_problem(self) -> LoadProblem | None:
        return self._load_problem

    @property
    def save_error(self) -> str | None:
        return self._save_error

    @property
    def disk_stamp(self) -> FileStamp | None:
        return self._disk

    @property
    def in_sync_root(self) -> bool:
        return self._in_sync_root

    @property
    def conflict_copies(self) -> list[str]:
        return list(self._siblings)

    @property
    def io_busy(self) -> bool:
        return self._io is not None

    @property
    def sync_status(self) -> SyncStatus:
        return self._sync.status

    @property
    def diagnosis(self) -> Diagnosis | None:
        return self._sync.diagnosis

    @property
    def name_warning(self) -> str | None:
        return sync_name_warning(self._path, self._in_sync_root)

    @property
    def text_format(self) -> textformat.TextFormat:
        """Encoding, byte order mark and line endings of the file as loaded."""
        return self._fmt

    def base_text(self) -> str:
        return _decompress(self._base_blob)

    def buffer_text(self) -> str:
        return self._adapter.text()

    def stored_draft(self) -> Draft | None:
        return self._drafts.load()

    def report_conflict_copies(self, names: list[str]) -> None:
        self._set_siblings(names)

    def explain(self, diagnosis: Diagnosis) -> Explanation:
        return self._sync.explain(diagnosis)

    def apply_action(self, diagnosis: Diagnosis, action: Action) -> ActionResult:
        return self._sync.apply(diagnosis, action)

    def diff(self, compare: Compare) -> DiffModel | None:
        return self._sync.diff(compare)

    def lineage(self) -> tuple[LedgerEntry, ...]:
        """This note's ledger entries, oldest first; empty without sync services."""
        return self._sync.lineage()

    def open(self) -> None:
        """Start loading the file. Cloud-only files are downloaded off the UI thread."""
        if self._closed or self._io is not None:
            return
        st = self._deps.fs.stat(self._path)
        if st is None:
            self._fail_load(LoadProblem.MISSING)
            return
        self._hydrating = onedrive.needs_hydration(st.attributes)
        self._in_sync_root = self._in_sync_root or onedrive.is_cloud_managed(st.attributes, st.reparse_tag)
        if self._hydrating:
            self._set_timer("hydrate", self._cfg.hydrate_timeout_s, self._on_hydrate_timeout)
            self._sync.hydrating(True)
        self._start_io("load", self._job_load, self._on_loaded)
        self._update_state()

    def open_read_only_lossy(self) -> None:
        """Show a file that cannot be decoded exactly. It is never written back."""
        if self._load_problem not in (LoadProblem.NOT_UTF8, LoadProblem.UNREPRESENTABLE):
            return
        try:
            data = self._deps.fs.read_bytes(self._path, self._cfg.max_bytes)
        except OSError as exc:
            self._report("read_failed", f"Could not read the file: {exc.strerror or exc}")
            return
        self._lossy = True
        self._load_problem = None
        self._loaded = True
        self._adapter.load(textformat.decode_lossy(data))
        self._adapter.set_read_only(True)
        self._update_state()

    def set_soft_lock(self, locked: bool) -> None:
        self._soft_locked = locked
        self._adapter.set_read_only(self.read_only)

    def notify_user_edit(self) -> None:
        if not self._loaded or self._closed:
            return
        modified = self._adapter.is_user_modified()
        if not modified:
            if self._dirty:
                self._set_dirty(False)
                self._cancel_timer("autosave")
                self._cancel_timer("max_wait")
                self._drafts.queue("delete")
            self._update_state()
            return
        now = self._deps.scheduler.now()
        if not self._dirty:
            self._set_dirty(True)
            self._first_unsaved_at = now
            self._set_timer("max_wait", self._cfg.max_wait_s, self._on_max_wait)
        self._set_timer("autosave", self._debounce(), self._on_autosave)
        if "draft" not in self._timers:
            self._set_timer("draft", self._cfg.draft_interval_s, self._journal)
        self._update_state()

    def flush(self, reason: FlushReason = FlushReason.USER) -> bool:
        """Save now if there is anything to save. Returns False when saving is blocked."""
        if not self._dirty:
            return True
        if not self._can_save():
            return False
        self._cancel_timer("autosave")
        self._cancel_timer("max_wait")
        if self._io is not None or self._holding_saves():
            self._pending_save = reason
            return True
        self._start_save(reason)
        return True

    def notify_fs_event(self) -> None:
        """A watcher reported activity in the note's folder. Checks after the burst settles."""
        if self._loaded and not self._closed:
            self._set_timer("settle", self._cfg.settle_s, lambda: self._request_check(full=True))

    def poll(self) -> None:
        """Cheap periodic check: a stat, and a read only when size or mtime differ."""
        if self._loaded and not self._closed:
            self._request_check(full=False)

    def check_now(self) -> None:
        """Full check, used when the window is activated."""
        if self._loaded and not self._closed:
            self._request_check(full=True)
            self._sync.activated()

    def resolve_conflict(self, choice: Resolution, manual_text: str | None = None) -> str | None:
        """Apply the user's decision. Returns the path of the copy for KEEP_BOTH."""
        conflict = self._conflict
        if conflict is None or self._closed:
            return None
        ours = self._adapter.text()
        if choice is Resolution.KEEP_THEIRS:
            self._sync.resolved(choice.value)
            self._take_theirs(conflict, ours)
            return None
        if choice is Resolution.KEEP_BOTH:
            if self._io is not None:
                self._report("busy", "Please try again in a moment.")
                return None
            sibling = self._sibling_path()
            self._sync.resolved(choice.value, sibling)
            data = self._encode_buffer(ours)
            self._resolving = True
            self._adapter.set_read_only(True)
            self._start_io(
                "sibling",
                lambda: atomic_save(self._deps.fs, sibling, data, Expect.ABSENT, self._cfg.max_bytes),
                lambda out: self._on_sibling_written(out, conflict, ours, sibling),
            )
            return sibling
        if choice is Resolution.MANUAL:
            if manual_text is None:
                raise ValueError("manual resolution needs the merged text")
            self._adapter.apply_external(manual_text)
        self._sync.resolved(choice.value)
        if conflict.theirs is not None:
            self._snapshot_text(conflict.theirs, "before_keep_mine")
        self._conflict = None
        self._adopt_theirs_as_base(conflict)
        self._adapter.set_base(conflict.theirs if conflict.theirs is not None else "\x00")
        self._set_dirty(self._adapter.is_user_modified())
        self._update_state()
        self.flush(FlushReason.RESOLVE)
        return None

    def restore_missing(self) -> None:
        """Write the buffer back to the original path after the file disappeared."""
        if not self._missing or self._io is not None:
            return
        self._write_to(self._path, Expect.ABSENT)

    def save_as(self, new_path: str, overwrite: bool = False) -> None:
        """Write the buffer to ``new_path`` and continue editing that file."""
        if self._io is not None or self._closed:
            return
        self._write_to(pathid.normalize(new_path), Expect.ANY if overwrite else Expect.ABSENT)

    def recover_draft(self, draft: Draft) -> None:
        """Put a journaled draft into the buffer as an ordinary (unsaved) edit."""
        if self._loaded and not self.read_only:
            self._sync.draft_recovered()
            self._adapter.apply_external(draft.text)
            self._adapter.set_base(self.base_text())
            self.notify_user_edit()

    def discard_draft(self) -> None:
        self._drafts.queue("delete")

    def restore_text(self, text: str, reason: str = "restore") -> bool:
        """Put ``text`` in the buffer and save it the normal safe way; the replaced text goes to history."""
        if not self._loaded or self.read_only or self._closed:
            return False
        self._snapshot_text(self._adapter.text(), f"before_{reason}")
        self._sync.restoring(digest(text.encode("utf-8", "surrogatepass")))
        self._adapter.apply_external(text)
        self._adapter.set_base(self.base_text())
        self.notify_user_edit()
        self.flush(FlushReason.USER)
        return True

    def restore_snapshot(self, snapshot: object) -> bool:
        reader = self._deps.history
        if not isinstance(reader, SnapshotReader):
            return False
        return self.restore_text(textformat.decode_lossy(reader.read(snapshot)), "restore_snapshot")

    def request_close(self) -> CloseResult:
        if self._closed:
            return CloseResult.CLOSED
        if self._io == "save" or (self._dirty and self.flush(FlushReason.CLOSE) and self._io is not None):
            return CloseResult.SAVING
        if self._dirty:
            return CloseResult.BLOCKED
        self.close()
        return CloseResult.CLOSED

    def close(self) -> None:
        """Stop all activity. A dirty buffer stays in the draft journal."""
        if self._closed:
            return
        if self._dirty and self._deps.drafts is not None:
            self._drafts.queue("write")
        for name in list(self._timers):
            self._cancel_timer(name)
        self._closed = True
        self._sync.closed()
        self._adapter.set_change_listener(None)
        self._update_state()
        self._release_listeners()

    def _release_listeners(self) -> None:
        """Drop every connection so listeners that reference their owner do not keep the session alive."""
        meta = self.metaObject()
        for index in range(meta.methodOffset(), meta.methodCount()):
            method = meta.method(index)
            if method.methodType() == QMetaMethod.MethodType.Signal and self.isSignalConnected(method):
                getattr(self, bytes(method.name().data()).decode()).disconnect()

    def flush_blocking(self, timeout_s: float = 5.0) -> bool:
        """Synchronous save for shutdown. Returns True when nothing unsaved remains."""
        for _ in range(3):
            if self._io is None:
                break
            self._deps.executor.drain(timeout_s)
        if not self._dirty:
            return True
        if not self._can_save() or self._io is not None:
            self._drafts.write_blocking()
            return False
        job, done = self._prepare_save(FlushReason.SESSION_END)
        if job is None:
            return True
        self._io = "save"
        self._op += 1
        op = self._op
        self._finish_io(op, done, run_job(job))
        if self._dirty:
            self._drafts.write_blocking()
        return not self._dirty

    def _on_autosave(self) -> None:
        self.flush(FlushReason.AUTOSAVE)

    def _on_max_wait(self) -> None:
        self.flush(FlushReason.MAX_WAIT)

    def _holding_saves(self) -> bool:
        return self._suspect_external or self._candidate is not None or self._missing_candidate

    def draft_matches_disk(self, draft: Draft) -> bool:
        """True when the draft was written against the version that is on disk now."""
        return draft.base_sha == self._base_plain_sha

    def _debounce(self) -> float:
        return self._cfg.onedrive_debounce_s if self._in_sync_root else self._cfg.debounce_s

    def _can_save(self) -> bool:
        return (
            self._loaded
            and not self._closed
            and self._conflict is None
            and not self._missing
            and not self.read_only
        )

    def _set_dirty(self, dirty: bool) -> None:
        if dirty != self._dirty:
            self._dirty = dirty
            if not dirty:
                self._first_unsaved_at = None
            self.dirty_changed.emit(dirty)

    def _derive_state(self) -> SessionState:
        if self._closed:
            return SessionState.CLOSED
        if self._load_problem is not None:
            return SessionState.LOAD_FAILED
        if not self._loaded:
            return SessionState.LOADING
        if self._conflict is not None:
            return SessionState.CONFLICT
        if self._missing:
            return SessionState.MISSING
        if (
            self._save_failing_since is not None
            and self._dirty
            and (
                self._deps.scheduler.now() - self._save_failing_since >= self._cfg.retry_quiet_s
                or self._save_error is not None
            )
        ):
            return SessionState.SAVE_FAILED
        if self._io == "save":
            return SessionState.SAVING
        if self._dirty:
            return SessionState.DIRTY
        return SessionState.READY

    def _update_state(self) -> None:
        state = self._derive_state()
        if state is not self._state:
            self._state = state
            logger.debug("%s: %s", ntpath.basename(self._path), state.value)
            self.state_changed.emit(state)

    def _report(self, code: str, message: str) -> None:
        logger.info("%s: %s", ntpath.basename(self._path), code)
        self.problem.emit(code, message)

    def _set_timer(self, name: str, delay: float, fn: Callable[[], None], coarse: bool = False) -> None:
        self._cancel_timer(name)

        def fire() -> None:
            self._timers.pop(name, None)
            if not self._closed:
                fn()

        self._timers[name] = self._deps.scheduler.call_later(delay, fire, coarse)

    def _cancel_timer(self, name: str) -> None:
        handle = self._timers.pop(name, None)
        if handle is not None:
            handle.cancel()

    def _start_io[T](self, kind: str, job: Callable[[], T], done: Callable[[Outcome[T]], None]) -> None:
        self._io = kind
        self._op += 1
        op = self._op
        self._deps.executor.submit(job, lambda out: self._finish_io(op, done, out), kind)

    def _finish_io[T](self, op: int, done: Callable[[Outcome[T]], None], outcome: Outcome[T]) -> None:
        if op != self._op:
            return
        self._io = None
        if self._closed:
            return
        done(outcome)
        self._update_state()
        self._drain_pending()

    def _drain_pending(self) -> None:
        if self._io is not None or self._closed or not self._loaded:
            return
        if self._pending_check is not None:
            full = self._pending_check
            self._pending_check = None
            self._request_check(full)
        elif self._pending_save is not None:
            reason = self._pending_save
            self._pending_save = None
            self.flush(reason)

    def _job_load(self) -> _Loaded:
        fs = self._deps.fs
        data = fs.read_bytes(self._path, self._cfg.max_bytes)
        st = fs.stat(self._path)
        if st is None:
            raise FileNotFoundError(2, "file vanished while loading", self._path)
        folder = ntpath.dirname(self._path)
        return _Loaded(st, data, onedrive.find_conflict_siblings(self._path, fs.list_dir(folder)))

    def _on_hydrate_timeout(self) -> None:
        if not self._loaded and self._io == "load":
            self._op += 1
            self._io = None
            self._hydrating = False
            self._fail_load(LoadProblem.HYDRATION_TIMEOUT)

    def _fail_load(self, problem: LoadProblem) -> None:
        self._load_problem = problem
        self._cancel_timer("hydrate")
        self._update_state()
        self._report(f"load_{problem.value}", _LOAD_MESSAGES[problem])

    def _on_loaded(self, outcome: Outcome[_Loaded]) -> None:
        self._cancel_timer("hydrate")
        if self._hydrating:
            self._sync.hydrating(False)
        self._hydrating = False
        if outcome.error is not None:
            exc = outcome.error
            if is_transient(exc) and self._load_attempts < self._cfg.load_retries:
                self._load_attempts += 1
                self._set_timer("load_retry", self._cfg.retry_initial_s * 2**self._load_attempts, self.open)
                return
            if isinstance(exc, TooLargeError):
                self._fail_load(LoadProblem.TOO_LARGE)
            elif isinstance(exc, FileNotFoundError):
                self._fail_load(LoadProblem.MISSING)
            else:
                logger.warning("Loading %s failed: %s", ntpath.basename(self._path), exc)
                self._fail_load(LoadProblem.IO_ERROR)
            return
        loaded = outcome.unwrap()
        decoded = self._decode(loaded.data)
        if isinstance(decoded, LoadProblem):
            self._fail_load(decoded)
            return
        text, fmt = decoded
        self._fmt = fmt
        self._set_base(text, digest(loaded.data))
        self._disk = FileStamp.of(loaded.stat, digest(loaded.data))
        self._read_only_attr = loaded.stat.read_only
        self._loaded = True
        self._adapter.load(text)
        self._adapter.track_line_endings(fmt.endings if fmt.mixed else None)
        self._adapter.set_read_only(self.read_only)
        self._set_siblings(loaded.siblings)
        self._snapshot_plain(loaded.data, "opened")
        self._update_state()
        self.loaded.emit()
        self._sync.loaded(loaded.stat, self._disk.sha256, text)
        self._drafts.offer(text, self.draft_available.emit)
        if self.name_warning is not None:
            self._report("name_not_synced", self.name_warning)

    def _decode(self, data: bytes) -> tuple[str, textformat.TextFormat] | LoadProblem:
        try:
            return textformat.decode(data, self._cfg.max_bytes)
        except textformat.DecodeError as exc:
            return {
                textformat.DecodeProblem.TOO_LARGE: LoadProblem.TOO_LARGE,
                textformat.DecodeProblem.BINARY: LoadProblem.BINARY,
                textformat.DecodeProblem.NOT_UTF8: LoadProblem.NOT_UTF8,
                textformat.DecodeProblem.UNREPRESENTABLE: LoadProblem.UNREPRESENTABLE,
            }[exc.problem]

    def _set_base(self, text: str, plain_sha: str) -> None:
        self._base_blob = _compress(text)
        self._base_plain_sha = plain_sha

    def _set_siblings(self, siblings: list[str]) -> None:
        if siblings != self._siblings:
            self._siblings = siblings
            if siblings:
                self.conflict_copies_found.emit(list(siblings))

    def _encode_buffer(self, text: str) -> bytes:
        if not self._fmt.mixed:
            return textformat.encode(text, self._fmt)
        return textformat.encode(text, self._fmt, self.base_text(), self._adapter.line_endings())

    def _prepare_save(
        self, reason: FlushReason
    ) -> tuple[Callable[[], FileStamp] | None, Callable[[Outcome[FileStamp]], None]]:
        text = self._adapter.text()
        plain = self._encode_buffer(text)
        plain_sha = digest(plain)
        if plain_sha == self._base_plain_sha and self._disk is not None:
            self._mark_saved(text, plain_sha, self._disk)
            return None, lambda out: None
        expected: Expected = self._disk if self._disk is not None else Expect.ABSENT
        path = self._path
        fs = self._deps.fs
        limit = self._cfg.max_bytes

        def job() -> FileStamp:
            return atomic_save(fs, path, plain, expected, limit)

        return job, lambda out: self._on_saved(out, text, plain_sha, reason)

    def _start_save(self, reason: FlushReason) -> None:
        job, done = self._prepare_save(reason)
        if job is None:
            self._update_state()
            return
        self._start_io("save", job, done)
        self._update_state()

    def _mark_saved(self, text: str, plain_sha: str, stamp: FileStamp) -> None:
        self._disk = stamp
        self._set_base(text, plain_sha)
        self._adapter.set_base(text)
        self._save_failing_since = None
        self._save_error = None
        self._retry_delay = self._cfg.retry_initial_s
        self._cancel_timer("retry")
        if self._adapter.is_user_modified():
            self._first_unsaved_at = self._deps.scheduler.now()
            self._set_timer("autosave", self._debounce(), self._on_autosave)
            self._set_timer("max_wait", self._cfg.max_wait_s, self._on_max_wait)
            self._journal()
        else:
            self._set_dirty(False)
            self._cancel_timer("draft")
            self._drafts.queue("delete")

    def _on_saved(self, outcome: Outcome[FileStamp], text: str, plain_sha: str, reason: FlushReason) -> None:
        exc = outcome.error
        if exc is None:
            stamp = outcome.unwrap()
            self._mark_saved(text, plain_sha, stamp)
            now = self._deps.scheduler.now()
            if now - self._last_snapshot_at >= self._cfg.history_min_interval_s:
                self._snapshot_text(text, "saved")
            self._sync.saved(stamp, text)
            self.saved.emit(stamp)
            return
        if isinstance(exc, ExternalChangeError | FileNotFoundError):
            self._suspect_external = True
            self._pending_check = True
            self._pending_save = reason
            return
        self._save_failed(exc)

    def _save_failed(self, exc: Exception) -> None:
        now = self._deps.scheduler.now()
        if self._save_failing_since is None:
            self._save_failing_since = now
        st = self._deps.fs.stat(self._path)
        if st is not None and st.read_only:
            self._read_only_attr = True
            self._save_error = "The file is read-only. Use Save As to keep your changes."
        elif not is_transient(exc):
            self._save_error = f"Could not save: {getattr(exc, 'strerror', None) or exc}"
        else:
            self._save_error = None
        logger.warning("Saving %s failed: %s", ntpath.basename(self._path), exc)
        self._sync.io_failed(is_transient(exc))
        self._journal()
        self._set_timer("retry", self._retry_delay, self._retry_save)
        self._retry_delay = min(self._retry_delay * 2, self._cfg.retry_max_s)
        self._update_state()
        if self._state is SessionState.SAVE_FAILED and self._save_error:
            self._report("save_failed", self._save_error)

    def _retry_save(self) -> None:
        if self._read_only_attr:
            st = self._deps.fs.stat(self._path)
            if st is not None and not st.read_only:
                self._read_only_attr = False
                self._adapter.set_read_only(self.read_only)
        if self._dirty and self._can_save():
            self.flush(FlushReason.RETRY)
        elif self._dirty:
            self._set_timer("retry", self._retry_delay, self._retry_save, coarse=True)

    def _write_to(self, target: str, expected: Expected) -> None:
        text = self._adapter.text()
        plain = self._encode_buffer(text)
        fs = self._deps.fs
        limit = self._cfg.max_bytes

        def job() -> FileStamp:
            fs.make_dirs(ntpath.dirname(target))
            return atomic_save(fs, target, plain, expected, limit)

        def done(out: Outcome[FileStamp]) -> None:
            if out.error is not None:
                self._report("write_failed", f"Could not write the file: {out.error}")
                return
            if not pathid.same_path(target, self._path):
                old = self._path
                self._drafts.queue("delete")
                self._path = target
                self._in_sync_root = onedrive.in_sync_root(target, self._deps.onedrive_roots)
                logger.info("Session moved from %s to %s", ntpath.basename(old), ntpath.basename(target))
                self.path_changed.emit(target)
            self._missing = False
            self._missing_candidate = False
            self._read_only_attr = False
            self._adapter.set_read_only(self.read_only)
            self._mark_saved(text, digest(plain), out.unwrap())
            self._sync.saved(out.unwrap(), text)
            self.saved.emit(out.unwrap())

        self._start_io("save", job, done)
        self._update_state()

    def _request_check(self, full: bool) -> None:
        if self._closed or not self._loaded or self._lossy:
            return
        if self._io is not None:
            self._pending_check = bool(self._pending_check) or full
            return
        expected = self._disk
        fs = self._deps.fs
        path = self._path
        limit = self._cfg.max_bytes

        def job() -> _CheckResult:
            st = fs.stat(path)
            if st is None:
                return _Missing()
            if not full and expected is not None and expected.same_stat(st):
                return _Unchanged(st)
            data = fs.read_bytes(path, limit)
            after = fs.stat(path)
            if after is None or after.size != len(data) or after.mtime_ns != st.mtime_ns:
                return None
            return _Observed(after, data, digest(data))

        self._start_io("check", job, self._on_checked)

    def _on_checked(self, outcome: Outcome[_CheckResult]) -> None:
        if outcome.error is not None:
            exc = outcome.error
            if not is_transient(exc):
                logger.warning("Checking %s failed: %s", ntpath.basename(self._path), exc)
            self._set_timer("recheck", self._cfg.settle_s * 2, lambda: self._request_check(full=True))
            return
        result = outcome.value
        if result is None:
            self._set_timer("recheck", self._cfg.settle_s, lambda: self._request_check(full=True))
            return
        if isinstance(result, _Missing):
            self._on_missing()
            return
        self._missing_candidate = False
        self._update_read_only(result.stat)
        if isinstance(result, _Unchanged):
            self._sync.observed(result.stat, None)
        else:
            self._sync.observed(result.stat, result.sha, result.data)
        if isinstance(result, _Unchanged):
            return
        if self._disk is not None and result.sha == self._disk.sha256:
            self._disk = FileStamp.of(result.stat, result.sha)
            self._candidate = None
            self._suspect_external = False
            if self._missing:
                self._missing = False
                self._update_state()
                if self._dirty:
                    self.flush(FlushReason.RETRY)
            return
        now = self._deps.scheduler.now()
        candidate = self._candidate
        if candidate is None or candidate[0] != result.sha or now - candidate[1] < self._cfg.settle_s * 0.9:
            if candidate is None or candidate[0] != result.sha:
                self._candidate = (result.sha, now)
            self._set_timer("confirm", self._cfg.settle_s, lambda: self._request_check(full=True))
            return
        self._candidate = None
        self._reconcile(result)

    def _update_read_only(self, st: StatInfo) -> None:
        if st.read_only != self._read_only_attr:
            self._read_only_attr = st.read_only
            self._adapter.set_read_only(self.read_only)
            if not st.read_only and self._dirty:
                self._save_error = None
                self.flush(FlushReason.RETRY)
            self._update_state()

    def _on_missing(self) -> None:
        if self._missing:
            return
        if not self._missing_candidate:
            self._missing_candidate = True
            self._set_timer("confirm", self._cfg.settle_s, lambda: self._request_check(full=True))
            return
        self._missing_candidate = False
        self._suspect_external = False
        self._missing = True
        self._journal()
        self._sync.missing()
        self._update_state()
        self._report("missing", "The file was deleted or renamed. Your text is kept; use Save As or Restore.")

    def _reconcile(self, observed: _Observed) -> None:
        self._suspect_external = False
        stamp = FileStamp.of(observed.stat, observed.sha)
        previous = self._disk.sha256 if self._disk is not None else None
        decoded = self._decode(observed.data)
        theirs: str | None = None
        theirs_fmt = self._fmt
        if not isinstance(decoded, LoadProblem):
            theirs, theirs_fmt = decoded
        self._missing = False
        if theirs is not None and (not self._dirty or self._adapter.text() == theirs):
            self._snapshot_text(self.base_text(), "before_reload")
            self._fmt = theirs_fmt
            self._set_base(theirs, digest(observed.data))
            self._disk = stamp
            kind = "reloaded" if not self._dirty else "converged"
            self._adapter.apply_external(theirs)
            self._adapter.track_line_endings(theirs_fmt.endings if theirs_fmt.mixed else None)
            self._set_dirty(False)
            self._cancel_timer("autosave")
            self._cancel_timer("max_wait")
            self._cancel_timer("draft")
            self._drafts.queue("delete")
            self._pending_save = None
            self._conflict = None
            self._update_state()
            self._sync.reloaded(kind, stamp, theirs)
            self.reloaded.emit(ReloadInfo(kind, self._path, previous, stamp.sha256))
            return
        ours = self._adapter.text()
        base = self.base_text()
        merged = merge3(base, ours, theirs) if theirs is not None else None
        if merged is not None and merged.clean and merged.text is not None and theirs is not None:
            self._snapshot_text(ours, "before_merge")
            self._conflict = None
            self._fmt = theirs_fmt
            self._set_base(theirs, digest(observed.data))
            self._disk = stamp
            self._adapter.apply_external(merged.text)
            self._adapter.set_base(theirs)
            self._set_dirty(self._adapter.is_user_modified())
            self._update_state()
            self._sync.reloaded("merged", stamp, theirs)
            self.reloaded.emit(ReloadInfo("merged", self._path, previous, stamp.sha256))
            if self._dirty:
                self._pending_save = None
                self.flush(FlushReason.MERGE)
            return
        self._cancel_timer("autosave")
        self._cancel_timer("max_wait")
        self._pending_save = None
        self._conflict = ConflictInfo(base, ours, theirs, stamp, self._deps.scheduler.wall_time())
        self._conflict_meta = (digest(observed.data) if theirs is not None else "", theirs_fmt)
        self._journal()
        self._sync.conflict(stamp)
        self._update_state()
        self.conflict_detected.emit(self._conflict)

    def _adopt_theirs_as_base(self, conflict: ConflictInfo) -> None:
        plain_sha, fmt = self._conflict_meta
        self._disk = conflict.theirs_stamp
        if conflict.theirs is not None:
            self._fmt = fmt
            self._set_base(conflict.theirs, plain_sha)
        else:
            self._base_plain_sha = ""

    def _take_theirs(self, conflict: ConflictInfo, ours: str) -> None:
        if conflict.theirs is None:
            self._report("unreadable", "The other version cannot be read as text. Keep yours or save a copy.")
            return
        self._snapshot_text(ours, "before_keep_theirs")
        self._conflict = None
        previous = self._disk.sha256 if self._disk is not None else None
        self._adopt_theirs_as_base(conflict)
        self._adapter.apply_external(conflict.theirs)
        self._adapter.track_line_endings(self._fmt.endings if self._fmt.mixed else None)
        self._set_dirty(False)
        self._drafts.queue("delete")
        self._update_state()
        self._sync.reloaded("kept_theirs", conflict.theirs_stamp, conflict.theirs)
        self.reloaded.emit(ReloadInfo("kept_theirs", self._path, previous, conflict.theirs_stamp.sha256))

    def _on_sibling_written(
        self, out: Outcome[FileStamp], conflict: ConflictInfo, ours: str, sibling: str
    ) -> None:
        self._resolving = False
        self._adapter.set_read_only(self.read_only)
        if out.error is not None:
            self._report("copy_failed", f"Could not write the copy: {out.error}")
            return
        logger.info("Conflict copy written: %s", ntpath.basename(sibling))
        self._take_theirs(conflict, ours)

    def _sibling_path(self) -> str:
        folder, name = ntpath.split(self._path)
        stem, ext = ntpath.splitext(name)
        stamp = time.strftime("%Y-%m-%d %H%M", time.localtime(self._deps.scheduler.wall_time()))
        host = "".join(ch for ch in self._deps.hostname if ch.isalnum() or ch in "-_") or "PC"
        candidate = ntpath.join(folder, f"{stem} (conflict {host} {stamp}){ext}")
        n = 2
        while self._deps.fs.stat(candidate) is not None:
            candidate = ntpath.join(folder, f"{stem} (conflict {host} {stamp} {n}){ext}")
            n += 1
        return candidate

    def _snapshot_text(self, text: str, reason: str) -> None:
        self._snapshot_plain(
            textformat.encode(text, self._fmt, self.base_text() if self._fmt.mixed else None), reason
        )

    def _snapshot_plain(self, data: bytes, reason: str) -> None:
        sink = self._deps.history
        if sink is None:
            return
        path = self._path
        self._last_snapshot_at = self._deps.scheduler.now()

        def done(out: Outcome[None]) -> None:
            if out.error is not None:
                logger.warning("History snapshot failed: %s", out.error)

        self._deps.executor.submit(lambda: sink.snapshot(path, data, reason), done, "history")

    def _journal(self) -> None:
        self._cancel_timer("draft")
        if self._dirty:
            self._drafts.queue("write")

    def _draft_snapshot(self) -> tuple[str, str, str]:
        return self._path, self._adapter.text(), self._base_plain_sha


_LOAD_MESSAGES = {
    LoadProblem.MISSING: "The file does not exist.",
    LoadProblem.TOO_LARGE: "The file is larger than the size limit.",
    LoadProblem.BINARY: "This is not a text file.",
    LoadProblem.NOT_UTF8: "The file is not UTF-8 text. It can be opened read-only.",
    LoadProblem.UNREPRESENTABLE: (
        "The file contains characters the editor cannot keep. It can be opened read-only."
    ),
    LoadProblem.HYDRATION_TIMEOUT: "Downloading the file from OneDrive took too long.",
    LoadProblem.IO_ERROR: "The file could not be read.",
}
