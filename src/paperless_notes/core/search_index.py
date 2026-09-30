"""Local full-text index of library notes with their tags (SQLite FTS5).

The database lives in the per-machine state folder, never beside notes. ``IndexStore`` is the synchronous
database layer; ``SearchIndex`` runs every database and file job on its own one-thread executor, one job at
a time, with searches ahead of time-boxed indexing batches so search stays responsive while notes are
indexed. Only metadata of the rows being worked on is held in memory; note bodies stay on disk.

Nothing here logs note text, queries, results or snippets: failures are logged by exception type only.
"""

from __future__ import annotations

import logging
import ntpath
import os
import re
import sqlite3
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from paperless_notes.core import pathid, textformat
from paperless_notes.core.fsops import FileSystem, digest
from paperless_notes.core.library import NOTE_EXTENSIONS, walk_notes
from paperless_notes.core.onedrive import needs_hydration
from paperless_notes.core.runtime import IOExecutor, Outcome
from paperless_notes.core.search_query import CompiledQuery, compile_query
from paperless_notes.core.tagnames import Tag

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
OVERVIEW_RECENT = 12
OVERVIEW_TAGS = 16
DB_NAME = "search.sqlite3"
_OPEN_MARK = "\x02"
_CLOSE_MARK = "\x03"
_ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"
_HEADING_MARK = re.compile(r"(^|\n)[ \t]{0,3}#{1,6}(?=[ \t\n]|$)")
_RAW_SNIPPET_CHARS = 4_000

type TagExtractor = Callable[[str], list[Tag]]
type Walker = Callable[[str], Iterator[tuple[str, int, int]]]


@dataclass(frozen=True, slots=True)
class IndexLimits:
    max_note_bytes: int = 2 * 1024 * 1024
    max_results: int = 100
    snippet_chars: int = 180
    max_tags: int = 5_000
    max_skipped: int = 500
    batch_seconds: float = 0.05
    batch_notes: int = 32


class NoteStatus(Enum):
    INDEXED = "indexed"
    TOO_LARGE = "too_large"
    NOT_TEXT = "not_text"
    ONLINE_ONLY = "online_only"
    UNREADABLE = "unreadable"


