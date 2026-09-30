"""Local version history (replaces the old backup pile; K1, K2, K4, K8, SEC10).

Layout under ``history/``: one folder per note, named by a hash of the note's normalised path plus a
readable name, holding ``meta.json``, content-addressed ``blobs/<sha256>.z`` (zlib) and one empty marker
``<utc ms>_<reason>_<sha256>.ref`` per snapshot. Listing reads names only; content is loaded on demand.
"""

from __future__ import annotations

import logging
import os
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from paperless_notes.core import pathid
from paperless_notes.core.fsops import digest, write_local_file
from paperless_notes.core.jsonstore import read_json, write_json

logger = logging.getLogger(__name__)

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
_REASON_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz_")


@dataclass(frozen=True, slots=True)
class Snapshot:
    folder: Path
    taken_ms: int
    reason: str
    sha256: str
    marker: str

    @property
    def taken_at(self) -> float:
        return self.taken_ms / 1000


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    keep_all_hours: int = 24
    hourly_days: int = 7
    daily_days: int = 90
    max_total_mb: int = 256


@dataclass(frozen=True, slots=True)
class PruneReport:
    removed_snapshots: int
    removed_blobs: int
    total_bytes: int


def _parse(folder: Path, name: str) -> Snapshot | None:
    if not name.endswith(".ref"):
        return None
    parts = name[:-4].split("_")
    if len(parts) < 3 or not parts[0].isdigit():
        return None
    sha = parts[-1]
    reason = "_".join(parts[1:-1])
    if len(sha) != 64 or not set(reason) <= _REASON_CHARS:
        return None
    return Snapshot(folder, int(parts[0]), reason, sha, name)


