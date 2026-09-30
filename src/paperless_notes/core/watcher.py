"""Routes folder notifications and periodic polls to open sessions.

OneDrive replaces files by renaming a downloaded copy over them, so the folder is watched as well as the
file, and the file watch is re-armed after each replacement. Polling is a cheap stat and becomes coarse
when the window is unfocused or minimised.
"""

from __future__ import annotations

import logging
import ntpath
from enum import Enum
from typing import Protocol

from PySide6.QtCore import QFileSystemWatcher, QObject, Qt, QTimer

from paperless_notes.core import pathid

logger = logging.getLogger(__name__)


class Activity(Enum):
    FOCUSED = "focused"
    UNFOCUSED = "unfocused"
    MINIMIZED = "minimized"


class Watchable(Protocol):
    @property
    def path(self) -> str: ...

    def notify_fs_event(self) -> None: ...

    def poll(self) -> None: ...

    def check_now(self) -> None: ...


class ChangeMonitor(QObject):
    def __init__(self, focused_s: float = 5, unfocused_s: float = 30, minimized_s: float = 60) -> None:
        super().__init__()
        self._intervals = {
            Activity.FOCUSED: focused_s,
            Activity.UNFOCUSED: unfocused_s,
            Activity.MINIMIZED: minimized_s,
        }
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_folder)
        self._watcher.fileChanged.connect(self._on_file)
        self._items: list[Watchable] = []
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._timer.timeout.connect(self.poll_all)
        self._activity = Activity.FOCUSED
        self.set_activity(Activity.FOCUSED)

    @property
    def activity(self) -> Activity:
        return self._activity

    @property
    def interval_s(self) -> float:
        return self._intervals[self._activity]

    def set_activity(self, activity: Activity) -> None:
        self._activity = activity
        self._timer.start(round(self._intervals[activity] * 1000))

    def add(self, item: Watchable) -> None:
        if item in self._items:
            return
        self._items.append(item)
        self._arm(item.path)

    def remove(self, item: Watchable) -> None:
        if item in self._items:
            self._items.remove(item)
        self._rebuild()

    def path_changed(self, item: Watchable) -> None:
        self._rebuild()

    def watched_paths(self) -> list[str]:
        return self._watcher.files() + self._watcher.directories()

    def poll_all(self) -> None:
        for item in list(self._items):
            item.poll()

    def check_all(self) -> None:
        for item in list(self._items):
            item.check_now()

    def _arm(self, path: str) -> None:
        folder = ntpath.dirname(path)
        wanted = [p for p in (folder, path) if p not in self.watched_paths()]
        if wanted:
            failed = self._watcher.addPaths(wanted)
            if failed:
                logger.debug("Could not watch %d path(s); polling covers them", len(failed))

    def _rebuild(self) -> None:
        current = self.watched_paths()
        if current:
            self._watcher.removePaths(current)
        for item in self._items:
            self._arm(item.path)

    def _on_folder(self, folder: str) -> None:
        for item in list(self._items):
            if pathid.same_path(ntpath.dirname(item.path), folder):
                item.notify_fs_event()
                self._arm(item.path)

    def _on_file(self, path: str) -> None:
        for item in list(self._items):
            if pathid.same_path(item.path, path):
                item.notify_fs_event()
                self._arm(item.path)
