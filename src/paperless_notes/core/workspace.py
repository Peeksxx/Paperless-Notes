"""Open tabs and window placement for this PC.

Restore is lazy: the UI creates a tab per entry and opens the note only when the tab is first shown.
Tabs whose file is missing (for example not yet synced) are kept until the user closes them.
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from paperless_notes.core.jsonstore import read_json, write_json
from paperless_notes.core.runtime import Scheduler, TimerHandle

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_TABS = 500
MAX_PATH_CHARS = 32_767
MAX_BLOB_CHARS = 64 * 1024


@dataclass(frozen=True, slots=True)
class TabState:
    path: str
    cursor: int = 0
    scroll: int = 0
    pinned: bool = False


@dataclass(frozen=True, slots=True)
class WindowState:
    geometry: str = ""
    state: str = ""
    maximized: bool = False


@dataclass(frozen=True, slots=True)
class Workspace:
    """Tabs, the active tab, window placement, and whether the second pane was open with which note."""

    tabs: tuple[TabState, ...] = ()
    active: int = -1
    window: WindowState = field(default_factory=WindowState)
    split_open: bool = False
    split_path: str = ""

    def partition(self, exists: Callable[[str], bool]) -> tuple[list[TabState], list[TabState]]:
        available = [t for t in self.tabs if exists(t.path)]
        missing = [t for t in self.tabs if not exists(t.path)]
        return available, missing


def _int(value: Any, low: int, high: int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
        return value
    return None


def _blob(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_BLOB_CHARS:
        return ""
    try:
        base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return ""
    return value


def parse_workspace(data: dict[str, Any] | None) -> Workspace:
    if data is None or data.get("schema_version") != SCHEMA_VERSION:
        return Workspace()
    tabs: list[TabState] = []
    raw_tabs = data.get("tabs")
    for raw in raw_tabs[:MAX_TABS] if isinstance(raw_tabs, list) else []:
        if not isinstance(raw, dict):
            continue
        path = raw.get("path")
        if not isinstance(path, str) or not 0 < len(path) <= MAX_PATH_CHARS or "\x00" in path:
            continue
        tabs.append(
            TabState(
                path,
                _int(raw.get("cursor"), 0, 2**31) or 0,
                _int(raw.get("scroll"), 0, 2**31) or 0,
                raw.get("pinned") is True,
            )
        )
    active = _int(data.get("active"), -1, len(tabs) - 1)
    raw_window = data.get("window")
    window = WindowState()
    if isinstance(raw_window, dict):
        window = WindowState(
            _blob(raw_window.get("geometry")),
            _blob(raw_window.get("state")),
            raw_window.get("maximized") is True,
        )
    raw_split = data.get("split")
    split_open = False
    split_path = ""
    if isinstance(raw_split, dict) and raw_split.get("open") is True:
        split_open = True
        path = raw_split.get("path")
        if isinstance(path, str) and 0 < len(path) <= MAX_PATH_CHARS and "\x00" not in path:
            split_path = path
    return Workspace(tuple(tabs), -1 if active is None else active, window, split_open, split_path)


def dump_workspace(ws: Workspace) -> dict[str, Any]:
    return {
        "split": {"open": ws.split_open, "path": ws.split_path},
        "schema_version": SCHEMA_VERSION,
        "tabs": [
            {"path": t.path, "cursor": t.cursor, "scroll": t.scroll, "pinned": t.pinned} for t in ws.tabs
        ],
        "active": ws.active,
        "window": {
            "geometry": ws.window.geometry,
            "state": ws.window.state,
            "maximized": ws.window.maximized,
        },
    }


class WorkspaceStore:
    """Debounced, atomic persistence. Writes are suspended while a session is being restored."""

    def __init__(self, path: Path, scheduler: Scheduler | None = None, delay_s: float = 1.0) -> None:
        self._path = path
        self._scheduler = scheduler
        self._delay = delay_s
        self._pending: Workspace | None = None
        self._timer: TimerHandle | None = None
        self._suspended = 0
        self.writes = 0

    def load(self) -> Workspace:
        return parse_workspace(read_json(self._path))

    def save_now(self, ws: Workspace) -> None:
        self._pending = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        write_json(self._path, dump_workspace(ws))
        self.writes += 1

    def schedule(self, ws: Workspace) -> None:
        self._pending = ws
        if self._suspended or self._scheduler is None:
            return
        if self._timer is not None:
            self._timer.cancel()
        self._timer = self._scheduler.call_later(self._delay, self.flush, coarse=True)

    def flush(self) -> None:
        self._timer = None
        if self._pending is not None and not self._suspended:
            try:
                self.save_now(self._pending)
            except OSError as exc:
                logger.warning("Saving the workspace failed: %s", exc)

    @contextmanager
    def suspended(self) -> Iterator[None]:
        self._suspended += 1
        try:
            yield
        finally:
            self._suspended -= 1
            if not self._suspended and self._pending is not None:
                self.schedule(self._pending)


def without_tab(ws: Workspace, index: int) -> Workspace:
    tabs = ws.tabs[:index] + ws.tabs[index + 1 :]
    active = ws.active - (1 if ws.active > index else 0)
    return replace(ws, tabs=tabs, active=min(active, len(tabs) - 1))