class HistoryStore:
    def __init__(
        self,
        root: Path,
        policy: RetentionPolicy | None = None,
        clock: Callable[[], float] = time.time,
        writer: Callable[[str, bytes], None] = write_local_file,
    ) -> None:
        self._root = root
        self._write = writer
        self._policy = policy or RetentionPolicy()
        self._clock = clock
        self._newest: dict[Path, tuple[int, str]] = {}

    def folder_for(self, path: str) -> Path:
        return self._root / f"{pathid.path_key(path)[:16]}-{pathid.readable_name(path)}"

    def snapshot(self, path: str, data: bytes, reason: str) -> None:
        """Record ``data`` unless it equals the newest snapshot of this note."""
        reason = "".join(c for c in reason.lower() if c in _REASON_CHARS) or "snapshot"
        folder = self.folder_for(path)
        sha = digest(data)
        newest = self._newest.get(folder)
        if newest is None:
            existing = self._snapshots(folder)
            newest = (existing[0].taken_ms, existing[0].sha256) if existing else (0, "")
        if newest[1] == sha:
            return
        (folder / "blobs").mkdir(parents=True, exist_ok=True)
        if not (folder / "meta.json").exists():
            write_json(folder / "meta.json", {"v": 1, "path": pathid.normalize(path)})
        blob = folder / "blobs" / f"{sha}.z"
        if not blob.exists():
            self._write(str(blob), zlib.compress(data, 6))
        taken = max(int(self._clock() * 1000), newest[0] + 1)
        self._write(str(folder / f"{taken}_{reason}_{sha}.ref"), b"")
        self._newest[folder] = (taken, sha)

    def snapshots(self, path: str) -> list[Snapshot]:
        """Snapshots of one note, newest first."""
        return self._snapshots(self.folder_for(path))

    def find(self, path: str, sha256: str) -> bytes | None:
        """Content with this hash if any snapshot of the note holds it."""
        blob = self.folder_for(path) / "blobs" / f"{sha256}.z"
        try:
            data = zlib.decompress(blob.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, zlib.error) as exc:
            logger.warning("History blob unreadable: %s", exc)
            return None
        return data if digest(data) == sha256 else None

    def lineage(self, path: str) -> list[tuple[int, str, str, int]]:
        """(taken_ms, sha256, reason, size) oldest first, for rebuilding a damaged ledger."""
        out: list[tuple[int, str, str, int]] = []
        for snap in reversed(self.snapshots(path)):
            data = self.find(path, snap.sha256)
            out.append((snap.taken_ms, snap.sha256, snap.reason, len(data) if data is not None else 0))
        return out

    def notes(self) -> list[tuple[str, Path]]:
        found: list[tuple[str, Path]] = []
        if not self._root.exists():
            return found
        for folder in sorted(p for p in self._root.iterdir() if p.is_dir()):
            meta = read_json(folder / "meta.json")
            if meta is not None and isinstance(meta.get("path"), str):
                found.append((meta["path"], folder))
        return found

    def read(self, snapshot: Snapshot) -> bytes:
        data = zlib.decompress((snapshot.folder / "blobs" / f"{snapshot.sha256}.z").read_bytes())
        if digest(data) != snapshot.sha256:
            raise ValueError("history blob is corrupt")
        return data

    @staticmethod
    def diff(old: str, new: str, old_label: str = "before", new_label: str = "after") -> str:
        import difflib

        return "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True), new.splitlines(keepends=True), old_label, new_label
            )
        )

    def clear(self, path: str) -> None:
        self._remove_folder(self.folder_for(path))

    def clear_all(self) -> None:
        if self._root.exists():
            for folder in [p for p in self._root.iterdir() if p.is_dir()]:
                self._remove_folder(folder)

    def prune(self) -> PruneReport:
        """Apply the time-decay policy to every note, then the total size cap."""
        now_ms = int(self._clock() * 1000)
        removed = 0
        kept: list[Snapshot] = []
        if not self._root.exists():
            return PruneReport(0, 0, 0)
        folders = [p for p in self._root.iterdir() if p.is_dir()]
        for folder in folders:
            snaps = self._snapshots(folder)
            keep = self._select(snaps, now_ms)
            for snap in snaps:
                if snap in keep:
                    kept.append(snap)
                else:
                    self._unlink(folder / snap.marker)
                    removed += 1
        total = self._total_bytes(folders)
        cap = self._policy.max_total_mb * 1024 * 1024
        newest = {s.folder: s for s in sorted(kept, key=lambda s: s.taken_ms)}
        for snap in sorted(kept, key=lambda s: s.taken_ms):
            if total <= cap:
                break
            if newest.get(snap.folder) is snap:
                continue
            self._unlink(snap.folder / snap.marker)
            removed += 1
            total = self._total_bytes(folders) if self._gc(snap.folder) else total
        blobs = sum(self._gc(folder) for folder in folders)
        return PruneReport(removed, blobs, self._total_bytes(folders))

    def _select(self, snaps: list[Snapshot], now_ms: int) -> set[Snapshot]:
        if not snaps:
            return set()
        p = self._policy
        keep = {snaps[0]}
        buckets: set[tuple[str, int]] = set()
        for snap in snaps:
            age = now_ms - snap.taken_ms
            if age <= p.keep_all_hours * HOUR_MS:
                keep.add(snap)
            elif age <= p.hourly_days * DAY_MS:
                bucket = ("h", snap.taken_ms // HOUR_MS)
                if bucket not in buckets:
                    buckets.add(bucket)
                    keep.add(snap)
            elif age <= p.daily_days * DAY_MS:
                bucket = ("d", snap.taken_ms // DAY_MS)
                if bucket not in buckets:
                    buckets.add(bucket)
                    keep.add(snap)
        return keep

    def _snapshots(self, folder: Path) -> list[Snapshot]:
        if not folder.exists():
            return []
        with os.scandir(folder) as entries:
            snaps = [s for s in (_parse(folder, e.name) for e in entries) if s]
        return sorted(snaps, key=lambda s: s.taken_ms, reverse=True)

    def _gc(self, folder: Path) -> int:
        referenced = {s.sha256 for s in self._snapshots(folder)}
        removed = 0
        blobs = folder / "blobs"
        if blobs.exists():
            for blob in blobs.iterdir():
                if blob.name.endswith(".z") and blob.name[:-2] not in referenced:
                    self._unlink(blob)
                    removed += 1
        if not referenced:
            self._remove_folder(folder)
        return removed

    def _total_bytes(self, folders: list[Path]) -> int:
        total = 0
        for folder in folders:
            blobs = folder / "blobs"
            if blobs.exists():
                total += sum(b.stat().st_size for b in blobs.iterdir() if b.is_file())
        return total

    def _unlink(self, path: Path) -> None:
        self._newest.pop(path.parent, None)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("Could not remove history file %s: %s", path.name, exc)

    def _remove_folder(self, folder: Path) -> None:
        self._newest.pop(folder, None)
        if not folder.exists():
            return
        for sub in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if sub.is_file():
                self._unlink(sub)
            else:
                try:
                    sub.rmdir()
                except OSError as exc:
                    logger.warning("Could not remove history folder %s: %s", sub.name, exc)
        try:
            folder.rmdir()
        except OSError as exc:
            logger.warning("Could not remove history folder %s: %s", folder.name, exc)
