"""Evidence about a note's file: cloud state, conflict copies, file identity and timestamps.

Every probe is cheap and bounded. Evidence is never proof: the in-sync bit in particular is a hypothesis
to calibrate (ADR-0006), exposed with a confidence rather than as a fact.
"""

from __future__ import annotations

import ctypes
import logging
import ntpath
import re
from collections.abc import Iterable
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from paperless_notes.core import onedrive
from paperless_notes.core.fsops import FileStamp, FileSystem, StatInfo

logger = logging.getLogger(__name__)

SIBLING_SCAN_LIMIT = 5_000
CF_PLACEHOLDER = 0x1
CF_IN_SYNC = 0x8
CF_PARTIAL = 0x10
CF_INVALID = 0xFFFFFFFF


class CloudState(Enum):
    NOT_IN_SYNC_ROOT = "not_in_sync_root"
    HYDRATED = "hydrated"
    CLOUD_ONLY = "cloud_only"
    PINNED = "pinned"
    UNPINNED = "unpinned"
    UNKNOWN = "unknown"


class MtimeRelation(Enum):
    SAME = "same"
    FORWARD = "forward"
    BACKWARD = "backward"
    FUTURE = "future"
    REUSED = "reused"


class IdentityChange(Enum):
    NONE = "none"
    REPLACED_SAME_CONTENT = "replaced_same_content"
    NEW_VERSION = "new_version"
    CHANGED_IN_PLACE = "changed_in_place"
    UNKNOWN = "unknown"


def cloud_state(st: StatInfo | None, in_root: bool) -> CloudState:
    if st is None:
        return CloudState.UNKNOWN
    if not in_root and not onedrive.is_cloud_managed(st.attributes, st.reparse_tag):
        return CloudState.NOT_IN_SYNC_ROOT
    if onedrive.needs_hydration(st.attributes):
        return CloudState.CLOUD_ONLY
    if st.attributes & onedrive.FILE_ATTRIBUTE_PINNED:
        return CloudState.PINNED
    if st.attributes & onedrive.FILE_ATTRIBUTE_UNPINNED:
        return CloudState.UNPINNED
    return CloudState.HYDRATED


class InSyncReader(Protocol):
    def __call__(self, path: str, st: StatInfo) -> bool | None:
        """True or False when the placeholder reports its sync state; None when it cannot."""
        ...


class PlaceholderApi:
    """``CfGetPlaceholderStateFromAttributeTag`` from cldapi.dll; reads no file, only attributes and tag."""

    def __init__(self) -> None:
        self._function: Any = None
        self._tried = False

    def _load(self) -> Any:
        if not self._tried:
            self._tried = True
            try:
                function = ctypes.WinDLL("cldapi").CfGetPlaceholderStateFromAttributeTag
                function.argtypes = [wintypes.DWORD, wintypes.DWORD]
                function.restype = wintypes.DWORD
                self._function = function
            except (OSError, AttributeError) as exc:
                logger.info("Cloud files API unavailable: %s", exc)
        return self._function

    def state(self, attributes: int, reparse_tag: int) -> int | None:
        function = self._load()
        if function is None:
            return None
        try:
            value = int(function(attributes & 0xFFFFFFFF, reparse_tag & 0xFFFFFFFF))
        except (OSError, ctypes.ArgumentError) as exc:
            logger.info("Placeholder state query failed: %s", exc)
            return None
        return None if value == CF_INVALID else value

    def __call__(self, path: str, st: StatInfo) -> bool | None:
        if not st.attributes & onedrive.FILE_ATTRIBUTE_REPARSE_POINT:
            return None
        flags = self.state(st.attributes, st.reparse_tag)
        if flags is None or not flags & CF_PLACEHOLDER:
            return None
        return bool(flags & CF_IN_SYNC)


@dataclass(frozen=True, slots=True)
class SiblingReport:
    onedrive: tuple[str, ...] = ()
    windows: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted({*self.onedrive, *self.windows}))


def windows_copies(path: str, names: Iterable[str]) -> list[str]:
    """``Name (1).md`` style copies made by Explorer or by apps saving a duplicate."""
    stem, ext = ntpath.splitext(ntpath.basename(path))
    pattern = re.compile(f"(?i:{re.escape(stem)}) \\(\\d{{1,3}}\\)(?i:{re.escape(ext)})\\Z")
    return sorted(n for n in names if pattern.match(n))


def probe_siblings(fs: FileSystem, path: str, limit: int = SIBLING_SCAN_LIMIT) -> SiblingReport:
    names = fs.list_dir(ntpath.dirname(path), limit + 1)
    truncated = len(names) > limit
    names = names[:limit]
    return SiblingReport(
        tuple(onedrive.find_conflict_siblings(path, names)), tuple(windows_copies(path, names)), truncated
    )


def mtime_relation(
    previous_ns: int | None, current_ns: int, now_wall_s: float, tolerance_s: float, reused: bool = False
) -> MtimeRelation:
    """Timestamp evidence only; it never orders versions from different machines."""
    if current_ns > int((now_wall_s + tolerance_s) * 1e9):
        return MtimeRelation.FUTURE
    if previous_ns is None or current_ns == previous_ns:
        return MtimeRelation.SAME
    if reused:
        return MtimeRelation.REUSED
    return MtimeRelation.BACKWARD if current_ns < previous_ns else MtimeRelation.FORWARD


def identity_change(previous: FileStamp | None, st: StatInfo, sha: str | None) -> IdentityChange:
    if previous is None or sha is None:
        return IdentityChange.UNKNOWN
    id_changed = bool(previous.file_id and st.file_id and previous.file_id != st.file_id)
    if sha == previous.sha256:
        return IdentityChange.REPLACED_SAME_CONTENT if id_changed else IdentityChange.NONE
    return IdentityChange.NEW_VERSION if id_changed else IdentityChange.CHANGED_IN_PLACE
