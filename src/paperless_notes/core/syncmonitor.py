"""Per-note sync monitoring: ledger, settle probes, evidence, oracle verdicts, sync status and diffs.

The session calls one-line hooks at the moments that matter (loaded, observed, saved, reloaded, conflict,
missing, I/O failure). Everything else lives here, so the session's save and reconcile logic is unchanged.
Probes only gather evidence; when they see the file move away from our write they ask the session to run
its normal check, which remains the only place that reconciles.
"""

from __future__ import annotations

import logging
import ntpath
import zlib
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING

from paperless_notes.core import textformat
from paperless_notes.core.diffing import DiffModel, diff_lines
from paperless_notes.core.evidence import EvidenceStore
from paperless_notes.core.fsops import FileStamp, StatInfo, digest
from paperless_notes.core.ledger import HistoryReader, Ledger, LedgerEntry, LedgerStore, Origin, Relation
from paperless_notes.core.merge import merge3
from paperless_notes.core.oracle import (
    Action,
    Code,
    Compare,
    Diagnosis,
    NoteContext,
    OneDriveModel,
    OwnWrite,
    Severity,
    Side,
    Verdict,
    diagnose,
)
from paperless_notes.core.probes import (
    CloudState,
    IdentityChange,
    InSyncReader,
    MtimeRelation,
    PlaceholderApi,
    SiblingReport,
    cloud_state,
    identity_change,
    mtime_relation,
    probe_siblings,
)
from paperless_notes.core.runtime import Outcome, SerialQueue, TimerHandle

if TYPE_CHECKING:
    from paperless_notes.core.session import NoteSession, SessionDeps

logger = logging.getLogger(__name__)

QUIET = frozenset({Code.UP_TO_DATE, Code.ECHO_OF_OWN_WRITE})
OWN_ORIGINS = frozenset({Origin.LOCAL_SAVE, Origin.MERGE, Origin.RESTORE, Origin.RECOVERED_DRAFT})
REVERSIONS = frozenset({Code.ROLLED_BACK_TO_KNOWN_VERSION, Code.OUR_WRITE_REVERTED})
_TEXT_CACHE = 4
_MAX_STICKIES = 4


class SyncState(Enum):
    LOCAL_ONLY = "local_only"
    SAVED_HERE = "saved_here"
    UPLOAD_PENDING = "upload_pending"
    UPLOADED = "uploaded"
    UPDATED_FROM_ELSEWHERE = "updated_from_elsewhere"
    NEEDS_REVIEW = "needs_review"
    OFFLINE_OR_STALLED = "offline_or_stalled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SyncStatus:
    state: SyncState
    since: float
    eta_seconds: float | None
    confidence: float
    plain_text: str


@dataclass(frozen=True, slots=True)
class Explanation:
    title: str
    text: str
    diff: DiffModel | None
    extra: tuple[DiffModel, ...] = ()


@dataclass(frozen=True, slots=True)
class ActionResult:
    done: bool
    message: str
    explanation: Explanation | None = None


UNKNOWN_STATUS = SyncStatus(SyncState.UNKNOWN, 0.0, None, 0.0, "Sync status unknown")


@dataclass
class SyncServices:
    ledgers: LedgerStore
    model: OneDriveModel = field(default_factory=OneDriveModel)
    in_sync: InSyncReader = field(default_factory=PlaceholderApi)
    history: HistoryReader | None = None
    trace: Callable[[NoteContext, Verdict], None] | None = None
    evidence: EvidenceStore | None = None

    def attach(self, session: NoteSession, deps: SessionDeps) -> SyncMonitor:
        return SyncMonitor(session, deps, self)


class MonitorBase:
    """No monitoring: used when a session has no sync services (tests, tools)."""

    status: SyncStatus = UNKNOWN_STATUS
    diagnosis: Diagnosis | None = None

    def loaded(self, stat: StatInfo, sha: str, text: str) -> None:
        return

    def observed(self, stat: StatInfo, sha: str | None, data: bytes | None = None) -> None:
        return

    def saved(self, stamp: FileStamp, text: str) -> None:
        return

    def reloaded(self, kind: str, stamp: FileStamp, text: str) -> None:
        return

    def conflict(self, stamp: FileStamp) -> None:
        return

    def resolved(self, choice: str, sibling: str | None = None) -> None:
        return

    def missing(self) -> None:
        return

    def io_failed(self, transient: bool) -> None:
        return

    def hydrating(self, active: bool) -> None:
        return

    def activated(self) -> None:
        return

    def draft_recovered(self) -> None:
        return

    def restoring(self, sha: str) -> None:
        return

    def closed(self) -> None:
        return

    def diff(self, compare: Compare) -> DiffModel | None:
        return None

    def explain(self, diagnosis: Diagnosis) -> Explanation:
        return Explanation(diagnosis.title, diagnosis.message, None)

    def apply(self, diagnosis: Diagnosis, action: Action) -> ActionResult:
        return ActionResult(False, "Sync monitoring is not available for this note.")

    def lineage(self) -> tuple[LedgerEntry, ...]:
        return ()


