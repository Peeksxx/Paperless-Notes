"""Version history: the note's kept versions and its ledger lineage, a scrubber with a live comparison against
the current text, restore through the session, and clearing history together with the sync evidence.

Moving the scrubber only reads; the note changes only after Restore is confirmed.
"""

from __future__ import annotations

import logging
import ntpath
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from paperless_notes.core import textformat
from paperless_notes.core.diffing import DiffModel, diff_lines
from paperless_notes.core.evidence import EvidenceStore
from paperless_notes.core.history import HistoryStore, Snapshot
from paperless_notes.core.ledger import LedgerEntry, Origin
from paperless_notes.core.session import NoteSession
from paperless_notes.ui.shell.dialogs import Prompter
from paperless_notes.ui.sync.diff_view import DiffView
from paperless_notes.ui.theme.tokens import Theme

logger = logging.getLogger(__name__)

REASONS = {
    "opened": "Opened in Paperless Notes",
    "saved": "Saved",
    "before_reload": "Before an update from elsewhere",
    "before_merge": "Your text before a merge",
    "before_keep_theirs": "Your text before keeping the other version",
    "before_keep_mine": "The other version before keeping yours",
    "before_restore_snapshot": "Before restoring an older version",
    "before_restore_version": "Before restoring the previous version",
    "before_restore": "Before a restore",
}
ORIGINS = {
    Origin.INITIAL: "First opened on this PC",
    Origin.LOCAL_SAVE: "Saved in Paperless Notes",
    Origin.EXTERNAL_OBSERVED: "Changed elsewhere",
    Origin.MERGE: "Merged with changes from elsewhere",
    Origin.CONFLICT_KEEP_THEIRS: "Kept the other version",
    Origin.CONFLICT_KEEP_BOTH: "Kept both versions",
    Origin.RESTORE: "Restored",
    Origin.RECOVERED_DRAFT: "Recovered unsaved text",
}
NOT_KEPT = "Recorded by sync, but the text is not kept on this PC, so it cannot be shown."


@dataclass(frozen=True, slots=True)
class Version:
    taken_at: float
    reason: str
    relation: str
    sha: str
    snapshot: Snapshot | None
    current: bool = False


def when(timestamp: float, now: float | None = None) -> str:
    """Plain-language time: 'Today 14:05', 'Yesterday 09:12' or '3 Mar 2026 18:40'."""
    local = time.localtime(timestamp)
    today = time.localtime(now if now is not None else time.time())
    clock = time.strftime("%H:%M", local)
    days = (
        time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))
        - time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))
    ) // 86400
    if days == 0:
        return f"Today {clock}"
    if days == 1:
        return f"Yesterday {clock}"
    return f"{local.tm_mday} {time.strftime('%b %Y', local)} {clock}"


def build_versions(
    snapshots: list[Snapshot], lineage: tuple[LedgerEntry, ...], current_sha: str | None
) -> list[Version]:
    """Oldest first. History snapshots carry text; ledger entries add relationships and missing versions."""
    by_sha = {e.content_sha256: e for e in lineage}
    versions: list[Version] = []
    seen: set[str] = set()
    for snap in sorted(snapshots, key=lambda s: s.taken_ms):
        entry = by_sha.get(snap.sha256)
        relation = ORIGINS.get(entry.origin, "") if entry is not None else "Kept in history on this PC"
        reason = REASONS.get(snap.reason, snap.reason.replace("_", " ").capitalize())
        versions.append(
            Version(snap.taken_at, reason, relation, snap.sha256, snap, snap.sha256 == current_sha)
        )
        seen.add(snap.sha256)
    for entry in lineage:
        if entry.content_sha256 in seen:
            continue
        seen.add(entry.content_sha256)
        versions.append(
            Version(
                entry.wall_time,
                ORIGINS.get(entry.origin, "Recorded"),
                NOT_KEPT,
                entry.content_sha256,
                None,
                entry.content_sha256 == current_sha,
            )
        )
    ordered = sorted(versions, key=lambda v: v.taken_at)
    newest = max((i for i, v in enumerate(ordered) if v.current), default=-1)
    return [replace(v, current=i == newest) for i, v in enumerate(ordered)]


