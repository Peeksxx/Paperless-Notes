"""Sync oracle: a pure, deterministic classifier of a note's disk state.

``diagnose`` maps one ``NoteContext`` to exactly one primary ``Diagnosis``. It reads no files and no clocks,
so the same context always gives the same answer. Rules are checked in the order of ``RULES`` and the first
that matches wins. Default actions are never destructive.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from enum import Enum, IntEnum, StrEnum
from pathlib import Path
from typing import Any

from paperless_notes.core.jsonstore import read_json
from paperless_notes.core.ledger import Relation
from paperless_notes.core.probes import CloudState, MtimeRelation

logger = logging.getLogger(__name__)


class Severity(IntEnum):
    INFO = 0
    NOTICE = 1
    WARN = 2
    ERROR = 3


class Code(StrEnum):
    UP_TO_DATE = "up_to_date"
    ECHO_OF_OWN_WRITE = "echo_of_own_write"
    REMOTE_UPDATE_CLEAN = "remote_update_clean"
    REMOTE_UPDATE_DIVERGED = "remote_update_diverged"
    ROLLED_BACK_TO_KNOWN_VERSION = "rolled_back_to_known_version"
    OUR_WRITE_REVERTED = "our_write_reverted"
    OUR_WRITE_OVERWRITTEN = "our_write_overwritten"
    CONFLICT_COPY_CREATED = "conflict_copy_created"
    UPLOAD_PENDING = "upload_pending"
    UPLOAD_STALLED = "upload_stalled"
    CLOUD_ONLY_NOT_LOADED = "cloud_only_not_loaded"
    HYDRATION_STALLED = "hydration_stalled"
    REPLACED_SAME_CONTENT = "replaced_same_content"
    TIMESTAMP_ANOMALY = "timestamp_anomaly"
    LOCKED_BY_SYNC_CLIENT = "locked_by_sync_client"
    DELETED_ELSEWHERE = "deleted_elsewhere"
    UNKNOWN_DIVERGENCE = "unknown_divergence"


class Action(StrEnum):
    NONE = "none"
    SHOW_DIFF = "show_diff"
    TAKE_THEIRS = "take_theirs"
    KEEP_MINE = "keep_mine"
    KEEP_BOTH = "keep_both"
    RESTORE_VERSION = "restore_version"
    RESTORE_FILE = "restore_file"
    RETRY_NOW = "retry_now"
    DISMISS = "dismiss"


NON_DESTRUCTIVE = frozenset({Action.NONE, Action.SHOW_DIFF})


class Side(StrEnum):
    BUFFER = "buffer"
    DISK = "disk"
    BASE = "base"
    THEIRS = "theirs"
    LEDGER_ENTRY = "ledger_entry"
    OUR_LAST_WRITE = "our_last_write"
    HISTORY_SNAPSHOT = "history_snapshot"
    DRAFT = "draft"
    CONFLICT_SIBLING = "conflict_sibling"


@dataclass(frozen=True, slots=True)
class Compare:
    left: Side
    right: Side
    left_ref: str | None = None
    right_ref: str | None = None


@dataclass(frozen=True, slots=True)
class Diagnosis:
    code: Code
    severity: Severity
    confidence: float
    title: str
    message: str
    evidence: tuple[str, ...] = ()
    default_action: Action = Action.NONE
    actions: tuple[Action, ...] = ()
    compare: Compare | None = None

    def __post_init__(self) -> None:
        if self.default_action not in NON_DESTRUCTIVE:
            raise ValueError("a diagnosis must never default to a destructive action")


class OwnWrite(Enum):
    NONE = "none"
    WRITTEN = "written"
    VERIFIED = "verified"
    PENDING = "pending"
    UPLOADED = "uploaded"
    OVERDUE = "overdue"


@dataclass(frozen=True, slots=True)
class NoteContext:
    relation: Relation = Relation.SELF
    disk_present: bool = True
    dirty: bool = False
    file_id_changed: bool = False
    cloud: CloudState = CloudState.UNKNOWN
    in_sync: bool | None = None
    mtime: MtimeRelation = MtimeRelation.SAME
    new_siblings: tuple[str, ...] = ()
    own_write: OwnWrite = OwnWrite.NONE
    own_write_verified: bool = False
    sharing_violations: int = 0
    retry_in_s: float | None = None
    hydrating_s: float | None = None
    conflict_active: bool = False
    drops_own_changes: bool = False
    head_sha: str | None = None
    disk_sha: str | None = None
    related_sha: str | None = None


@dataclass(frozen=True, slots=True)
class Verdict:
    primary: Diagnosis
    rule: str
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OneDriveModel:
    settle_schedule_s: tuple[float, ...] = (1.0, 4.0, 15.0, 60.0)
    max_probe_reads: int = 5
    upload_base_s: float = 120.0
    upload_s_per_mb: float = 30.0
    upload_max_s: float = 1800.0
    hydration_stall_s: float = 60.0
    lock_window_s: float = 120.0
    lock_threshold: int = 3
    lock_retry_hint_s: float = 5.0
    future_tolerance_s: float = 300.0
    in_sync_confidence: float = 0.6
    temp_files_sync: bool = False

    def upload_eta_s(self, size_bytes: int) -> float:
        return min(self.upload_max_s, self.upload_base_s + self.upload_s_per_mb * size_bytes / 1_048_576)


CONSTANT_SOURCES: Mapping[str, str] = {
    "settle_schedule_s": "assumption: brief default, replaced by calibration.json",
    "max_probe_reads": "brief: at most 5 reads per save",
    "upload_base_s": "assumption: OneDrive usually uploads within seconds; 2 minutes before calling it late",
    "upload_s_per_mb": "assumption: about 35 KB/s worst-case upstream",
    "upload_max_s": "assumption: 30 minutes cap before reporting a stall",
    "hydration_stall_s": "assumption: a note-sized download should finish within a minute",
    "lock_window_s": "assumption: window for counting sharing violations",
    "lock_threshold": "assumption: three violations in the window look like an uploading client",
    "lock_retry_hint_s": "measured: simulated retries clear holds within seconds",
    "future_tolerance_s": "assumption: clocks on synced PCs differ by at most 5 minutes",
    "in_sync_confidence": "hypothesis: the placeholder in-sync bit means uploaded; calibrate",
    "temp_files_sync": "documented: Microsoft says temporary .tmp files are not synced",
}

_RANGES: Mapping[str, tuple[float, float]] = {
    "max_probe_reads": (1, 5),
    "upload_base_s": (1, 86_400),
    "upload_s_per_mb": (0, 86_400),
    "upload_max_s": (1, 86_400),
    "hydration_stall_s": (1, 86_400),
    "lock_window_s": (1, 3_600),
    "lock_threshold": (1, 100),
    "lock_retry_hint_s": (0.1, 600),
    "future_tolerance_s": (0, 86_400),
    "in_sync_confidence": (0, 1),
}


def load_model(path: Path | None) -> OneDriveModel:
    """Defaults, overridden by valid values from ``calibration.json``. Never raises."""
    base = OneDriveModel()
    data = read_json(path) if path is not None else None
    if data is None:
        return base
    values = data.get("model", data)
    if not isinstance(values, dict):
        return base
    changes: dict[str, Any] = {}
    for f in fields(OneDriveModel):
        value = values.get(f.name)
        if value is None:
            continue
        if f.name == "settle_schedule_s":
            ok = (
                isinstance(value, list)
                and 1 <= len(value) <= 8
                and all(isinstance(v, int | float) and 0 < v <= 3_600 for v in value)
                and value == sorted(value)
            )
            if ok:
                changes[f.name] = tuple(float(v) for v in value)
        elif f.name == "temp_files_sync":
            if isinstance(value, bool):
                changes[f.name] = value
        else:
            low, high = _RANGES[f.name]
            if isinstance(value, int | float) and not isinstance(value, bool) and low <= value <= high:
                changes[f.name] = int(value) if isinstance(getattr(base, f.name), int) else float(value)
            else:
                logger.warning("Calibration value %s ignored", f.name)
    return replace(base, **changes)


def _d(
    code: Code,
    severity: Severity,
    confidence: float,
    title: str,
    message: str,
    actions: tuple[Action, ...] = (),
    compare: Compare | None = None,
    default: Action = Action.NONE,
) -> Diagnosis:
    return Diagnosis(code, severity, confidence, title, message, (), default, actions, compare)


def _conflict(c: NoteContext, m: OneDriveModel) -> Diagnosis:
    return _d(
        Code.REMOTE_UPDATE_DIVERGED,
        Severity.WARN,
        0.95,
        "Changed in two places",
        "This note changed elsewhere while you were editing, and the changes touch the same text. Nothing "
        "was overwritten. Compare the versions and choose what to keep.",
        (Action.SHOW_DIFF, Action.KEEP_MINE, Action.TAKE_THEIRS, Action.KEEP_BOTH),
        Compare(Side.BUFFER, Side.THEIRS),
        Action.SHOW_DIFF,
    )


def _deleted(c: NoteContext, m: OneDriveModel) -> Diagnosis:
    return _d(
        Code.DELETED_ELSEWHERE,
        Severity.WARN,
        0.9,
        "File deleted or moved",
        "The file was deleted or moved, perhaps on another device. Your text is still here: restore the file "
        "or save it somewhere else.",
        (Action.RESTORE_FILE, Action.SHOW_DIFF, Action.DISMISS),
        Compare(Side.BASE, Side.BUFFER, c.head_sha),
    )


def _hydrating(c: NoteContext, m: OneDriveModel) -> Diagnosis:
    stalled = (c.hydrating_s or 0.0) > m.hydration_stall_s
    if stalled:
        return _d(
            Code.HYDRATION_STALLED,
            Severity.WARN,
            0.7,
            "Download is slow",
            "Downloading this note from OneDrive is taking longer than expected. Check that OneDrive is "
            "running and the PC is online.",
            (Action.RETRY_NOW,),
        )
    return _d(
        Code.CLOUD_ONLY_NOT_LOADED,
        Severity.INFO,
        0.9,
        "Downloading",
        "This note is stored online only and is being downloaded.",
    )


def _locked(c: NoteContext, m: OneDriveModel) -> Diagnosis:
    wait = c.retry_in_s if c.retry_in_s is not None else m.lock_retry_hint_s
    return _d(
        Code.LOCKED_BY_SYNC_CLIENT,
        Severity.NOTICE,
        0.7,
        "File busy",
        f"Another program, probably OneDrive, is holding the file. Saving retries automatically in about "
        f"{max(1, round(wait))} seconds.",
        (Action.RETRY_NOW,),
    )


def _older_version(c: NoteContext, m: OneDriveModel) -> Diagnosis:
    compare = Compare(Side.LEDGER_ENTRY, Side.DISK, c.head_sha, c.disk_sha)
    if c.relation is Relation.KNOWN_ANCESTOR and c.own_write_verified:
        return _d(
            Code.OUR_WRITE_REVERTED,
            Severity.ERROR,
            0.85,
            "Your saved change was undone",
            "A change you saved earlier is no longer in the file; an older version replaced it, perhaps from "
            "another device. Your version is kept. Compare them before choosing.",
            (Action.SHOW_DIFF, Action.KEEP_MINE, Action.TAKE_THEIRS),
            Compare(Side.OUR_LAST_WRITE, Side.DISK, c.head_sha, c.disk_sha),
            Action.SHOW_DIFF,
        )
    side = c.relation is Relation.KNOWN_SIDE_VERSION
    return _d(
        Code.ROLLED_BACK_TO_KNOWN_VERSION,
        Severity.WARN,
        0.85,
        "Older version returned",
        (
            "The file now holds a version that was set aside in an earlier conflict. "
            if side
            else "The file went back to an older version, perhaps one restored in OneDrive or a stale copy. "
        )
        + "Nothing was deleted. Compare it with your latest version.",
        (Action.SHOW_DIFF, Action.RESTORE_VERSION, Action.TAKE_THEIRS, Action.DISMISS),
        compare,
        Action.SHOW_DIFF,
    )


def _overwritten(c: NoteContext, m: OneDriveModel) -> Diagnosis:
    return _d(
        Code.OUR_WRITE_OVERWRITTEN,
        Severity.ERROR if c.own_write_verified else Severity.WARN,
        0.8,
        "Your last change is missing",
        "A newer version arrived from elsewhere that does not include your last saved change, probably "
        "because it was edited from an older copy. Nothing was deleted. Compare them; Keep mine adds your "
        "change back.",
        (Action.SHOW_DIFF, Action.KEEP_MINE, Action.TAKE_THEIRS, Action.DISMISS),
        Compare(Side.OUR_LAST_WRITE, Side.DISK, c.head_sha, c.disk_sha),
        Action.SHOW_DIFF,
    )


def _copy(c: NoteContext, m: OneDriveModel, severity: Severity) -> Diagnosis:
    return _d(
        Code.CONFLICT_COPY_CREATED,
        severity,
        0.6,
        "Conflict copy found",
        "A separate copy of this note appeared, which OneDrive creates when a file changed in two places. "
        "Compare it before deleting it.",
        (Action.SHOW_DIFF, Action.DISMISS),
        Compare(Side.CONFLICT_SIBLING, Side.DISK, c.new_siblings[0] if c.new_siblings else None, c.disk_sha),
        Action.SHOW_DIFF,
    )


def _unknown_new(c: NoteContext, m: OneDriveModel) -> Diagnosis:
    if c.dirty:
        return _d(
            Code.REMOTE_UPDATE_DIVERGED,
            Severity.NOTICE,
            0.9,
            "Changed elsewhere while editing",
            "This note changed elsewhere while you have unsaved edits. Both sets of changes are being "
            "merged; if they overlap you will be asked.",
            (Action.SHOW_DIFF,),
            Compare(Side.BUFFER, Side.DISK, None, c.disk_sha),
            Action.SHOW_DIFF,
        )
    if c.mtime is MtimeRelation.SAME and not c.file_id_changed:
        return _d(
            Code.UNKNOWN_DIVERGENCE,
            Severity.WARN,
            0.5,
            "Unexpected change",
            "The file's content changed but its time and identity did not, which sync clients do not "
            "normally do. Nothing was overwritten. Compare the versions.",
            (Action.SHOW_DIFF, Action.DISMISS),
            Compare(Side.BASE, Side.DISK, c.head_sha, c.disk_sha),
            Action.SHOW_DIFF,
        )
    return _d(
        Code.REMOTE_UPDATE_CLEAN,
        Severity.INFO,
        0.9,
        "Updated from elsewhere",
        "This note was changed on another device or by another app. The new version is shown.",
        (Action.SHOW_DIFF,),
        Compare(Side.LEDGER_ENTRY, Side.DISK, c.head_sha, c.disk_sha),
    )


def _self(c: NoteContext, m: OneDriveModel) -> Diagnosis:
    if c.new_siblings:
        return _copy(c, m, Severity.NOTICE)
    if c.mtime in (MtimeRelation.BACKWARD, MtimeRelation.FUTURE, MtimeRelation.REUSED):
        return _d(
            Code.TIMESTAMP_ANOMALY,
            Severity.NOTICE,
            0.5,
            "Unusual file time",
            "The file's modified time moved backwards, into the future, or matches an older version. The "
            "content is unaffected; this happens when device clocks differ.",
            (Action.DISMISS,),
        )
    if c.file_id_changed:
        return _d(
            Code.REPLACED_SAME_CONTENT,
            Severity.INFO,
            0.9,
            "Replaced by an identical copy",
            "The file was replaced by an identical copy, which is normal during sync.",
        )
    if c.cloud is CloudState.CLOUD_ONLY:
        return _hydrating(replace(c, hydrating_s=0.0), m)
    if c.own_write is OwnWrite.OVERDUE:
        return _d(
            Code.UPLOAD_STALLED,
            Severity.WARN,
            m.in_sync_confidence,
            "Not uploaded yet",
            "Saved on this PC, but OneDrive has not uploaded it in the expected time. Check that OneDrive is "
            "running and the PC is online.",
            (Action.RETRY_NOW, Action.DISMISS),
        )
    if c.own_write is OwnWrite.PENDING:
        return _d(
            Code.UPLOAD_PENDING,
            Severity.INFO,
            m.in_sync_confidence,
            "Waiting to upload",
            "Saved on this PC, waiting for OneDrive to upload it.",
        )
    if c.own_write in (OwnWrite.WRITTEN, OwnWrite.VERIFIED, OwnWrite.UPLOADED):
        return _d(Code.ECHO_OF_OWN_WRITE, Severity.INFO, 0.95, "Saved", "Your latest save is on disk.")
    return _d(Code.UP_TO_DATE, Severity.INFO, 0.95, "Up to date", "This note matches the file on disk.")


type Rule = tuple[
    str, Callable[[NoteContext, OneDriveModel], bool], Callable[[NoteContext, OneDriveModel], Diagnosis]
]

RULES: tuple[Rule, ...] = (
    ("conflict held", lambda c, m: c.conflict_active, _conflict),
    ("file missing", lambda c, m: not c.disk_present, _deleted),
    ("downloading", lambda c, m: c.hydrating_s is not None, _hydrating),
    ("sync client holds the file", lambda c, m: c.sharing_violations >= m.lock_threshold, _locked),
    (
        "older known version on disk",
        lambda c, m: c.relation in (Relation.KNOWN_ANCESTOR, Relation.KNOWN_SIDE_VERSION),
        _older_version,
    ),
    (
        "new version without our last save",
        lambda c, m: c.relation is Relation.UNKNOWN and c.drops_own_changes,
        _overwritten,
    ),
    (
        "new version with a conflict copy",
        lambda c, m: c.relation is Relation.UNKNOWN and bool(c.new_siblings),
        lambda c, m: _copy(c, m, Severity.WARN),
    ),
    ("new version", lambda c, m: c.relation is Relation.UNKNOWN, _unknown_new),
    ("same content", lambda c, m: True, _self),
)


def diagnose(context: NoteContext, model: OneDriveModel) -> Verdict:
    evidence = _evidence(context)
    for name, applies, build in RULES:
        if applies(context, model):
            primary = replace(build(context, model), evidence=evidence)
            return Verdict(primary, name, evidence)
    raise AssertionError("the last rule always applies")


def _evidence(c: NoteContext) -> tuple[str, ...]:
    items = [f"content: {c.relation.value}"]
    if c.file_id_changed:
        items.append("file identity changed (replaced by rename)")
    if c.mtime is not MtimeRelation.SAME:
        items.append(f"modified time: {c.mtime.value}")
    if c.cloud is not CloudState.UNKNOWN:
        items.append(f"cloud state: {c.cloud.value}")
    if c.in_sync is not None:
        items.append(f"placeholder in-sync bit: {c.in_sync}")
    if c.own_write is not OwnWrite.NONE:
        items.append(f"last save: {c.own_write.value}")
    if c.sharing_violations:
        items.append(f"sharing violations recently: {c.sharing_violations}")
    if c.new_siblings:
        items.append(f"conflict copies: {len(c.new_siblings)}")
    return tuple(items)