@dataclass
class _OwnWrite:
    stamp: FileStamp
    at: float
    verified: bool = False
    uploaded: bool = False
    overdue: bool = False
    signal: bool = False
    reads: int = 0
    agreed: int = 0
    last_stat: tuple[int, int, int] | None = None


class SyncMonitor(MonitorBase):
    def __init__(self, session: NoteSession, deps: SessionDeps, services: SyncServices) -> None:
        self._s = session
        self._fs = deps.fs
        self._clock = deps.scheduler
        self._exe = deps.executor
        self._svc = services
        self._model = services.model
        self._queue = SerialQueue(deps.executor, "ledger")
        self._ledger: Ledger | None = None
        self._stickies: list[Diagnosis] = []
        self.diagnosis = None
        self.status = UNKNOWN_STATUS
        self._own: _OwnWrite | None = None
        self._probes: list[TimerHandle] = []
        self._timers: dict[str, TimerHandle] = {}
        self._last: tuple[StatInfo, str | None] | None = None
        self._identity = IdentityChange.NONE
        self._mtime = MtimeRelation.SAME
        self._cloud = CloudState.UNKNOWN
        self._in_sync: bool | None = None
        self._acknowledged: set[str] = set()
        self._siblings: tuple[str, ...] = ()
        self._locks: deque[float] = deque(maxlen=64)
        self._hydrating_since: float | None = None
        self._missing = False
        self._pending: tuple[Origin, tuple[str, ...] | None] | None = None
        self._reload_origin: Origin | None = None
        self._conflict_base: str | None = None
        self._texts: OrderedDict[str, bytes] = OrderedDict()
        self._sticky_texts: dict[str, bytes] = {}
        self._dismissed: set[_Key] = set()
        self._updated_at: float | None = None
        self._closed = False
        self._owner = object()
        self.probe_reads: list[int] = []
        self.verdicts: list[tuple[NoteContext, Verdict]] = []

    def loaded(self, stat: StatInfo, sha: str, text: str) -> None:
        self._remember(sha, text)
        try:
            self._ledger = self._svc.ledgers.open(self._s.path, self._queue.submit)
        except OSError as exc:
            logger.warning("Ledger unavailable for %s: %s", ntpath.basename(self._s.path), exc)
            self._ledger = None
        ledger = self._ledger
        self._update_cloud(stat)
        if ledger is not None:
            head = ledger.head
            relation = ledger.relate(sha)
            if head is None:
                ledger.append(sha, stat.size, stat.mtime_ns, stat.file_id, (), Origin.INITIAL)
                self._protect_evidence()
            elif relation.relation is not Relation.SELF:
                previous = StatInfo(head.size, head.mtime_ns, 0, 0, head.file_id)
                self._evidence(previous, stat, head.content_sha256, sha)
                own_verified = head.origin in OWN_ORIGINS
                drops = relation.relation is Relation.UNKNOWN and self._drops_own_changes(head, text)
                self._stick(relation.relation, head.content_sha256, sha, own_verified, drops)
                self._append(Origin.EXTERNAL_OBSERVED, sha, stat, (head.content_sha256,))
        self._last = (stat, sha)
        self._probe_siblings()
        self.evaluate()

    def observed(self, stat: StatInfo, sha: str | None, data: bytes | None = None) -> None:
        """A check or probe saw the file. ``data`` (already read) keeps a new version comparable later."""
        if sha is not None and data is not None and sha not in self._texts:
            self._remember(sha, textformat.decode_lossy(data))
        self._update_cloud(stat)
        self._missing = False
        last = self._last
        known = last[1] if last is not None else None
        if last is not None and (last[0] != stat or (sha is not None and sha != known)):
            self._evidence(last[0], stat, known, sha)
        if (
            sha is None
            and last is not None
            and (last[0].size, last[0].mtime_ns) == (stat.size, stat.mtime_ns)
        ):
            sha = known
        self._last = (stat, sha)
        ledger = self._ledger
        if sha is not None and ledger is not None and ledger.head is not None:
            relation = ledger.relate(sha)
            if relation.relation in (Relation.KNOWN_ANCESTOR, Relation.KNOWN_SIDE_VERSION):
                verified = self._own is not None and self._own.verified
                self._stick(relation.relation, ledger.head.content_sha256, sha, verified)
        self.evaluate()

    def saved(self, stamp: FileStamp, text: str) -> None:
        self._remember(stamp.sha256, text)
        origin, parents = self._pending or (Origin.LOCAL_SAVE, None)
        self._pending = None
        self._append(origin, stamp.sha256, _stat_of(stamp), parents)
        if self._own is not None:
            self.probe_reads.append(self._own.reads)
        self._own = _OwnWrite(stamp, self._clock.now())
        self._last = (_stat_of(stamp), stamp.sha256)
        self._identity = IdentityChange.NONE
        self._mtime = MtimeRelation.SAME
        self._updated_at = None
        self._dismissed = {d for d in self._dismissed if d[1:] != (None, None)}
        self._stickies = [d for d in self._stickies if d.severity > Severity.NOTICE]
        self._pin_texts()
        self._schedule_probes()
        self._probe_siblings()
        self.evaluate()

    def reloaded(self, kind: str, stamp: FileStamp, text: str) -> None:
        self._remember(stamp.sha256, text)
        ledger = self._ledger
        stat = _stat_of(stamp)
        if self._last is not None and self._last[1] != stamp.sha256 and kind != "kept_theirs":
            self._evidence(self._last[0], stat, self._last[1], stamp.sha256)
        if ledger is not None:
            head = ledger.head
            head_sha = head.content_sha256 if head else None
            relation = ledger.relate(stamp.sha256).relation
            if kind == "kept_theirs":
                origin = self._reload_origin or Origin.CONFLICT_KEEP_THEIRS
                self._append(origin, stamp.sha256, stat, (stamp.sha256,))
            elif relation is not Relation.SELF:
                if (
                    relation in (Relation.KNOWN_ANCESTOR, Relation.KNOWN_SIDE_VERSION)
                    and head_sha is not None
                ):
                    verified = self._own is not None and self._own.verified
                    self._stick(relation, head_sha, stamp.sha256, verified)
                elif head is not None and head_sha is not None:
                    drops = self._drops_own_changes(head, text)
                    verified = self._own is not None and self._own.verified
                    self._stick(relation, head_sha, stamp.sha256, verified, drops)
                self._append(Origin.EXTERNAL_OBSERVED, stamp.sha256, stat, (head_sha,) if head_sha else ())
            if kind == "merged":
                parents = tuple(p for p in (stamp.sha256, self._conflict_base or head_sha) if p)
                self._pending = (Origin.MERGE, parents)
        self._reload_origin = None
        if self._own is not None:
            self.probe_reads.append(self._own.reads)
        self._own = None
        self._cancel_probes()
        self._last = (stat, stamp.sha256)
        self._updated_at = self._clock.now()
        self._probe_siblings()
        self.evaluate()

    def conflict(self, stamp: FileStamp) -> None:
        held = self._s.conflict
        if held is not None and held.theirs is not None:
            self._remember(stamp.sha256, held.theirs)
        ledger = self._ledger
        if ledger is not None and ledger.head is not None:
            self._conflict_base = ledger.head.content_sha256
            if ledger.head.content_sha256 != stamp.sha256:
                self._append(Origin.EXTERNAL_OBSERVED, stamp.sha256, _stat_of(stamp), (self._conflict_base,))
        self._last = (_stat_of(stamp), stamp.sha256)
        self._probe_siblings()
        self.evaluate()

    def resolved(self, choice: str, sibling: str | None = None) -> None:
        base = self._conflict_base
        ledger = self._ledger
        theirs = ledger.head.content_sha256 if ledger is not None and ledger.head is not None else None
        if choice == "keep_mine":
            self._pending = (Origin.LOCAL_SAVE, (base,) if base else ())
        elif choice == "manual":
            self._pending = (Origin.MERGE, tuple(p for p in (base, theirs) if p))
        elif choice == "keep_both":
            self._reload_origin = Origin.CONFLICT_KEEP_BOTH
            if sibling:
                self._acknowledged.add(ntpath.basename(sibling))
        self.evaluate()

    def missing(self) -> None:
        self._missing = True
        ledger = self._ledger
        head = ledger.head.content_sha256 if ledger is not None and ledger.head is not None else None
        self._stick_context(NoteContext(disk_present=False, head_sha=head))
        self._probe_siblings()
        self.evaluate()

    def io_failed(self, transient: bool) -> None:
        if transient:
            self._locks.append(self._clock.now())
        self.evaluate()

    def hydrating(self, active: bool) -> None:
        self._hydrating_since = self._clock.now() if active else None
        if active:
            self._set_timer("hydration", self._model.hydration_stall_s + 0.01, self.evaluate)
        else:
            self._cancel_timer("hydration")
        self.evaluate()

    def activated(self) -> None:
        self._probe_siblings()

    def draft_recovered(self) -> None:
        self._pending = (Origin.RECOVERED_DRAFT, None)

    def restoring(self, sha: str) -> None:
        ledger = self._ledger
        head = ledger.head.content_sha256 if ledger is not None and ledger.head is not None else None
        self._pending = (Origin.RESTORE, tuple(p for p in (head, sha) if p))

    def closed(self) -> None:
        self._closed = True
        store = self._svc.evidence
        if store is not None:
            path, owner = self._s.path, self._owner
            self._queue.submit(lambda: store.release(path, owner))
        if self._own is not None:
            self.probe_reads.append(self._own.reads)
            self._own = None
        self._cancel_probes()
        for name in list(self._timers):
            self._cancel_timer(name)
        self._ledger = None

    def evaluate(self) -> None:
        if self._closed:
            return
        context = self._context()
        verdict = diagnose(context, self._model)
        self.verdicts.append((context, verdict))
        if len(self.verdicts) > 256:
            del self.verdicts[:128]
        if self._svc.trace is not None:
            self._svc.trace(context, verdict)
        chosen = verdict.primary
        if _key(chosen) in self._dismissed:
            chosen = _QUIET_DIAGNOSIS
        sticky = self._sticky
        if sticky is not None and (sticky.severity > chosen.severity or chosen.code in QUIET):
            chosen = sticky
        published = None if chosen.code in QUIET else chosen
        if published != self.diagnosis:
            self.diagnosis = published
            self._s.diagnosis_changed.emit(published)
        status = self._derive_status(context, published)
        if (status.state, status.plain_text) != (self.status.state, self.status.plain_text):
            self.status = status
            self._s.sync_status_changed.emit(status)
        elif status.eta_seconds != self.status.eta_seconds:
            self.status = replace(status, since=self.status.since)

    def _context(self) -> NoteContext:
        ledger = self._ledger
        head = ledger.head if ledger is not None else None
        disk_sha = self._last[1] if self._last is not None else None
        relation = Relation.SELF
        related: str | None = None
        if ledger is not None and head is not None and disk_sha is not None:
            result = ledger.relate(disk_sha)
            relation = result.relation
            related = result.entry.content_sha256 if result.entry else None
        now = self._clock.now()
        window = [t for t in self._locks if now - t <= self._model.lock_window_s]
        new_siblings = tuple(n for n in self._siblings if n not in self._acknowledged)
        own = self._own
        return NoteContext(
            relation=relation,
            disk_present=not self._missing,
            dirty=self._s.dirty,
            file_id_changed=self._identity is IdentityChange.REPLACED_SAME_CONTENT
            or (self._identity is IdentityChange.NEW_VERSION and relation is not Relation.SELF),
            cloud=self._cloud,
            in_sync=self._in_sync,
            mtime=self._mtime,
            new_siblings=new_siblings,
            own_write=self._own_state(),
            own_write_verified=own is not None and own.verified,
            sharing_violations=len(window),
            retry_in_s=self._model.lock_retry_hint_s if window else None,
            hydrating_s=None if self._hydrating_since is None else now - self._hydrating_since,
            conflict_active=self._s.conflict is not None,
            head_sha=head.content_sha256 if head else None,
            disk_sha=disk_sha,
            related_sha=related,
        )

    def _own_state(self) -> OwnWrite:
        own = self._own
        if own is None:
            return OwnWrite.NONE
        if own.overdue:
            return OwnWrite.OVERDUE
        if own.uploaded:
            return OwnWrite.UPLOADED
        if own.signal and self._cloud is not CloudState.NOT_IN_SYNC_ROOT:
            return OwnWrite.PENDING
        return OwnWrite.VERIFIED if own.verified else OwnWrite.WRITTEN

    def _derive_status(self, context: NoteContext, published: Diagnosis | None) -> SyncStatus:
        now = self._clock.now()

        def status(
            state: SyncState, text: str, confidence: float = 0.95, eta: float | None = None
        ) -> SyncStatus:
            since = self.status.since if self.status.state is state else now
            return SyncStatus(state, since, eta, confidence, text)

        own = self._own
        if published is not None and published.code in (Code.UPLOAD_STALLED, Code.HYDRATION_STALLED):
            return status(SyncState.OFFLINE_OR_STALLED, published.message, published.confidence)
        if published is not None and published.severity >= Severity.WARN:
            return status(
                SyncState.NEEDS_REVIEW, f"Needs your attention: {published.title}", published.confidence
            )
        if context.cloud is CloudState.NOT_IN_SYNC_ROOT:
            return status(SyncState.LOCAL_ONLY, "Saved on this PC (this folder is not synced)")
        if own is not None:
            if own.uploaded:
                return status(
                    SyncState.UPLOADED, "Saved and uploaded to OneDrive", self._model.in_sync_confidence
                )
            if own.overdue:
                return status(
                    SyncState.OFFLINE_OR_STALLED,
                    "Saved on this PC; OneDrive has not uploaded it yet",
                    self._model.in_sync_confidence,
                )
            if own.signal:
                eta = max(0.0, own.at + self._model.upload_eta_s(own.stamp.size) - now)
                return status(
                    SyncState.UPLOAD_PENDING,
                    "Saved on this PC, waiting to upload",
                    self._model.in_sync_confidence,
                    eta,
                )
            return status(SyncState.SAVED_HERE, "Saved on this PC")
        if self._updated_at is not None:
            return status(SyncState.UPDATED_FROM_ELSEWHERE, "Updated with changes from another device")
        if context.in_sync is True:
            return status(SyncState.UPLOADED, "In sync with OneDrive", self._model.in_sync_confidence)
        return status(SyncState.UNKNOWN, "In a synced folder; upload state unknown", 0.0)

    def _append(
        self, origin: Origin, sha: str, stat: StatInfo, parents: tuple[str, ...] | None = None
    ) -> None:
        ledger = self._ledger
        if ledger is None:
            return
        head = ledger.head
        if parents is None:
            parents = (head.content_sha256,) if head else ()
        ledger.append(sha, stat.size, stat.mtime_ns, stat.file_id, parents, origin, self._pinned())
        self._protect_evidence()

    def _pinned(self) -> set[str]:
        pins: set[str] = set()
        for d in (self.diagnosis, *self._stickies):
            if d is not None and d.compare is not None:
                pins.update(r for r in (d.compare.left_ref, d.compare.right_ref) if r)
        return pins

    def _stick(
        self, relation: Relation, head_sha: str, disk_sha: str, own_verified: bool, drops: bool = False
    ) -> None:
        self._stick_context(
            NoteContext(
                relation=relation,
                head_sha=head_sha,
                disk_sha=disk_sha,
                own_write_verified=own_verified,
                drops_own_changes=drops,
                file_id_changed=self._identity
                in (IdentityChange.REPLACED_SAME_CONTENT, IdentityChange.NEW_VERSION),
                mtime=self._mtime,
            )
        )

    def _stick_context(self, context: NoteContext) -> None:
        verdict = diagnose(context, self._model)
        self.verdicts.append((context, verdict))
        candidate = verdict.primary
        if candidate.code in QUIET or _key(candidate) in self._dismissed:
            return
        key = _key(candidate)
        kept = [d for d in self._stickies if _key(d) != key]
        self._stickies = [*kept[-(_MAX_STICKIES - 1) :], candidate]
        self._pin_texts()

    @property
    def _sticky(self) -> Diagnosis | None:
        """The newest unresolved warning, else the newest note; older ones resurface once it is handled."""
        serious = [d for d in self._stickies if d.severity >= Severity.WARN]
        pool = serious or self._stickies
        return pool[-1] if pool else None

    def _drop_sticky(self, diagnosis: Diagnosis) -> None:
        key = _key(diagnosis)
        floor = Severity.WARN if diagnosis.severity >= Severity.WARN else Severity.INFO
        self._stickies = [d for d in self._stickies if _key(d) != key and d.severity >= floor]
        self._pin_texts()

    def _pin_texts(self) -> None:
        pinned: dict[str, bytes] = {}
        for d in self._stickies:
            for ref in self._refs(d):
                blob = self._texts.get(ref) or self._sticky_texts.get(ref)
                if blob is not None:
                    pinned[ref] = blob
        self._sticky_texts = pinned
        self._protect_evidence()

    def _protect_evidence(self) -> None:
        """Tell the evidence store which exact versions this note still needs."""
        store = self._svc.evidence
        if store is None or self._closed:
            return
        path, owner, keep = self._s.path, self._owner, self._evidence_keep()
        self._queue.submit(lambda: store.protect(path, owner, keep))

    def _refs(self, diagnosis: Diagnosis) -> list[str]:
        compare = diagnosis.compare
        if compare is None:
            return []
        refs = [r for r in (compare.left_ref, compare.right_ref) if r]
        ledger = self._ledger
        if diagnosis.code is Code.OUR_WRITE_OVERWRITTEN and ledger is not None and compare.left_ref:
            entry = ledger.find(compare.left_ref)
            if entry is not None and entry.parents:
                refs.append(entry.parents[0])
        return refs

    def _drops_own_changes(self, head: LedgerEntry, theirs: str) -> bool:
        """True when ``theirs`` lacks changes from our last save, judged by merging it with that save."""
        if head.origin not in OWN_ORIGINS or not head.parents:
            return False
        ours = self._text_for(head.content_sha256)
        parent = self._text_for(head.parents[0])
        if ours is None or parent is None or ours == parent:
            return False
        merged = merge3(parent, ours, theirs)
        return not merged.clean or merged.text != theirs

    def _restore_base(self, diagnosis: Diagnosis) -> str | None:
        compare = diagnosis.compare
        if compare is None:
            return None
        if diagnosis.code in REVERSIONS:
            return self._text_for(compare.right_ref)
        ledger = self._ledger
        entry = ledger.find(compare.left_ref) if ledger is not None and compare.left_ref else None
        return self._text_for(entry.parents[0]) if entry is not None and entry.parents else None

    def _evidence(
        self, previous: StatInfo, current: StatInfo, previous_sha: str | None, sha: str | None
    ) -> None:
        stamp = FileStamp.of(previous, previous_sha) if previous_sha else None
        self._identity = identity_change(stamp, current, sha)
        reused = False
        ledger = self._ledger
        if ledger is not None and sha is not None and current.mtime_ns != previous.mtime_ns:
            reused = any(
                e.mtime_ns == current.mtime_ns and e.content_sha256 != sha
                for e in ledger.entries
                if e.mtime_ns
            )
        self._mtime = mtime_relation(
            previous.mtime_ns,
            current.mtime_ns,
            self._clock.wall_time(),
            self._model.future_tolerance_s,
            reused,
        )

    def _update_cloud(self, st: StatInfo, own: _OwnWrite | None = None) -> None:
        self._cloud = cloud_state(st, self._s.in_sync_root)
        try:
            self._in_sync = self._svc.in_sync(self._s.path, st)
        except OSError as exc:
            logger.debug("In-sync probe failed: %s", exc)
            self._in_sync = None
        own = own or self._own
        if own is None or self._cloud is CloudState.NOT_IN_SYNC_ROOT:
            return
        if self._in_sync is not None:
            own.signal = True
        if self._in_sync is True:
            own.uploaded = True
            own.overdue = False
            self._cancel_timer("deadline")
        elif own.signal and "deadline" not in self._timers and not own.overdue and not own.uploaded:
            due = own.at + self._model.upload_eta_s(own.stamp.size) - self._clock.now()
            self._set_timer("deadline", max(0.1, due), self._deadline_check)

    def _deadline_check(self) -> None:
        own = self._own
        if own is None:
            return
        fs, path = self._fs, self._s.path

        def done(out: Outcome[StatInfo | None]) -> None:
            if own is not self._own or self._closed:
                return
            if out.value is not None:
                self._update_cloud(out.value, own)
            if not own.uploaded:
                own.overdue = True
            self.evaluate()

        self._exe.submit(lambda: fs.stat(path), done, "probe")

    def _schedule_probes(self) -> None:
        self._cancel_probes()
        own = self._own
        for index, delay in enumerate(self._model.settle_schedule_s):
            self._probes.append(self._clock.call_later(delay, self._prober(own, index), coarse=True))

    def _prober(self, own: _OwnWrite | None, index: int) -> Callable[[], None]:
        return lambda: self._probe(own, index)

    def _cancel_probes(self) -> None:
        for handle in self._probes:
            handle.cancel()
        self._probes = []

    def _probe(self, own: _OwnWrite | None, index: int) -> None:
        if own is None or own is not self._own or self._closed:
            return
        fs, path = self._fs, self._s.path
        self._exe.submit(lambda: fs.stat(path), lambda out: self._after_stat(own, index, out), "probe")

    def _after_stat(self, own: _OwnWrite, index: int, out: Outcome[StatInfo | None]) -> None:
        if own is not self._own or self._closed:
            return
        if out.error is not None:
            logger.debug("Settle probe failed: %s", out.error)
            return
        st = out.value
        if st is None:
            self._s.check_now()
            return
        self._update_cloud(st, own)
        key = (st.size, st.mtime_ns, st.file_id)
        ours = (own.stamp.size, own.stamp.mtime_ns, own.stamp.file_id)
        own.agreed = own.agreed + 1 if own.last_stat == key else 0
        own.last_stat = key
        last = index == len(self._model.settle_schedule_s) - 1
        upload_done = own.uploaded or self._cloud is CloudState.NOT_IN_SYNC_ROOT
        stop_early = own.agreed >= 1 and upload_done
        final = last or stop_early
        budget = self._model.max_probe_reads if final else self._model.max_probe_reads - 1
        if (key != ours or final) and own.reads < budget:
            own.reads += 1
            fs, path, limit = self._fs, self._s.path, textformat.DEFAULT_MAX_BYTES * 20

            def read_hash() -> tuple[str, bytes]:
                data = fs.read_bytes(path, limit)
                return digest(data), data

            self._exe.submit(read_hash, lambda o: self._after_hash(own, st, o, final), "probe")
        elif stop_early:
            self._cancel_probes()
        self.evaluate()

    def _after_hash(self, own: _OwnWrite, st: StatInfo, out: Outcome[tuple[str, bytes]], final: bool) -> None:
        if own is not self._own or self._closed:
            return
        if out.error is not None:
            logger.debug("Settle probe read failed: %s", out.error)
            return
        sha, data = out.unwrap()
        if sha == own.stamp.sha256:
            own.verified = True
        else:
            self.observed(st, sha, data)
            self._s.check_now()
        if final:
            self._cancel_probes()
        self.evaluate()

    def _probe_siblings(self) -> None:
        fs, path = self._fs, self._s.path

        def done(out: Outcome[SiblingReport]) -> None:
            if self._closed:
                return
            if out.error is not None:
                logger.debug("Sibling probe failed: %s", out.error)
                return
            names = out.unwrap().names
            if names != self._siblings:
                self._siblings = names
                self._s.report_conflict_copies(list(names))
                self.evaluate()

        self._exe.submit(lambda: probe_siblings(fs, path), done, "probe")

    def _set_timer(self, name: str, delay: float, fn: Callable[[], None]) -> None:
        self._cancel_timer(name)

        def fire() -> None:
            self._timers.pop(name, None)
            if not self._closed:
                fn()

        self._timers[name] = self._clock.call_later(delay, fire, coarse=True)

    def _cancel_timer(self, name: str) -> None:
        handle = self._timers.pop(name, None)
        if handle is not None:
            handle.cancel()

    def _remember(self, sha: str, text: str) -> None:
        self._texts[sha] = zlib.compress(text.encode("utf-8", "surrogatepass"), 1)
        self._texts.move_to_end(sha)
        while len(self._texts) > _TEXT_CACHE:
            self._texts.popitem(last=False)
        store = self._svc.evidence
        if store is not None and not self._closed:
            path, keep, owner = self._s.path, self._evidence_keep(), self._owner
            protected = frozenset((sha, *keep))
            self._queue.submit(lambda: store.put(path, sha, text, keep, owner=owner, protect=protected))

    def _evidence_keep(self) -> tuple[str, ...]:
        refs = [r for d in self._stickies for r in self._refs(d)]
        ledger = self._ledger
        head = ledger.head if ledger is not None else None
        if head is not None:
            refs += [head.content_sha256, *head.parents[:1]]
        return tuple(refs)

    def _text_for(self, sha: str | None) -> str | None:
        """Text of one exact version: memory, then sync evidence, then history."""
        if sha is None:
            return None
        blob = self._texts.get(sha) or self._sticky_texts.get(sha)
        if blob is not None:
            return zlib.decompress(blob).decode("utf-8", "surrogatepass")
        store = self._svc.evidence
        text = store.get(self._s.path, sha) if store is not None else None
        if text is not None:
            return text
        history = self._svc.history
        data = history.find(self._s.path, sha) if history is not None else None
        return None if data is None else textformat.decode_lossy(data)

    def _version_text(self, sha: str | None) -> str | None:
        """Retained text of ``sha``, or the file on disk only if it still has exactly that hash."""
        text = self._text_for(sha)
        if text is not None or sha is None or self._cloud is CloudState.CLOUD_ONLY:
            return text
        try:
            data = self._fs.read_bytes(self._s.path, textformat.DEFAULT_MAX_BYTES * 20)
        except OSError as exc:
            logger.info("Could not read the note for comparison: %s", exc)
            return None
        return textformat.decode_lossy(data) if digest(data) == sha else None

    def _side_text(self, side: Side, ref: str | None) -> str | None:
        s = self._s
        if side is Side.BUFFER:
            return s.buffer_text()
        if side is Side.BASE:
            if ref is not None:
                return self._version_text(ref)
            conflict = s.conflict
            return conflict.base if conflict is not None else s.base_text()
        if side is Side.THEIRS:
            conflict = s.conflict
            return conflict.theirs if conflict is not None else None
        if side is Side.DISK:
            if ref is not None:
                return self._version_text(ref)
            if self._cloud is CloudState.CLOUD_ONLY:
                return None
            return self._read_text(s.path)
        if side is Side.CONFLICT_SIBLING:
            return None if ref is None else self._read_text(ntpath.join(ntpath.dirname(s.path), ref))
        if side is Side.DRAFT:
            draft = s.stored_draft()
            return draft.text if draft is not None else None
        return self._version_text(ref)

    def _read_text(self, path: str) -> str | None:
        try:
            return textformat.decode_lossy(self._fs.read_bytes(path, textformat.DEFAULT_MAX_BYTES * 20))
        except OSError as exc:
            logger.info("Could not read a file for comparison: %s", exc)
            return None

    def lineage(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._ledger.entries) if self._ledger is not None else ()

    def diff(self, compare: Compare) -> DiffModel | None:
        left = self._side_text(compare.left, compare.left_ref)
        right = self._side_text(compare.right, compare.right_ref)
        if left is None or right is None:
            return None
        return diff_lines(left, right, labels=(_LABELS[compare.left], _LABELS[compare.right]))

    def explain(self, diagnosis: Diagnosis) -> Explanation:
        diff = self.diff(diagnosis.compare) if diagnosis.compare is not None else None
        extra: tuple[DiffModel, ...] = ()
        conflict = self._s.conflict
        if (
            diagnosis.code is Code.REMOTE_UPDATE_DIVERGED
            and conflict is not None
            and conflict.theirs is not None
        ):
            extra = (
                diff_lines(conflict.base, self._s.buffer_text(), labels=("base", "yours")),
                diff_lines(conflict.base, conflict.theirs, labels=("base", "theirs")),
            )
        text = diagnosis.message
        if diff is None and diagnosis.compare is not None:
            text += " One of the versions is not available to compare."
        return Explanation(diagnosis.title, text, diff, extra)

    def apply(self, diagnosis: Diagnosis, action: Action) -> ActionResult:
        s = self._s
        if action not in diagnosis.actions and action is not Action.DISMISS:
            return ActionResult(False, "That action does not apply to this situation.")
        if action is Action.SHOW_DIFF:
            return ActionResult(True, "", self.explain(diagnosis))
        if action is Action.DISMISS:
            self._dismissed.add(_key(diagnosis))
            if diagnosis.code is Code.CONFLICT_COPY_CREATED:
                self._acknowledged.update(self._siblings)
            self._drop_sticky(diagnosis)
            self.evaluate()
            return ActionResult(True, "Dismissed.")
        from paperless_notes.core.session import FlushReason, Resolution

        if action is Action.RETRY_NOW:
            self._locks.clear()
            s.check_now()
            s.flush(FlushReason.RETRY)
            return ActionResult(True, "Retrying now.")
        if action is Action.RESTORE_FILE:
            s.restore_missing()
            return ActionResult(True, "Restoring the file.")
        choices = {
            Action.KEEP_MINE: Resolution.KEEP_MINE,
            Action.TAKE_THEIRS: Resolution.KEEP_THEIRS,
            Action.KEEP_BOTH: Resolution.KEEP_BOTH,
        }
        if s.conflict is not None and action in choices:
            s.resolve_conflict(choices[action])
            return ActionResult(True, "Applied your choice.")
        compare = diagnosis.compare
        if action in (Action.KEEP_MINE, Action.RESTORE_VERSION) and compare is not None:
            text = self._side_text(compare.left, compare.left_ref)
            if text is None:
                return ActionResult(False, "That version is no longer available.")
            base = self._restore_base(diagnosis)
            current = s.buffer_text()
            if base is not None and current != base:
                merged = merge3(base, text, current)
                if not merged.clean or merged.text is None:
                    return ActionResult(
                        False,
                        "Your change overlaps newer edits, so it cannot be added back automatically. "
                        "Use Compare to copy it across.",
                        self.explain(diagnosis),
                    )
                text = merged.text
            ok = s.restore_text(text, "keep_mine" if action is Action.KEEP_MINE else "restore_version")
            if ok:
                self._drop_sticky(diagnosis)
                self.evaluate()
            return ActionResult(
                ok, "Restored; it will be saved normally." if ok else "The note is read-only."
            )
        if action is Action.TAKE_THEIRS:
            self._drop_sticky(diagnosis)
            self.evaluate()
            return ActionResult(True, "Keeping the version on disk.")
        return ActionResult(False, "That action does not apply to this situation.")


_LABELS = {
    Side.BUFFER: "your text",
    Side.DISK: "file on disk",
    Side.BASE: "last saved",
    Side.THEIRS: "other version",
    Side.LEDGER_ENTRY: "previous version",
    Side.OUR_LAST_WRITE: "your last save",
    Side.HISTORY_SNAPSHOT: "history",
    Side.DRAFT: "unsaved draft",
    Side.CONFLICT_SIBLING: "conflict copy",
}


_QUIET_DIAGNOSIS = diagnose(NoteContext(), OneDriveModel()).primary


type _Key = tuple[Code, str | None, str | None]


def _key(diagnosis: Diagnosis) -> _Key:
    compare = diagnosis.compare
    if compare is None:
        return (diagnosis.code, None, None)
    return (diagnosis.code, compare.left_ref, compare.right_ref)


def _stat_of(stamp: FileStamp) -> StatInfo:
    return StatInfo(stamp.size, stamp.mtime_ns, stamp.attributes, stamp.reparse_tag, stamp.file_id)
