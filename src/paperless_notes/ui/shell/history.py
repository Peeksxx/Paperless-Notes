"""Qt-free bookkeeping for tabs and navigation: recency order, closed tabs, pins, back and forward."""

from __future__ import annotations

from dataclasses import dataclass, field

from paperless_notes.core import pathid
from paperless_notes.core.workspace import TabState


@dataclass
class TabHistory:
    limit: int = 20
    _mru: list[str] = field(default_factory=list)
    _closed: list[TabState] = field(default_factory=list)

    def activated(self, path: str) -> None:
        key = pathid.identity(path)
        self._mru = [key, *(k for k in self._mru if k != key)]

    def removed(self, state: TabState) -> None:
        key = pathid.identity(state.path)
        self._mru = [k for k in self._mru if k != key]
        self._closed = [s for s in self._closed if pathid.identity(s.path) != key]
        self._closed.append(state)
        del self._closed[: -self.limit]

    def reopen(self) -> TabState | None:
        return self._closed.pop() if self._closed else None

    def closed(self) -> list[TabState]:
        return list(reversed(self._closed))

    def mru(self) -> list[str]:
        return list(self._mru)

    def step(self, open_paths: list[str], depth: int) -> str | None:
        """The open tab ``depth`` places back in recency order (1 is the previously used tab)."""
        keys = {pathid.identity(p): p for p in open_paths}
        ordered = [keys[k] for k in self._mru if k in keys]
        ordered += [p for p in open_paths if p not in ordered]
        if len(ordered) < 2:
            return None
        return ordered[depth % len(ordered)]


@dataclass
class Navigation:
    limit: int = 100
    _back: list[str] = field(default_factory=list)
    _forward: list[str] = field(default_factory=list)
    _current: str | None = None

    @property
    def current(self) -> str | None:
        return self._current

    def visit(self, path: str) -> None:
        if self._current is not None and pathid.same_path(self._current, path):
            return
        if self._current is not None:
            self._back.append(self._current)
            del self._back[: -self.limit]
        self._current = path
        self._forward.clear()

    def can_go_back(self) -> bool:
        return bool(self._back)

    def can_go_forward(self) -> bool:
        return bool(self._forward)

    def back(self) -> str | None:
        if not self._back:
            return None
        if self._current is not None:
            self._forward.append(self._current)
        self._current = self._back.pop()
        return self._current

    def forward(self) -> str | None:
        if not self._forward:
            return None
        if self._current is not None:
            self._back.append(self._current)
        self._current = self._forward.pop()
        return self._current

    def forget(self, path: str) -> None:
        key = pathid.identity(path)
        self._back = [p for p in self._back if pathid.identity(p) != key]
        self._forward = [p for p in self._forward if pathid.identity(p) != key]
        if self._current is not None and pathid.identity(self._current) == key:
            self._current = self._back.pop() if self._back else None

    def rename(self, old: str, new: str) -> None:
        key = pathid.identity(old)

        def swap(p: str) -> str:
            return new if pathid.identity(p) == key else p

        self._back = [swap(p) for p in self._back]
        self._forward = [swap(p) for p in self._forward]
        if self._current is not None:
            self._current = swap(self._current)