class HistoryDialog(QDialog):
    restored = Signal(str)

    def __init__(
        self,
        session: NoteSession,
        store: HistoryStore | None,
        evidence: EvidenceStore | None,
        theme: Theme,
        prompter: Prompter,
        clock: Callable[[], float] = time.time,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Version history")
        self.resize(1040, 700)
        self.session = session
        self._store = store
        self._evidence = evidence
        self._theme = theme
        self._prompter = prompter
        self._clock = clock
        self._texts: dict[str, str | None] = {}
        self.versions: list[Version] = []
        layout = QVBoxLayout(self)
        s = theme.spacing
        layout.setSpacing(s.md)
        title = QLabel(f"Version history of {ntpath.basename(session.path)}")
        title.setProperty("role", "heading")
        layout.addWidget(title)
        intro = QLabel(
            "Versions are kept on this PC only. Move the slider or pick a version to compare it with your "
            "text now. Nothing changes until you choose Restore."
        )
        intro.setWordWrap(True)
        intro.setProperty("role", "secondary")
        layout.addWidget(intro)
        body = QHBoxLayout()
        body.setSpacing(s.lg)
        self.list = QListWidget()
        self.list.setProperty("role", "plain")
        self.list.setAccessibleName("Versions, oldest first")
        self.list.setMinimumWidth(280)
        self.list.setMaximumWidth(360)
        self.list.setWordWrap(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.currentRowChanged.connect(self._on_row)
        body.addWidget(self.list)
        right = QVBoxLayout()
        right.setSpacing(s.sm)
        scrub_row = QHBoxLayout()
        scrub_label = QLabel("Version")
        scrub_label.setProperty("role", "secondary")
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setAccessibleName("Version scrubber")
        self.slider.setToolTip("Drag, or use the arrow keys, to step through versions")
        self.slider.setPageStep(1)
        self.slider.valueChanged.connect(self._on_slider)
        scrub_label.setBuddy(self.slider)
        self.position = QLabel("")
        self.position.setProperty("role", "muted")
        scrub_row.addWidget(scrub_label)
        scrub_row.addWidget(self.slider, 1)
        scrub_row.addWidget(self.position)
        right.addLayout(scrub_row)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        right.addWidget(self.detail)
        self.diff_host = QVBoxLayout()
        self.diff_view: DiffView | None = None
        right.addLayout(self.diff_host, 1)
        body.addLayout(right, 1)
        layout.addLayout(body, 1)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setProperty("role", "secondary")
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        self.clear_button = QPushButton("Clear history of this note")
        self.clear_button.setProperty("kind", "danger")
        self.clear_button.setToolTip("Delete the kept versions and sync evidence of this note on this PC")
        self.clear_button.clicked.connect(self.clear_note)
        self.clear_all_button = QPushButton("Clear all history")
        self.clear_all_button.setProperty("kind", "quiet")
        self.clear_all_button.setToolTip("Delete kept versions and sync evidence of every note on this PC")
        self.clear_all_button.clicked.connect(self.clear_all)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        self.restore_button = QPushButton("Restore this version")
        self.restore_button.setProperty("kind", "primary")
        self.restore_button.setToolTip("Put this version into the note and save it normally")
        self.restore_button.clicked.connect(self.restore_selected)
        buttons.addWidget(self.clear_button)
        buttons.addWidget(self.clear_all_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        buttons.addWidget(self.restore_button)
        layout.addLayout(buttons)
        self.reload()

    def reload(self) -> None:
        snapshots = self._store.snapshots(self.session.path) if self._store is not None else []
        stamp = self.session.disk_stamp
        self.versions = build_versions(snapshots, self.session.lineage(), stamp.sha256 if stamp else None)
        self._texts.clear()
        self.list.blockSignals(True)
        self.slider.blockSignals(True)
        self.list.clear()
        now = self._clock()
        for version in self.versions:
            label = f"{when(version.taken_at, now)}\n{version.reason}"
            if version.current:
                label += " (on disk now)"
            item = QListWidgetItem(label)
            item.setToolTip(version.relation)
            if version.snapshot is None:
                item.setForeground(self.palette().placeholderText())
            self.list.addItem(item)
        self.slider.setRange(0, max(0, len(self.versions) - 1))
        self.slider.setEnabled(len(self.versions) > 1)
        self.list.blockSignals(False)
        self.slider.blockSignals(False)
        has_history = self._store is not None and bool(snapshots)
        self.clear_button.setEnabled(has_history or self._evidence is not None)
        if self.versions:
            self.select(len(self.versions) - 1)
        else:
            self._show(None)

    def select(self, index: int) -> None:
        index = max(0, min(len(self.versions) - 1, index))
        self.slider.blockSignals(True)
        self.slider.setValue(index)
        self.slider.blockSignals(False)
        self.list.blockSignals(True)
        self.list.setCurrentRow(index)
        self.list.blockSignals(False)
        self._show(self.versions[index] if self.versions else None)

    def _on_row(self, row: int) -> None:
        if row >= 0:
            self.select(row)

    def _on_slider(self, value: int) -> None:
        self.select(value)

    def selected(self) -> Version | None:
        row = self.slider.value()
        return self.versions[row] if 0 <= row < len(self.versions) else None

    def text_of(self, version: Version) -> str | None:
        if version.sha not in self._texts:
            text: str | None = None
            if version.snapshot is not None and self._store is not None:
                try:
                    text = textformat.decode_lossy(self._store.read(version.snapshot))
                except (OSError, ValueError, zlib.error) as exc:
                    logger.info("A history version could not be read: %s", exc)
            self._texts[version.sha] = text
        return self._texts[version.sha]

    def comparison(self, version: Version) -> DiffModel | None:
        text = self.text_of(version)
        if text is None:
            return None
        return diff_lines(text, self.session.buffer_text(), labels=("this version", "your text now"))

    def _show(self, version: Version | None) -> None:
        if self.diff_view is not None:
            self.diff_host.removeWidget(self.diff_view)
            self.diff_view.deleteLater()
            self.diff_view = None
        if version is None:
            self.position.setText("")
            self.detail.setText(
                "No versions are kept for this note yet. Versions appear after saves and updates."
            )
            self.restore_button.setEnabled(False)
            return
        number = self.versions.index(version) + 1
        self.position.setText(f"{number} of {len(self.versions)}")
        self.detail.setText(f"{when(version.taken_at, self._clock())}. {version.reason}. {version.relation}.")
        model = self.comparison(version)
        note = "" if version.snapshot is not None else NOT_KEPT
        self.diff_view = DiffView(model, self._theme, note)
        self.diff_host.addWidget(self.diff_view)
        self.restore_button.setEnabled(
            model is not None
            and not model.identical
            and not self.session.read_only
            and self._store is not None
        )

    def restore_selected(self) -> bool:
        version = self.selected()
        if version is None or version.snapshot is None:
            return False
        model = self.comparison(version)
        change = ""
        if model is not None:
            stats = model.stats
            change = f" Compared with your text now: {stats.added} lines added, {stats.removed} removed."
        if not self._prompter.confirm(
            "Restore this version?",
            f"The note will contain the version from {when(version.taken_at, self._clock())}.{change} Your "
            "current text is kept in history first, and the restored text is saved the normal way.",
            "Restore",
        ):
            return False
        if not self.session.restore_snapshot(version.snapshot):
            self.message.setText("Nothing was restored: the note is read-only or not open for editing.")
            return False
        self.restored.emit(when(version.taken_at, self._clock()))
        self.accept()
        return True

    def clear_note(self) -> bool:
        if not self._prompter.confirm(
            "Clear history of this note?",
            "All kept versions of this note on this PC are deleted, together with the saved sync "
            "comparisons. "
            "Messages about earlier sync changes may no longer be able to show what changed, also after a "
            "restart. The note itself is not changed.",
            "Clear history",
            danger=True,
        ):
            return False
        path = self.session.path
        if self._store is not None:
            self._store.clear(path)
        if self._evidence is not None:
            self._evidence.clear(path)
        self.message.setText("History of this note was cleared.")
        self.reload()
        return True

    def clear_all(self) -> bool:
        if not self._prompter.confirm(
            "Clear all history?",
            "Kept versions and saved sync comparisons of every note on this PC are deleted. Messages about "
            "earlier sync changes may no longer be able to show what changed. Your notes are not changed.",
            "Clear all history",
            danger=True,
        ):
            return False
        if self._store is not None:
            self._store.clear_all()
        if self._evidence is not None:
            self._evidence.clear_all()
        self.message.setText("All history was cleared.")
        self.reload()
        return True
