"""The draft journal for one session: throttled, ordered, off the UI thread."""

from __future__ import annotations

import logging
import ntpath
from collections.abc import Callable

from paperless_notes.core.drafts import Draft, DraftStore
from paperless_notes.core.runtime import IOExecutor, Outcome

logger = logging.getLogger(__name__)

type Snapshot = Callable[[], tuple[str, str, str]]


class DraftJournal:
    """Writes and deletes run one at a time; a newer request replaces a queued one."""

    def __init__(
        self,
        store: DraftStore | None,
        executor: IOExecutor,
        snapshot: Snapshot,
        is_dirty: Callable[[], bool],
        wall_time: Callable[[], float],
    ) -> None:
        self._store = store
        self._executor = executor
        self._snapshot = snapshot
        self._is_dirty = is_dirty
        self._wall_time = wall_time
        self._busy = False
        self._next: str | None = None

    def queue(self, action: str) -> None:
        store = self._store
        if store is None:
            return
        if self._busy:
            self._next = action
            return
        self._busy = True
        path, text, base_sha = self._snapshot()
        if action == "write":
            when = self._wall_time()

            def job() -> None:
                store.write(path, text, base_sha, when)

        else:

            def job() -> None:
                store.delete(path)

        self._executor.submit(job, self._done, "draft")

    def _done(self, out: Outcome[None]) -> None:
        self._busy = False
        if out.error is not None:
            logger.error("Draft journal failed: %s", out.error)
        action = self._next
        self._next = None
        if action == "write" and not self._is_dirty():
            action = "delete"
        if action is not None:
            self.queue(action)

    def write_blocking(self) -> None:
        if self._store is None:
            return
        path, text, base_sha = self._snapshot()
        try:
            self._store.write(path, text, base_sha, self._wall_time())
        except OSError as exc:
            logger.error("Writing the draft for %s failed: %s", ntpath.basename(path), exc)

    def offer(self, disk_text: str, on_draft: Callable[[Draft], None]) -> None:
        """Report a journaled draft that differs from the file; drop one that matches it."""
        store = self._store
        if store is None:
            return
        path = self._snapshot()[0]

        def job() -> Draft | None:
            return store.load(path)

        def done(out: Outcome[Draft | None]) -> None:
            if out.error is not None:
                logger.warning("Reading draft failed: %s", out.error)
                return
            draft = out.value
            if draft is not None and draft.text != disk_text:
                on_draft(draft)
            elif draft is not None:
                self.queue("delete")

        self._executor.submit(job, done, "draft")

    def load(self) -> Draft | None:
        if self._store is None:
            return None
        try:
            return self._store.load(self._snapshot()[0])
        except OSError as exc:
            logger.warning("Reading draft failed: %s", exc)
            return None