SKIP_REASONS = {
    NoteStatus.TOO_LARGE: "Too large to search",
    NoteStatus.NOT_TEXT: "Not a text file",
    NoteStatus.ONLINE_ONLY: "Only in OneDrive, not downloaded to this PC",
    NoteStatus.UNREADABLE: "Could not be read",
}


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One result. ``snippet`` is plain text; ``marks`` are (start, end) indexes of matched words in it."""

    path: str
    title: str
    snippet: str
    marks: tuple[tuple[int, int], ...]
    score: float
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class SearchResult:
    hits: tuple[SearchHit, ...] = ()
    more: bool = False
    problem: str = ""


@dataclass(frozen=True, slots=True)
class TagCount:
    key: str
    display: str
    count: int


@dataclass(frozen=True, slots=True)
class NoteInfo:
    path: str
    title: str
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class Overview:
    """What the home screen shows: counts, the notes changed most recently (by anyone, on any PC), details
    of notes the caller asked about, and the most used tags."""

    indexed: int = 0
    skipped: int = 0
    changed_since: int = 0
    recent: tuple[NoteInfo, ...] = ()
    known: tuple[NoteInfo, ...] = ()
    tags: tuple[TagCount, ...] = ()


@dataclass(frozen=True, slots=True)
class SkippedNote:
    path: str
    status: NoteStatus

    @property
    def reason(self) -> str:
        return SKIP_REASONS.get(self.status, "Not searched")


@dataclass(frozen=True, slots=True)
class _Known:
    id: int
    size: int
    mtime_ns: int
    sha: str
    status: NoteStatus


def clean_snippet(raw: str, limit: int) -> tuple[str, tuple[tuple[int, int], ...]]:
    """Plain text of at most ``limit`` characters and the match ranges in it, from an FTS5 snippet marked
    with STX and ETX. Heading marks are dropped, whitespace is collapsed and the window moves so the first
    match stays visible."""
    out: list[str] = []
    marks: list[tuple[int, int]] = []
    opened: int | None = None
    for ch in _HEADING_MARK.sub(r"\1", raw):
        if ch == _OPEN_MARK:
            opened = len(out)
            continue
        if ch == _CLOSE_MARK:
            if opened is not None and len(out) > opened:
                marks.append((opened, len(out)))
            opened = None
            continue
        if ch.isspace():
            if not out or out[-1] == " ":
                continue
            ch = " "
        out.append(ch)
    if opened is not None and len(out) > opened:
        marks.append((opened, len(out)))
    text = "".join(out)
    start = 0
    if marks and marks[0][1] > limit:
        start = max(0, marks[0][0] - limit // 3)
        space = text.find(" ", start, marks[0][0])
        start = space + 1 if space >= 0 else start
    prefix = _ELLIPSIS if start > 0 and not text[start:].startswith(_ELLIPSIS) else ""
    body = (prefix + text[start : start + limit - len(prefix)]).rstrip()
    shift = len(prefix) - start
    kept = tuple(
        (a + shift, min(b + shift, len(body))) for a, b in marks if a >= start and a + shift < len(body)
    )
    return body, kept


def title_of(path: str) -> str:
    return ntpath.splitext(ntpath.basename(path))[0]


def is_note_path(path: str) -> bool:
    return path.casefold().endswith(NOTE_EXTENSIONS)


_SCHEMA = (
    "CREATE TABLE notes(id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, path TEXT NOT NULL, "
    "title TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, sha TEXT NOT NULL, "
    "status TEXT NOT NULL)",
    "CREATE VIRTUAL TABLE body USING fts5(title, text, tokenize='unicode61 remove_diacritics 2')",
    "CREATE TABLE tags(note INTEGER NOT NULL, key TEXT NOT NULL, display TEXT NOT NULL, "
    "PRIMARY KEY(note, key)) WITHOUT ROWID",
    "CREATE INDEX tags_by_key ON tags(key)",
)


class _RebuildNeededError(Exception):
    pass


class IndexStore:
    """The database: one row per note (identity, path, title, size, mtime, hash, status), the searchable
    title and text in an FTS5 table, and the note's tags. Use from one thread at a time."""

    def __init__(self, connection: sqlite3.Connection, path: Path | None, rebuilt: bool) -> None:
        self._db = connection
        self.path = path
        self.rebuilt = rebuilt

    @classmethod
    def open(cls, path: Path | None, force_rebuild: bool = False) -> IndexStore:
        """Open the index, rebuilding it when it is missing, damaged or from another schema version."""
        if path is None:
            memory = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
            cls._create(memory)
            return cls(memory, None, False)
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        if existed and not force_rebuild:
            current: sqlite3.Connection | None = None
            try:
                current = cls._connect(path)
                cls._validate(current)
                return cls(current, path, False)
            except (sqlite3.DatabaseError, _RebuildNeededError) as exc:
                logger.warning("Rebuilding the search index: %s", type(exc).__name__)
                if current is not None:
                    current.close()
        cls._remove_files(path)
        db = cls._connect(path)
        cls._create(db)
        return cls(db, path, existed)

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        try:
            db.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            db.close()
            raise
        return db

    @staticmethod
    def _validate(db: sqlite3.Connection) -> None:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise _RebuildNeededError("schema version")
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"notes", "body", "tags"} <= names:
            raise _RebuildNeededError("tables")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise _RebuildNeededError("integrity")
        db.execute("SELECT count(*) FROM body WHERE body MATCH ?", ('"integrity"',)).fetchone()

    @staticmethod
    def _create(db: sqlite3.Connection) -> None:
        for statement in _SCHEMA:
            db.execute(statement)
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _remove_files(path: Path) -> None:
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)

    def close(self) -> None:
        self._db.close()

    def lookup(self, key: str) -> _Known | None:
        row = self._db.execute(
            "SELECT id, size, mtime_ns, sha, status FROM notes WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return _Known(row[0], row[1], row[2], row[3], NoteStatus(row[4]))

    def put(
        self,
        path: str,
        size: int,
        mtime_ns: int,
        sha: str,
        status: NoteStatus,
        text: str,
        tags: Sequence[Tag] = (),
    ) -> None:
        """Insert or replace one note, its searchable text and its tags in one transaction."""
        key = pathid.identity(path)
        title = title_of(path)
        clean = text.replace(_OPEN_MARK, " ").replace(_CLOSE_MARK, " ")
        db = self._db
        db.execute("BEGIN")
        try:
            row = db.execute("SELECT id FROM notes WHERE key = ?", (key,)).fetchone()
            if row is None:
                cursor = db.execute(
                    "INSERT INTO notes(key, path, title, size, mtime_ns, sha, status) VALUES (?,?,?,?,?,?,?)",
                    (key, path, title, size, mtime_ns, sha, status.value),
                )
                note_id = cursor.lastrowid
            else:
                note_id = row[0]
                db.execute(
                    "UPDATE notes SET path=?, title=?, size=?, mtime_ns=?, sha=?, status=? WHERE id=?",
                    (path, title, size, mtime_ns, sha, status.value, note_id),
                )
                db.execute("DELETE FROM body WHERE rowid = ?", (note_id,))
                db.execute("DELETE FROM tags WHERE note = ?", (note_id,))
            db.execute("INSERT INTO body(rowid, title, text) VALUES (?,?,?)", (note_id, title, clean))
            db.executemany(
                "INSERT OR IGNORE INTO tags(note, key, display) VALUES (?,?,?)",
                [(note_id, t.key, t.display) for t in tags],
            )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def touch(self, key: str, path: str, size: int, mtime_ns: int) -> None:
        """Same content with a new time stamp (or a new spelling of the same path)."""
        self._db.execute(
            "UPDATE notes SET path=?, size=?, mtime_ns=? WHERE key=?", (path, size, mtime_ns, key)
        )

    def remove(self, keys: Sequence[str]) -> int:
        removed = 0
        db = self._db
        db.execute("BEGIN")
        try:
            for key in keys:
                row = db.execute("SELECT id FROM notes WHERE key = ?", (key,)).fetchone()
                if row is None:
                    continue
                db.execute("DELETE FROM body WHERE rowid = ?", (row[0],))
                db.execute("DELETE FROM tags WHERE note = ?", (row[0],))
                db.execute("DELETE FROM notes WHERE id = ?", (row[0],))
                removed += 1
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return removed

    def all_keys(self) -> Iterator[str]:
        """Every note identity, streamed."""
        for row in self._db.execute("SELECT key FROM notes"):
            yield row[0]

    def keys_under(self, folder: str) -> list[str]:
        prefix = pathid.identity(folder).rstrip("\\") + "\\"
        rows = self._db.execute(
            "SELECT key FROM notes WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)
        ).fetchall()
        return [row[0] for row in rows]

    def search(self, query: CompiledQuery, limits: IndexLimits) -> SearchResult:
        """Best matches first (title words weigh more). A tag-only query lists the notes with those tags."""
        if not query.match:
            return self._search_tags(query.tags, limits)
        sql = (
            "SELECT n.path, n.title, n.size, n.mtime_ns, bm25(body, 4.0, 1.0), "
            "substr(snippet(body, 1, char(2), char(3), ?, 24), 1, ?) "
            "FROM body JOIN notes n ON n.id = body.rowid WHERE body MATCH ?"
        )
        params: list[object] = [_ELLIPSIS, _RAW_SNIPPET_CHARS, query.match]
        for tag in query.tags:
            sql += " AND n.id IN (SELECT note FROM tags WHERE key = ?)"
            params.append(tag)
        sql += " ORDER BY 5, n.title LIMIT ?"
        params.append(limits.max_results + 1)
        rows = self._db.execute(sql, params).fetchall()
        hits: list[SearchHit] = []
        for path, title, size, mtime_ns, rank, raw in rows[: limits.max_results]:
            snippet, marks = clean_snippet(raw or "", limits.snippet_chars)
            hits.append(SearchHit(path, title, snippet, marks, -float(rank), size, mtime_ns))
        return SearchResult(tuple(hits), len(rows) > limits.max_results)

    def _search_tags(self, tags: tuple[str, ...], limits: IndexLimits) -> SearchResult:
        """Notes carrying every tag, by title, each with the text around its first use of the tag."""
        sql = (
            "SELECT n.path, n.title, n.size, n.mtime_ns, t.display, "
            "max(1, instr(b.text, '#' || t.display) - 60), "
            "substr(b.text, max(1, instr(b.text, '#' || t.display) - 60), 400) "
            "FROM tags t JOIN notes n ON n.id = t.note JOIN body b ON b.rowid = n.id WHERE t.key = ?"
        )
        params: list[object] = [tags[0]]
        for tag in tags[1:]:
            sql += " AND n.id IN (SELECT note FROM tags WHERE key = ?)"
            params.append(tag)
        sql += " ORDER BY n.title, n.path LIMIT ?"
        params.append(limits.max_results + 1)
        rows = self._db.execute(sql, params).fetchall()
        hits: list[SearchHit] = []
        for path, title, size, mtime_ns, display, start, raw in rows[: limits.max_results]:
            text = raw or ""
            needle = "#" + display
            at = text.find(needle)
            if at >= 0:
                text = text[:at] + _OPEN_MARK + needle + _CLOSE_MARK + text[at + len(needle) :]
            if start > 1:
                text = _ELLIPSIS + text
            snippet, marks = clean_snippet(text, limits.snippet_chars)
            hits.append(SearchHit(path, title, snippet, marks, 0.0, size, mtime_ns))
        return SearchResult(tuple(hits), len(rows) > limits.max_results)

    def tag_counts(self, limit: int) -> list[TagCount]:
        rows = self._db.execute(
            "SELECT key, MIN(display), COUNT(*) FROM tags GROUP BY key ORDER BY key LIMIT ?", (limit,)
        ).fetchall()
        return [TagCount(key, display, count) for key, display, count in rows]

    def overview(self, paths: Sequence[str], since_ns: int, recent: int, tags: int) -> Overview:
        indexed, skipped = self.counts()
        db = self._db
        rows = db.execute(
            "SELECT path, title, size, mtime_ns FROM notes ORDER BY mtime_ns DESC, title LIMIT ?", (recent,)
        ).fetchall()
        changed = db.execute("SELECT COUNT(*) FROM notes WHERE mtime_ns >= ?", (since_ns,)).fetchone()[0]
        known: list[NoteInfo] = []
        for path in paths:
            row = db.execute(
                "SELECT path, title, size, mtime_ns FROM notes WHERE key = ?", (pathid.identity(path),)
            ).fetchone()
            if row is not None:
                known.append(NoteInfo(*row))
        top = db.execute(
            "SELECT key, MIN(display), COUNT(*) AS uses FROM tags "
            "GROUP BY key ORDER BY uses DESC, key LIMIT ?",
            (tags,),
        ).fetchall()
        return Overview(
            indexed,
            skipped,
            changed,
            tuple(NoteInfo(*row) for row in rows),
            tuple(known),
            tuple(TagCount(key, display, count) for key, display, count in top),
        )

    def counts(self) -> tuple[int, int]:
        """(indexed, skipped) note counts."""
        indexed = skipped = 0
        for status, count in self._db.execute("SELECT status, COUNT(*) FROM notes GROUP BY status"):
            if status == NoteStatus.INDEXED.value:
                indexed += count
            else:
                skipped += count
        return indexed, skipped

    def skipped(self, limit: int) -> list[SkippedNote]:
        rows = self._db.execute(
            "SELECT path, status FROM notes WHERE status != ? ORDER BY path LIMIT ?",
            (NoteStatus.INDEXED.value, limit),
        ).fetchall()
        return [SkippedNote(path, NoteStatus(status)) for path, status in rows]


class IndexState(Enum):
    STARTING = "starting"
    INDEXING = "indexing"
    READY = "ready"
    ERROR = "error"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class IndexStatus:
    state: IndexState
    indexed: int = 0
    skipped: int = 0
    pending: int = 0
    rebuilt: bool = False
    problem: str = ""


@dataclass
class _Scan:
    """A walk of some folders; rows under ``scope`` that the walk did not see are removed at the end."""

    scope: list[str]
    walker: Iterator[tuple[str, int, int]]
    seen: set[str] = field(default_factory=set)
    done: bool = False


@dataclass(frozen=True, slots=True)
class _BatchResult:
    changed: tuple[str, ...] = ()
    processed: tuple[str, ...] = ()
    scan_done: bool = False


def _is_dir(path: str) -> bool:
    return os.path.isdir(pathid.to_os_path(path))


def _each[T](callbacks: list[Callable[[T], None]], value: T) -> None:
    for callback in callbacks:
        callback(value)


class SearchIndex(QObject):
    """Keeps the index in step with the library and answers searches, all off the UI thread.

    Call ``start(roots)`` once, then ``set_roots`` when folders are added or removed, ``refresh(paths)``
    after a note or folder was saved, created, renamed, moved or deleted, and ``rescan()`` to catch changes
    made by other programs. ``close()`` waits for the running job and closes the database.
    """

    status_changed = Signal(object)
    content_changed = Signal()

    def __init__(
        self,
        db_path: Path | None,
        executor: IOExecutor,
        fs: FileSystem,
        tags: TagExtractor,
        limits: IndexLimits | None = None,
        walker: Walker = walk_notes,
        is_dir: Callable[[str], bool] = _is_dir,
    ) -> None:
        super().__init__()
        self._db_path = db_path
        self._executor = executor
        self._fs = fs
        self._extract = tags
        self._limits = limits or IndexLimits()
        self._walker = walker
        self._is_dir = is_dir
        self._store: IndexStore | None = None
        self._roots: tuple[str, ...] = ()
        self._opening = False
        self._force_rebuild = False
        self._rebuilds = 0
        self._prune = False
        self._scans: deque[_Scan] = deque()
        self._checks: dict[str, str] = {}
        self._failed: set[str] = set()
        self._search: tuple[CompiledQuery, int, Callable[[SearchResult], None]] | None = None
        self._search_ticket = 0
        self._tag_requests: list[Callable[[list[TagCount]], None]] = []
        self._skipped_requests: list[Callable[[list[SkippedNote]], None]] = []
        self._overview: tuple[tuple[str, ...], int, Callable[[Overview], None]] | None = None
        self._busy = False
        self._pumping = False
        self._again = False
        self._closed = False
        self._started = False
        self._dirty = False
        self._status = IndexStatus(IndexState.STARTING)

    @property
    def status(self) -> IndexStatus:
        return self._status

    @property
    def roots(self) -> list[str]:
        return list(self._roots)

    def start(self, roots: Sequence[str]) -> None:
        if self._started or self._closed:
            return
        self._started = True
        self._roots = self._normalized(roots)
        self._opening = True
        self._full_scan()
        self._pump()

    def set_roots(self, roots: Sequence[str]) -> None:
        """Index added folders and forget notes that are no longer under any folder."""
        fresh = self._normalized(roots)
        added = [r for r in fresh if not any(pathid.same_path(r, old) for old in self._roots)]
        self._roots = fresh
        self._prune = True
        for root in added:
            self._scans.append(_Scan([root], self._walk([root])))
        self._pump()

    def refresh(self, paths: Sequence[str]) -> None:
        """Re-check notes or folders that changed: index new or edited notes, forget missing ones."""
        for path in paths:
            normalized = pathid.normalize(path)
            if not self._within_roots(normalized):
                continue
            key = pathid.identity(normalized)
            self._failed.discard(key)
            if is_note_path(normalized) and not self._is_dir(normalized):
                self._checks[key] = normalized
            else:
                self._scans.append(_Scan([normalized], self._walk([normalized])))
        self._pump()

    def rescan(self) -> None:
        """Walk every folder again (cheap: only notes whose size or time changed are read)."""
        if not any(s.scope == list(self._roots) and not s.done for s in self._scans):
            self._full_scan()
        self._pump()

    def search(self, text: str, done: Callable[[SearchResult], None]) -> int:
        """Search in the background and return a ticket. Only the newest request is answered: an older
        request still waiting is dropped, and an older result arriving late is discarded."""
        self._search_ticket += 1
        compiled = compile_query(text)
        if compiled is None:
            self._search = None
            done(SearchResult())
            return self._search_ticket
        if self._status.state is IndexState.ERROR:
            done(SearchResult(problem=self._status.problem))
            return self._search_ticket
        self._search = (compiled, self._search_ticket, done)
        self._pump()
        return self._search_ticket

    def tags(self, done: Callable[[list[TagCount]], None]) -> None:
        self._tag_requests.append(done)
        self._pump()

    def skipped(self, done: Callable[[list[SkippedNote]], None]) -> None:
        self._skipped_requests.append(done)
        self._pump()

    def overview(self, paths: Sequence[str], since_ns: int, done: Callable[[Overview], None]) -> None:
        """Counts, recently changed notes, details of ``paths`` and top tags. Only the newest request is
        answered; ``since_ns`` is the wall-clock start of the "changed recently" window."""
        self._overview = (tuple(paths), since_ns, done)
        self._pump()

    def close(self, timeout_s: float = 5.0) -> None:
        """Stop, wait for the running job and close the database. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._scans.clear()
        self._checks.clear()
        self._search = None
        self._tag_requests.clear()
        self._skipped_requests.clear()
        self._overview = None
        if self._busy and not self._executor.drain(timeout_s):
            logger.warning("The search index job did not finish before shutdown")
        store, self._store = self._store, None
        if store is not None:
            store.close()
        self._set_status(IndexStatus(IndexState.CLOSED))

    @staticmethod
    def _normalized(roots: Sequence[str]) -> tuple[str, ...]:
        result: list[str] = []
        for root in roots:
            normalized = pathid.normalize(root)
            if not any(pathid.same_path(normalized, r) for r in result):
                result.append(normalized)
        return tuple(result)

    def _walk(self, folders: list[str]) -> Iterator[tuple[str, int, int]]:
        for folder in folders:
            yield from self._walker(folder)

    def _full_scan(self) -> None:
        self._prune = True
        self._scans.append(_Scan(list(self._roots), self._walk(list(self._roots))))

    def _within_roots(self, path: str) -> bool:
        return any(pathid.is_within(path, root) for root in self._roots)

    def _pump(self) -> None:
        if self._pumping:
            self._again = True
            return
        self._pumping = True
        try:
            while True:
                self._again = False
                if not self._busy and not self._closed and self._started:
                    self._submit_next()
                if not self._again:
                    break
        finally:
            self._pumping = False

    def _run[T](
        self,
        job: Callable[[IndexStore], T],
        done: Callable[[T], None],
        kind: str,
        failed: Callable[[], None] = lambda: None,
    ) -> None:
        store = self._store
        if store is None:
            return
        self._busy = True

        def work() -> T:
            return job(store)

        def finished(outcome: Outcome[T]) -> None:
            self._busy = False
            if self._closed:
                return
            if outcome.error is not None:
                logger.warning("Search index %s job failed: %s", kind, type(outcome.error).__name__)
                if isinstance(outcome.error, sqlite3.DatabaseError) and not isinstance(
                    outcome.error, sqlite3.OperationalError
                ):
                    self._recover()
                else:
                    failed()
            elif outcome.value is not None:
                done(outcome.value)
            self._pump()

        self._executor.submit(work, finished, "index")

    def _submit_next(self) -> None:
        if self._store is None:
            if self._opening:
                self._open()
            return
        if self._search is not None:
            query, ticket, callback = self._search
            self._search = None
            self._run(
                lambda s: s.search(query, self._limits),
                lambda result: self._deliver_search(ticket, callback, result),
                "search",
            )
            return
        if self._tag_requests:
            callbacks, self._tag_requests = self._tag_requests, []
            self._run(
                lambda s: s.tag_counts(self._limits.max_tags), lambda counts: _each(callbacks, counts), "tags"
            )
            return
        if self._skipped_requests:
            waiting, self._skipped_requests = self._skipped_requests, []
            self._run(
                lambda s: s.skipped(self._limits.max_skipped), lambda notes: _each(waiting, notes), "skipped"
            )
            return
        if self._overview is not None:
            wanted, since, answer = self._overview
            self._overview = None
            self._run(lambda s: s.overview(wanted, since, OVERVIEW_RECENT, OVERVIEW_TAGS), answer, "overview")
            return
        roots = self._roots
        if self._prune:
            self._prune = False
            self._run(lambda s: self._prune_job(s, roots), self._wrote, "prune")
            return
        if self._checks:
            paths = list(self._checks.values())[: self._limits.batch_notes]
            self._run(
                lambda s: self._check_job(s, paths, roots),
                self._after_batch,
                "batch",
                lambda: self._give_up(paths),
            )
            return
        if self._scans:
            scan = self._scans[0]
            if scan.done:
                self._scans.popleft()
                self._run(lambda s: self._finish_scan_job(s, scan), self._wrote, "finish")
            else:
                self._run(lambda s: self._scan_job(s, scan), self._after_batch, "scan", self._drop_scan)
            return
        self._publish(IndexState.READY)

    def _open(self) -> None:
        self._opening = False
        self._busy = True
        force = self._force_rebuild
        self._force_rebuild = False
        path = self._db_path

        def opened(outcome: Outcome[IndexStore]) -> None:
            self._busy = False
            store = outcome.value
            if outcome.error is not None or store is None:
                name = type(outcome.error).__name__ if outcome.error is not None else "None"
                logger.warning("The search index could not be opened: %s", name)
                self._set_status(IndexStatus(IndexState.ERROR, problem="Search is not available right now."))
                return
            if self._closed:
                store.close()
                return
            self._store = store
            self._publish(IndexState.INDEXING, rebuilt=store.rebuilt)
            self._pump()

        self._set_status(IndexStatus(IndexState.STARTING))
        self._executor.submit(lambda: IndexStore.open(path, force), opened, "index")

    def _give_up(self, paths: list[str]) -> None:
        """A batch failed for a reason other than a damaged database: do not retry those notes until
        they change again."""
        for path in paths:
            key = pathid.identity(path)
            self._failed.add(key)
            self._checks.pop(key, None)

    def _drop_scan(self) -> None:
        if self._scans:
            self._scans.popleft()

    def _recover(self) -> None:
        """A damaged database: close it, rebuild from scratch in the background and scan again."""
        self._rebuilds += 1
        store, self._store = self._store, None
        if store is not None:
            store.close()
        self._scans.clear()
        self._checks.clear()
        if self._rebuilds > 2:
            self._set_status(IndexStatus(IndexState.ERROR, problem="Search is not available right now."))
            return
        self._force_rebuild = True
        self._opening = True
        self._full_scan()

    def _deliver_search(
        self, ticket: int, callback: Callable[[SearchResult], None], result: SearchResult
    ) -> None:
        if ticket == self._search_ticket:
            callback(result)

    def _wrote(self, _removed: int) -> None:
        self._dirty = True

    def _after_batch(self, value: _BatchResult) -> None:
        for path in value.processed:
            self._checks.pop(pathid.identity(path), None)
        for path in value.changed:
            key = pathid.identity(path)
            if key not in self._failed:
                self._checks[key] = path
        if value.processed:
            self._dirty = True
        self._publish(IndexState.INDEXING)

    def _publish(self, state: IndexState, rebuilt: bool = False) -> None:
        store = self._store
        if store is None:
            return
        if state is IndexState.READY and (self._checks or self._scans or self._prune):
            state = IndexState.INDEXING
        if state is IndexState.READY and self._status.state is IndexState.READY and not self._dirty:
            return
        try:
            indexed, skipped = store.counts()
        except sqlite3.Error as exc:
            logger.warning("Reading search index counts failed: %s", type(exc).__name__)
            indexed, skipped = self._status.indexed, self._status.skipped
        self._set_status(
            IndexStatus(state, indexed, skipped, len(self._checks), rebuilt or self._status.rebuilt)
        )
        if self._dirty and state is IndexState.READY:
            self._dirty = False
            self.content_changed.emit()

    def _set_status(self, status: IndexStatus) -> None:
        if status != self._status:
            self._status = status
            self.status_changed.emit(status)

    @staticmethod
    def _prune_job(store: IndexStore, roots: tuple[str, ...]) -> int:
        prefixes = [pathid.identity(r).rstrip("\\") + "\\" for r in roots]
        outside = [k for k in store.all_keys() if not any(k.startswith(p) for p in prefixes)]
        return store.remove(outside)

    def _scan_job(self, store: IndexStore, scan: _Scan) -> _BatchResult:
        """Walk until the time box is used up; notes whose size or time changed are queued for reading."""
        deadline = time.monotonic() + self._limits.batch_seconds
        changed: list[str] = []
        for path, size, mtime_ns in scan.walker:
            key = pathid.identity(path)
            scan.seen.add(key)
            known = store.lookup(key)
            if known is None or (known.size, known.mtime_ns) != (size, mtime_ns):
                changed.append(path)
            if time.monotonic() >= deadline:
                return _BatchResult(changed=tuple(changed))
        scan.done = True
        return _BatchResult(changed=tuple(changed), scan_done=True)

    @staticmethod
    def _finish_scan_job(store: IndexStore, scan: _Scan) -> int:
        gone: list[str] = []
        for folder in scan.scope:
            gone += [k for k in store.keys_under(folder) if k not in scan.seen]
        return store.remove(gone)

    def _check_job(self, store: IndexStore, paths: list[str], roots: tuple[str, ...]) -> _BatchResult:
        deadline = time.monotonic() + self._limits.batch_seconds
        processed: list[str] = []
        for path in paths:
            self._check_one(store, path, roots)
            processed.append(path)
            if time.monotonic() >= deadline:
                break
        return _BatchResult(processed=tuple(processed))

    def _check_one(self, store: IndexStore, path: str, roots: tuple[str, ...]) -> None:
        key = pathid.identity(path)
        inside = is_note_path(path) and any(pathid.is_within(path, r) for r in roots)
        st = self._fs.stat(path) if inside else None
        if st is None:
            store.remove([key])
            return
        known = store.lookup(key)
        unchanged = known is not None and (known.size, known.mtime_ns) == (st.size, st.mtime_ns)
        if needs_hydration(st.attributes):
            if not (unchanged and known is not None and known.status is NoteStatus.INDEXED):
                store.put(path, st.size, st.mtime_ns, "", NoteStatus.ONLINE_ONLY, "")
            return
        if st.size > self._limits.max_note_bytes:
            store.put(path, st.size, st.mtime_ns, "", NoteStatus.TOO_LARGE, "")
            return
        try:
            data = self._fs.read_bytes(path, self._limits.max_note_bytes)
        except OSError as exc:
            logger.info("A note could not be read for search: %s", type(exc).__name__)
            store.put(path, st.size, st.mtime_ns, "", NoteStatus.UNREADABLE, "")
            return
        sha = digest(data)
        if known is not None and known.sha == sha and known.status is NoteStatus.INDEXED:
            store.touch(key, path, len(data), st.mtime_ns)
            return
        try:
            text, _format = textformat.decode(data, self._limits.max_note_bytes)
        except textformat.DecodeError as exc:
            too_large = exc.problem is textformat.DecodeProblem.TOO_LARGE
            status = NoteStatus.TOO_LARGE if too_large else NoteStatus.NOT_TEXT
            store.put(path, len(data), st.mtime_ns, sha, status, "")
            return
        store.put(path, len(data), st.mtime_ns, sha, NoteStatus.INDEXED, text, self._extract(text))
