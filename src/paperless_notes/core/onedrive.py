"""OneDrive awareness: sync roots, cloud placeholders and conflict-copy heuristics.

Assumptions (see ADR-0002): OneDrive exposes its roots through ``OneDrive*`` environment variables,
cloud-only files carry the RECALL_ON_* / OFFLINE attributes and a cloud reparse tag, the client
replaces files by renaming a downloaded copy over them, and a copy that lost a conflict is renamed to
``<stem>-<COMPUTERNAME>.<ext>`` (optionally with a ``-<n>`` suffix).
"""

from __future__ import annotations

import ntpath
import re
from collections.abc import Iterable, Mapping

from paperless_notes.core import pathid

FILE_ATTRIBUTE_READONLY = 0x1
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_PINNED = 0x80000
FILE_ATTRIBUTE_UNPINNED = 0x100000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000

_HYDRATION_FLAGS = (
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_OFFLINE
)
_CLOUD_TAG_MASK = 0xFFFF0FFF
_CLOUD_TAG = 0x9000001A

TEMP_PREFIX = "~$"


def roots_from_env(env: Mapping[str, str]) -> list[str]:
    roots: list[str] = []
    for key, value in env.items():
        candidate = value.strip()
        if (
            key.casefold().startswith("onedrive")
            and candidate
            and not any(pathid.same_path(candidate, r) for r in roots)
        ):
            roots.append(candidate)
    return roots


def in_sync_root(path: pathid.StrPath, roots: Iterable[str]) -> bool:
    return any(pathid.is_within(path, r) for r in roots)


def needs_hydration(attributes: int) -> bool:
    return bool(attributes & _HYDRATION_FLAGS)


def is_cloud_reparse_tag(tag: int) -> bool:
    return (tag & _CLOUD_TAG_MASK) == _CLOUD_TAG


def is_cloud_managed(attributes: int, reparse_tag: int) -> bool:
    return needs_hydration(attributes) or (
        bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT) and is_cloud_reparse_tag(reparse_tag)
    )


def is_temp_artifact(name: str) -> bool:
    """Our own in-flight temp files; OneDrive does not sync names that start with ``~$``."""
    return name.startswith(TEMP_PREFIX)


def find_conflict_siblings(path: pathid.StrPath, names: Iterable[str]) -> list[str]:
    """Names in the same folder that look like OneDrive conflict copies of ``path``. Advisory only."""
    base = ntpath.basename(pathid.normalize(path))
    stem, ext = ntpath.splitext(base)
    pattern = re.compile(
        f"(?i:{re.escape(stem)})-(?:[A-Z0-9][A-Z0-9-]{{0,14}}(?:-\\d{{1,3}})?|\\d{{1,3}})(?i:{re.escape(ext)})\\Z"
    )
    base_id = base.casefold()
    return sorted(n for n in names if n.casefold() != base_id and pattern.match(n))
