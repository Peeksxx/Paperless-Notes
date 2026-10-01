"""Time and background I/O abstractions. Qt implementations here; the simulation provides virtual ones."""

from __future__ import annotations

import gc
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QObject,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
    Slot,
)

logger = logging.getLogger(__name__)

IO_THREADS = 2


class TimerHandle(Protocol):
    def cancel(self) -> None: ...


class Scheduler(Protocol):
    def now(self) -> float:
        """Monotonic seconds."""
        ...

    def wall_time(self) -> float:
        """Seconds since the epoch, for labels only; never used to order events across machines."""
        ...

    def call_later(self, delay_s: float, fn: Callable[[], None], coarse: bool = False) -> TimerHandle: ...


@dataclass(frozen=True, slots=True)
class Outcome[T]:
    value: T | None = None
    error: Exception | None = None

    def unwrap(self) -> T:
        if self.error is not None:
            raise self.error
        return self.value  # type: ignore[return-value]


class IOExecutor(Protocol):
    def submit[T](
        self, job: Callable[[], T], done: Callable[[Outcome[T]], None], kind: str = "io"
    ) -> None: ...

    def drain(self, timeout_s: float) -> bool:
        """Wait for queued jobs and deliver their completions now (used at shutdown)."""
        ...


def run_job[T](job: Callable[[], T]) -> Outcome[T]:
    try:
        return Outcome(value=job())
    except Exception as exc:  # noqa: BLE001 - handed to the caller as Outcome.error, never dropped
        return Outcome(error=exc)


class SerialQueue:
    """Runs jobs on the executor one at a time, in submission order. Failures are logged, not raised."""

    def __init__(self, executor: IOExecutor, kind: str) -> None:
        self._executor = executor
        self._kind = kind
        self._jobs: deque[Callable[[], None]] = deque()
        self._busy = False

    @property
    def idle(self) -> bool:
        return not self._busy and not self._jobs

    def submit(self, job: Callable[[], None]) -> None:
        self._jobs.append(job)
        if not self._busy:
            self._next()

    def _next(self) -> None:
        if not self._jobs:
            self._busy = False
            return
        self._busy = True
        self._executor.submit(self._jobs.popleft(), self._done, self._kind)

    def _done(self, outcome: Outcome[None]) -> None:
        if outcome.error is not None:
            logger.warning("Background %s job failed: %s", self._kind, outcome.error)
        self._next()


class _QtTimerHandle:
    """Cancelling is idempotent and safe after the timer fired: a fired timer is already scheduled for
    deletion, so the handle must never touch it again."""

    __slots__ = ("_owner", "_timer")

    def __init__(self, owner: QtScheduler, timer: QTimer) -> None:
        self._owner = owner
        self._timer: QTimer | None = timer

    def cancel(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            self._owner._release(timer)


class QtScheduler:
    def __init__(self) -> None:
        self._live: set[QTimer] = set()

    def now(self) -> float:
        return time.monotonic()

    def wall_time(self) -> float:
        return time.time()

    def call_later(self, delay_s: float, fn: Callable[[], None], coarse: bool = False) -> TimerHandle:
        timer = QTimer()
        timer.setSingleShot(True)
        timer.setTimerType(Qt.TimerType.CoarseTimer if coarse else Qt.TimerType.PreciseTimer)

        def fire() -> None:
            self._release(timer)
            fn()

        timer.timeout.connect(fire)
        self._live.add(timer)
        timer.start(max(0, round(delay_s * 1000)))
        return _QtTimerHandle(self, timer)

    def pending(self) -> int:
        return len(self._live)

    def _release(self, timer: QTimer) -> None:
        if timer in self._live:
            self._live.discard(timer)
            timer.stop()
            timer.deleteLater()


class _Job(QRunnable):
    def __init__(self, fn: Callable[[], None]) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        self._fn()


class QtIOExecutor(QObject):
    """One small shared pool. Completion callbacks run on the thread that owns this object.

    The job, its callback and its outcome travel in one list that the owner thread empties, so the last
    reference to anything they hold (a session, its document) is always dropped on the owner thread: a
    Qt object whose last Python reference goes away on a worker thread is never destroyed (measured).
    """

    _deliver = Signal(object)

    def __init__(self, threads: int = IO_THREADS) -> None:
        super().__init__()
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(threads)
        self._pool.setExpiryTimeout(30_000)
        self._deliver.connect(self._on_deliver, Qt.ConnectionType.QueuedConnection)

    def submit[T](self, job: Callable[[], T], done: Callable[[Outcome[T]], None], kind: str = "io") -> None:
        box: list[Any] = [job, done]

        def work() -> None:
            box.append(run_job(box[0]))
            self._deliver.emit(box)

        self._pool.start(_Job(work))

    @Slot(object)
    def _on_deliver(self, box: list[Any]) -> None:
        _job, done, outcome = box
        box.clear()
        done(outcome)

    def drain(self, timeout_s: float) -> bool:
        finished = self._pool.waitForDone(round(timeout_s * 1000))
        QCoreApplication.sendPostedEvents(self, QEvent.Type.MetaCall)
        return finished


_holders = 0
_restore_automatic = True


def _hold_collection() -> None:
    global _holders, _restore_automatic
    if _holders == 0:
        _restore_automatic = gc.isenabled()
        gc.disable()
    _holders += 1


def _release_collection() -> None:
    global _holders
    _holders = max(0, _holders - 1)
    if _holders == 0 and _restore_automatic:
        gc.enable()


class MainThreadCollector(QObject):
    """Runs Python's cycle collector only on the thread that owns this object (the GUI thread) while it
    exists.

    Automatic collection runs on whichever thread happens to allocate. A collection on an I/O worker can
    free Qt objects there, which crashes the process; the spelling word list (loaded on a worker at start)
    made that likely. While any collector exists, automatic collection is off and each one checks the same
    thresholds on a timer. ``MainWindow`` owns one, so the policy lasts as long as its worker threads;
    automatic collection comes back when the last collector is stopped or destroyed."""

    def __init__(self, interval_ms: int = 500, parent: QObject | None = None) -> None:
        super().__init__(parent)
        held = [True]

        def release() -> None:
            if held[0]:
                held[0] = False
                _release_collection()

        _hold_collection()
        self._release = release
        self.destroyed.connect(lambda *_args: release())
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.check)
        self._timer.start()

    def check(self) -> int:
        """Collect the oldest generation whose count passed its threshold (younger ones are included);
        returns the number of unreachable objects found."""
        counts = gc.get_count()
        thresholds = gc.get_threshold()
        for generation in (2, 1, 0):
            if thresholds[generation] and counts[generation] > thresholds[generation]:
                return gc.collect(generation)
        return 0

    def stop(self) -> None:
        self._timer.stop()
        self._release()
