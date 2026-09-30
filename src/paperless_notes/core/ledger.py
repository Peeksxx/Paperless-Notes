"""Signed revision ledger: the lineage of contents this install has seen for each note.

One append-only JSONL file per note in the local state folder (never beside the note, never inside it).
Each entry's hash covers the previous entry's hash and the entry's canonical JSON, so any truncation,
edit or reordering is evident. A damaged file loads as its longest valid prefix, the tail is rebuilt from
the history store, and nothing is ever raised to the user. Entries hold hashes and metadata only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import ntpath
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Any, Protocol

from paperless_notes.core import pathid
from paperless_notes.core.fsops import Expect, FileSystem, TooLargeError, atomic_save

logger = logging.getLogger(__name__)

GENESIS = ""
MAX_ENTRIES = 200
COMPACT_SLACK = 50
MAX_LINE_BYTES = 4096
MAX_FILE_BYTES = 256 * 1024
MAX_PARENTS = 8
MAX_TOTAL_BYTES = 32 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_TEXT_FIELD = re.compile(r"[\x20-\x7e]{0,64}\Z")


class Origin(StrEnum):
    INITIAL = "initial"
    LOCAL_SAVE = "local_save"
    EXTERNAL_OBSERVED = "external_observed"
    MERGE = "merge"
    CONFLICT_KEEP_THEIRS = "conflict_keep_theirs"
    CONFLICT_KEEP_BOTH = "conflict_keep_both"
    RESTORE = "restore"
    RECOVERED_DRAFT = "recovered_draft"


class Relation(Enum):
    SELF = "self"
    KNOWN_ANCESTOR = "known_ancestor"
    KNOWN_SIDE_VERSION = "known_side_version"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LedgerIdentity:
    install_id: str
    host: str
    session_id: str


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    seq: int
    prev: str
    content_sha256: str
    size: int
    mtime_ns: int
    file_id: int
    parents: tuple[str, ...]
    origin: Origin
    install_id: str
    host: str
    session_id: str
    wall_time: float
    entry_hash: str

    def fields(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "content_sha256": self.content_sha256,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "file_id": self.file_id,
            "parents": list(self.parents),
            "origin": self.origin.value,
            "install_id": self.install_id,
            "host": self.host,
            "session_id": self.session_id,
            "wall_time": self.wall_time,
        }

    def to_line(self) -> bytes:
        return _canonical({**self.fields(), "prev": self.prev, "entry_hash": self.entry_hash}) + b"\n"


@dataclass(frozen=True, slots=True)
class RelationResult:
    relation: Relation
    entry: LedgerEntry | None


@dataclass(frozen=True, slots=True)
class LoadReport:
    valid: int
    dropped: int
    rebuilt: int
    repaired: bool


class HistoryReader(Protocol):
    def lineage(self, path: str) -> list[tuple[int, str, str, int]]:
        """Snapshots of ``path`` oldest first as (taken_ms, sha256, reason, size)."""
        ...

    def find(self, path: str, sha256: str) -> bytes | None: ...


def _canonical(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def entry_hash(prev: str, fields: dict[str, Any]) -> str:
    return hashlib.sha256(prev.encode("ascii") + _canonical(fields)).hexdigest()


def _int(value: Any, low: int = 0) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= low:
        return value
    return None


def parse_entry(raw: bytes) -> LedgerEntry | None:
    """Decode one line; None unless every field is well formed and the hash matches."""
    if len(raw) > MAX_LINE_BYTES:
        return None
    try:
        obj = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    seq = _int(obj.get("seq"))
    size = _int(obj.get("size"))
    mtime = _int(obj.get("mtime_ns"), low=-(2**63))
    file_id = _int(obj.get("file_id"))
    prev, sha, digest_ = obj.get("prev"), obj.get("content_sha256"), obj.get("entry_hash")
    parents = obj.get("parents")
    wall = obj.get("wall_time")
    texts = [obj.get(k) for k in ("install_id", "host", "session_id")]
    install_id, host, session_id = (v if isinstance(v, str) else "" for v in texts)
    if (
        seq is None
        or size is None
        or mtime is None
        or file_id is None
        or not isinstance(prev, str)
        or not (prev == GENESIS or _SHA.match(prev))
        or not isinstance(sha, str)
        or not _SHA.match(sha)
        or not isinstance(digest_, str)
        or not _SHA.match(digest_)
        or not isinstance(parents, list)
        or len(parents) > MAX_PARENTS
        or not all(isinstance(p, str) and _SHA.match(p) for p in parents)
        or not isinstance(wall, int | float)
        or isinstance(wall, bool)
        or not all(isinstance(v, str) and _TEXT_FIELD.match(v) for v in texts)
        or obj.get("origin") not in Origin._value2member_map_
    ):
        return None
    entry = LedgerEntry(
        seq,
        prev,
        sha,
        size,
        mtime,
        file_id,
        tuple(parents),
        Origin(obj["origin"]),
        install_id,
        host,
        session_id,
        float(wall),
        digest_,
    )
    if entry_hash(prev, entry.fields()) != digest_:
        return None
    return entry


def _sync_run(job: Callable[[], None]) -> None:
    job()


class Ledger:
    """The ledger of one open note. Only open notes keep one in memory."""

    def __init__(
        self,
        fs: FileSystem,
        file: str,
        note_path: str,
        identity: LedgerIdentity,
        wall_clock: Callable[[], float],
        persist: Callable[[Callable[[], None]], None] = _sync_run,
        history: HistoryReader | None = None,
    ) -> None:
        self._fs = fs
        self._file = file
        self._note = note_path
        self._id = identity
        self._clock = wall_clock
        self._persist = persist
        self._history = history
        self.entries: list[LedgerEntry] = []

    @property
    def head(self) -> LedgerEntry | None:
        return self.entries[-1] if self.entries else None

    def load(self) -> LoadReport:
        try:
            data = self._fs.read_bytes(self._file, MAX_FILE_BYTES)
        except FileNotFoundError:
            return LoadReport(0, 0, 0, False)
        except TooLargeError:
            logger.warning("Ledger for %s exceeded its size cap; starting a new chain", self._label())
            return self._repair([], 1)
        except OSError as exc:
            logger.warning("Ledger for %s is unreadable (%s); using an empty chain", self._label(), exc)
            return LoadReport(0, 0, 0, False)
        lines = data.split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
        valid: list[LedgerEntry] = []
        prev = GENESIS
        for raw in lines:
            entry = parse_entry(raw)
            if entry is None or entry.prev != prev or (valid and entry.seq <= valid[-1].seq):
                break
            valid.append(entry)
            prev = entry.entry_hash
        dropped = len(lines) - len(valid)
        if dropped:
            return self._repair(valid, dropped)
        self.entries = valid
        if len(self.entries) > MAX_ENTRIES + COMPACT_SLACK:
            self.compact()
        return LoadReport(len(valid), 0, 0, False)

    def _label(self) -> str:
        return ntpath.basename(self._note)

    def _repair(self, valid: list[LedgerEntry], dropped: int) -> LoadReport:
        self.entries = valid
        rebuilt = self._rebuild_tail()
        logger.warning(
            "Ledger for %s was damaged: kept %d entries, dropped %d, rebuilt %d from history",
            self._label(),
            len(valid),
            dropped,
            rebuilt,
        )
        if len(self.entries) > MAX_ENTRIES + COMPACT_SLACK:
            self.compact()
        else:
            self._rewrite()
        return LoadReport(len(valid), dropped, rebuilt, True)

    def _rebuild_tail(self) -> int:
        if self._history is None:
            return 0
        try:
            snapshots = self._history.lineage(self._note)
        except OSError as exc:
            logger.warning("History unavailable while repairing a ledger: %s", exc)
            return 0
        after_ms = int(self.entries[-1].wall_time * 1000) if self.entries else -1
        count = 0
        for taken_ms, sha, reason, size in snapshots:
            if taken_ms <= after_ms or (self.head is not None and self.head.content_sha256 == sha):
                continue
            if reason == "saved":
                origin = Origin.LOCAL_SAVE
            elif reason == "opened" and not self.entries:
                origin = Origin.INITIAL
            else:
                origin = Origin.EXTERNAL_OBSERVED
            parents = (self.head.content_sha256,) if self.head else ()
            self.entries.append(self._make(sha, size, 0, 0, parents, origin, taken_ms / 1000, "rebuilt"))
            count += 1
        return count

    def _make(
        self,
        sha: str,
        size: int,
        mtime_ns: int,
        file_id: int,
        parents: tuple[str, ...],
        origin: Origin,
        wall_time: float,
        session_id: str | None = None,
    ) -> LedgerEntry:
        head = self.head
        prev = head.entry_hash if head else GENESIS
        seq = head.seq + 1 if head else 0
        unique_parents = tuple(dict.fromkeys(p for p in parents if p != sha))[:MAX_PARENTS]
        draft = LedgerEntry(
            seq,
            prev,
            sha,
            size,
            mtime_ns,
            file_id,
            unique_parents,
            origin,
            self._id.install_id,
            self._id.host,
            session_id or self._id.session_id,
            round(wall_time, 3),
            "",
        )
        return LedgerEntry(**{**_asdict(draft), "entry_hash": entry_hash(prev, draft.fields())})

    def append(
        self,
        sha: str,
        size: int,
        mtime_ns: int,
        file_id: int,
        parents: Iterable[str],
        origin: Origin,
        pinned: Iterable[str] = (),
    ) -> LedgerEntry:
        entry = self._make(sha, size, mtime_ns, file_id, tuple(parents), origin, self._clock())
        self.entries.append(entry)
        if len(self.entries) > MAX_ENTRIES + COMPACT_SLACK:
            self.compact(pinned)
        else:
            line = entry.to_line()
            fs, file = self._fs, self._file
            self._persist(lambda: fs.append(file, line))
        return entry

    def compact(self, pinned: Iterable[str] = ()) -> None:
        """Keep the first entry, the newest MAX_ENTRIES and any entry whose content is pinned."""
        pins = set(pinned)
        head_start = max(1, len(self.entries) - MAX_ENTRIES)
        keep = [self.entries[0]]
        keep += [e for e in self.entries[1:head_start] if e.content_sha256 in pins]
        keep += self.entries[head_start:]
        rechained: list[LedgerEntry] = []
        prev = GENESIS
        for e in keep:
            fields = e.fields()
            rechained.append(
                LedgerEntry(**{**_asdict(e), "prev": prev, "entry_hash": entry_hash(prev, fields)})
            )
            prev = rechained[-1].entry_hash
        self.entries = rechained
        self._rewrite()

    def _rewrite(self) -> None:
        data = b"".join(e.to_line() for e in self.entries)
        fs, file = self._fs, self._file

        def job() -> None:
            atomic_save(fs, file, data, Expect.ANY, len(data) + 1)

        self._persist(job)

    def find(self, sha: str) -> LedgerEntry | None:
        for entry in reversed(self.entries):
            if entry.content_sha256 == sha:
                return entry
        return None

    def relate(self, sha: str) -> RelationResult:
        head = self.head
        if head is None:
            return RelationResult(Relation.UNKNOWN, None)
        if head.content_sha256 == sha:
            return RelationResult(Relation.SELF, head)
        parents_of: dict[str, set[str]] = {}
        for e in self.entries:
            parents_of.setdefault(e.content_sha256, set()).update(e.parents)
        seen: set[str] = set()
        frontier = list(head.parents)
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(parents_of.get(current, ()))
        found = self.find(sha)
        if found is None:
            return RelationResult(Relation.UNKNOWN, None)
        if sha in seen:
            return RelationResult(Relation.KNOWN_ANCESTOR, found)
        return RelationResult(Relation.KNOWN_SIDE_VERSION, found)


def _asdict(entry: LedgerEntry) -> dict[str, Any]:
    return {name: getattr(entry, name) for name in LedgerEntry.__slots__}


class LedgerStore:
    def __init__(
        self,
        fs: FileSystem,
        directory: str,
        identity: LedgerIdentity,
        wall_clock: Callable[[], float],
        history: HistoryReader | None = None,
        max_total_bytes: int = MAX_TOTAL_BYTES,
    ) -> None:
        self._fs = fs
        self._dir = directory
        self._id = identity
        self._clock = wall_clock
        self._history = history
        self._max_total = max_total_bytes

    def file_for(self, path: str) -> str:
        return ntpath.join(self._dir, pathid.path_key(path)[:32] + ".jsonl")

    def open(self, path: str, persist: Callable[[Callable[[], None]], None] = _sync_run) -> Ledger:
        self._fs.make_dirs(self._dir)
        ledger = Ledger(self._fs, self.file_for(path), path, self._id, self._clock, persist, self._history)
        ledger.load()
        return ledger

    def enforce_caps(self, open_paths: Iterable[str] = ()) -> int:
        """Delete the least recently changed ledgers of closed notes until the total fits the cap."""
        keep = {ntpath.basename(self.file_for(p)).casefold() for p in open_paths}
        try:
            names = [n for n in self._fs.list_dir(self._dir) if n.endswith(".jsonl")]
        except FileNotFoundError:
            return 0
        files = []
        for name in names:
            st = self._fs.stat(ntpath.join(self._dir, name))
            if st is not None:
                files.append((st.mtime_ns, st.size, name))
        total = sum(size for _, size, _ in files)
        removed = 0
        for _, size, name in sorted(files):
            if total <= self._max_total:
                break
            if name.casefold() in keep:
                continue
            self._fs.remove(ntpath.join(self._dir, name))
            total -= size
            removed += 1
        return removed
