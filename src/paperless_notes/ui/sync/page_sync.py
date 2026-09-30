"""Connects one note session to its page: the sync line, diagnosis banners, draft recovery and the
'changed elsewhere' marks. Every action goes through the session; nothing here decides for the user.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QMetaObject, QObject, Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget

from paperless_notes.core.diffing import DiffModel, diff_lines
from paperless_notes.core.drafts import Draft
from paperless_notes.core.oracle import Action, Code, Compare, Diagnosis, Side
from paperless_notes.core.session import ConflictInfo, NoteSession, ReloadInfo, SessionState
from paperless_notes.core.syncmonitor import SyncState
from paperless_notes.ui.editor.note_editor import NoteEditor
from paperless_notes.ui.shell.flow import FlowLayout
from paperless_notes.ui.sync.banner import DiagnosisBanner, shows_banner
from paperless_notes.ui.sync.marks import change_marks, model_mapper
from paperless_notes.ui.theme.tokens import Theme

UPLOADED_TIP = (
    "OneDrive reports this file as uploaded. This is OneDrive's own status, not a check of the copy in the "
    "cloud."
)
REPLACING = {
    Action.KEEP_MINE: (
        "Put your version back?",
        "Your version replaces the text in the note and is saved normally. The version it replaces is kept "
        "in history.",
        "Keep my version",
    ),
    Action.TAKE_THEIRS: (
        "Keep the version on disk?",
        "The note keeps the version that is on disk now. Your text is kept in history.",
        "Keep the version on disk",
    ),
    Action.RESTORE_VERSION: (
        "Restore the previous version?",
        "The previous version replaces the text in the note and is saved normally. The current text is kept "
        "in history.",
        "Restore",
    ),
}
IN_CONTEXT_ELSEWHERE = frozenset({"missing", "save_failed"})
_SHORT = {
    SyncState.LOCAL_ONLY: "Saved",
    SyncState.SAVED_HERE: "Saved",
    SyncState.UPLOAD_PENDING: "Uploading",
    SyncState.UPLOADED: "Uploaded",
    SyncState.UPDATED_FROM_ELSEWHERE: "Updated",
    SyncState.NEEDS_REVIEW: "Needs review",
    SyncState.OFFLINE_OR_STALLED: "Stalled",
    SyncState.UNKNOWN: "",
}


def sync_line(session: NoteSession) -> tuple[str, str]:
    """The plain-English line under the title and its style state. UNKNOWN says nothing."""
    state = session.state
    if state is SessionState.LOADING:
        return ("Opening", "busy")
    if state is SessionState.LOAD_FAILED:
        return ("This note could not be opened.", "needs_review")
    if state is SessionState.CONFLICT:
        return ("Changed in two places. Nothing was overwritten; choose what to keep.", "needs_review")
    if state is SessionState.MISSING:
        return ("The file is no longer on disk. Your text is still here.", "needs_review")
    if state is SessionState.SAVE_FAILED:
        return (f"Not saved yet. {session.save_error or 'Trying again.'}", "needs_review")
    if state is SessionState.SAVING:
        return ("Saving", "busy")
    if state is SessionState.DIRTY:
        return ("Edited; saves automatically in a moment", "busy")
    status = session.sync_status
    if status.state is SyncState.UNKNOWN:
        return ("", SyncState.UNKNOWN.value)
    return (status.plain_text, status.state.value)


def short_state(session: NoteSession) -> str:
    """One or two words for the status bar."""
    words = {
        SessionState.LOADING: "Opening",
        SessionState.LOAD_FAILED: "Not opened",
        SessionState.CONFLICT: "Conflict",
        SessionState.MISSING: "Missing",
        SessionState.SAVE_FAILED: "Not saved",
        SessionState.SAVING: "Saving",
        SessionState.DIRTY: "Edited",
    }
    return words.get(session.state) or _SHORT.get(session.sync_status.state, "")


_TROUBLE = frozenset(
    {SessionState.CONFLICT, SessionState.SAVE_FAILED, SessionState.MISSING, SessionState.LOAD_FAILED}
)
_WORKING = frozenset({SessionState.DIRTY, SessionState.SAVING, SessionState.LOADING})


def session_tone(session: NoteSession) -> str:
    """The state tone of one note: attention when the user must look at it, busy while text is unsaved or
    waiting to upload, otherwise ok."""
    sync = session.sync_status.state
    if session.state in _TROUBLE or sync in (SyncState.NEEDS_REVIEW, SyncState.OFFLINE_OR_STALLED):
        return "attention"
    if session.state in _WORKING or sync is SyncState.UPLOAD_PENDING:
        return "busy"
    return "ok"


def summarize(sessions: list[NoteSession]) -> tuple[str, str, str, str]:
    """(tone, words, annotation, detail) describing every open note at once."""
    if not sessions:
        return ("idle", "No notes open", "", "")
    tones = [session_tone(s) for s in sessions]
    detail = "\n".join(f"{s.path.rsplit(chr(92), 1)[-1]}: {short_state(s) or 'Open'}" for s in sessions[:12])
    count = f"{len(sessions)} OPEN"
    trouble = tones.count("attention")
    if trouble:
        return (
            "attention",
            f"{trouble} note{'s' if trouble != 1 else ''} need{'s' if trouble == 1 else ''} attention",
            count,
            detail,
        )
    if any(s.state in _WORKING for s in sessions):
        return ("busy", "Saving changes", count, detail)
    if any(s.sync_status.state is SyncState.UPLOAD_PENDING for s in sessions):
        return ("busy", "Waiting for OneDrive to upload", count, detail)
    if all(s.sync_status.state is SyncState.UPLOADED for s in sessions):
        return ("ok", "All open notes saved and uploaded", count, detail)
    return ("ok", "All open notes saved", count, detail)


@dataclass
class SyncHooks:
    toast: Callable[[str], None]
    open_diff: Callable[[str, DiffModel | None, str], object]
    open_conflict: Callable[[NoteSession, ConflictInfo], object]
    confirm: Callable[[str, str, str, bool], bool]


class DraftBar(QFrame):
    """Unsaved text from an earlier run, offered in context: recover, compare or discard."""

    def __init__(self, draft: Draft, matches_disk: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Banner")
        self.setProperty("severity", "notice" if matches_disk else "warn")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.draft = draft
        layout = QVBoxLayout(self)
        title = QLabel("Unsaved text from an earlier session")
        title.setObjectName("BannerTitle")
        title.setWordWrap(True)
        text = "Paperless Notes kept text that was not saved when the app last closed."
        if not matches_disk:
            text += " The file changed since then, so recovering replaces newer text; compare first."
        body = QLabel(text)
        body.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(body)
        row = FlowLayout()
        self.recover_button = QPushButton("Recover unsaved text")
        self.recover_button.setProperty("kind", "primary" if matches_disk else "")
        self.recover_button.setToolTip("Put the kept text into the note as an ordinary edit")
        self.compare_button = QPushButton("Compare")
        self.compare_button.setToolTip("Show how the kept text differs from the note")
        self.discard_button = QPushButton("Discard")
        self.discard_button.setProperty("kind", "quiet")
        self.discard_button.setToolTip("Delete the kept text; the note stays as it is")
        for button in (self.recover_button, self.compare_button, self.discard_button):
            row.addWidget(button)
        layout.addLayout(row)


class SyncPresenter(QObject):
    changed = Signal()

    def __init__(
        self,
        session: NoteSession,
        editor: NoteEditor,
        line: QLabel,
        banners: QVBoxLayout,
        theme: Theme,
        hooks: SyncHooks,
        notice: QLabel | None = None,
    ) -> None:
        super().__init__()
        self.session = session
        self.editor = editor
        self.line = line
        self.notice = notice or QLabel()
        self.notice.hide()
        self.banners = banners
        self.theme = theme
        self.hooks = hooks
        self.banner: DiagnosisBanner | None = None
        self.draft_bar: DraftBar | None = None
        self._connections: list[QMetaObject.Connection] = [
            session.diagnosis_changed.connect(self._on_diagnosis),
            session.sync_status_changed.connect(self._refresh),
            session.state_changed.connect(self._refresh),
            session.reloaded.connect(self._on_reloaded),
            session.draft_available.connect(self._on_draft),
            session.problem.connect(self._on_problem),
            session.saved.connect(self._clear_notice),
        ]
        self._refresh()
        self._on_diagnosis(session.diagnosis)

    def dispose(self) -> None:
        """Disconnect from the session so a closed page never keeps it or itself alive."""
        for connection in self._connections:
            QObject.disconnect(connection)
        self._connections.clear()
        self._remove_banner()
        self._remove_draft_bar()

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        diagnosis = self.banner.diagnosis if self.banner is not None else None
        self._remove_banner()
        self._on_diagnosis(diagnosis)

    def _refresh(self, *_args: Any) -> None:
        text, state = sync_line(self.session)
        self.line.setText(text)
        self.line.setProperty("state", state)
        self.line.setToolTip(UPLOADED_TIP if state == SyncState.UPLOADED.value else text)
        self.line.setAccessibleName(f"Sync status: {text}" if text else "Sync status")
        style = self.line.style()
        style.unpolish(self.line)
        style.polish(self.line)
        self.changed.emit()

    def _on_diagnosis(self, diagnosis: Diagnosis | None) -> None:
        if not shows_banner(diagnosis) or diagnosis is None:
            self._remove_banner()
            return
        current = self.banner.diagnosis if self.banner is not None else None
        if current == diagnosis:
            return
        self._remove_banner()
        banner = DiagnosisBanner(diagnosis, self.theme)
        banner.action_triggered.connect(self.trigger)
        self.banners.insertWidget(0, banner)
        self.banner = banner
        self.changed.emit()

    def _remove_banner(self) -> None:
        if self.banner is not None:
            self.banners.removeWidget(self.banner)
            self.banner.deleteLater()
            self.banner = None

    def trigger(self, diagnosis: Diagnosis, action: Action) -> bool:
        """Run one action the user clicked. Returns True when the engine reports it done."""
        if action not in diagnosis.actions:
            return False
        session = self.session
        if action is Action.SHOW_DIFF:
            conflict = session.conflict
            if conflict is not None and diagnosis.code is Code.REMOTE_UPDATE_DIVERGED:
                self.hooks.open_conflict(session, conflict)
                return True
            result = session.apply_action(diagnosis, action)
            explanation = result.explanation
            if explanation is not None:
                self.hooks.open_diff(explanation.title, explanation.diff, explanation.text)
            return result.done
        if action in REPLACING:
            title, text, button = REPLACING[action]
            if not self.hooks.confirm(title, text, button, False):
                return False
        result = session.apply_action(diagnosis, action)
        if not result.done:
            if self.banner is not None and self.banner.diagnosis == diagnosis:
                self.banner.show_result(result.message)
            explanation = result.explanation
            if explanation is not None:
                self.hooks.open_diff(explanation.title, explanation.diff, explanation.text)
            return False
        if result.message and action is not Action.DISMISS:
            self.hooks.toast(result.message)
        return True

    def _on_reloaded(self, info: ReloadInfo) -> None:
        if info.kind == "reloaded":
            self.hooks.toast("Updated with changes from elsewhere")
        elif info.kind == "merged":
            self.hooks.toast("Merged changes from elsewhere with your edits")
        self.show_marks(info)

    def show_marks(self, info: ReloadInfo) -> int:
        """Marks from the engine's comparison of the two versions only; none if either is unavailable."""
        if info.kind not in ("reloaded", "merged") or not info.previous_sha or not info.sha:
            return 0
        model = self.session.diff(Compare(Side.LEDGER_ENTRY, Side.DISK, info.previous_sha, info.sha))
        if model is None:
            return 0
        mapper = None
        if info.kind == "merged":
            mapper = model_mapper(self.session.diff(Compare(Side.DISK, Side.BUFFER, info.sha, None)))
            if mapper is None:
                return 0
        marks = change_marks(model, mapper)
        self.editor.set_change_marks(marks)
        return len(marks)

    def _on_draft(self, draft: Draft) -> None:
        self._remove_draft_bar()
        bar = DraftBar(draft, self.session.draft_matches_disk(draft))
        bar.recover_button.clicked.connect(self.recover_draft)
        bar.compare_button.clicked.connect(self.compare_draft)
        bar.discard_button.clicked.connect(self.discard_draft)
        self.banners.addWidget(bar)
        self.draft_bar = bar

    def _remove_draft_bar(self) -> None:
        if self.draft_bar is not None:
            self.banners.removeWidget(self.draft_bar)
            self.draft_bar.deleteLater()
            self.draft_bar = None

    def recover_draft(self) -> None:
        if self.draft_bar is None:
            return
        self.session.recover_draft(self.draft_bar.draft)
        self._remove_draft_bar()
        self.hooks.toast("Recovered unsaved text")

    def compare_draft(self) -> None:
        if self.draft_bar is None:
            return
        model = diff_lines(
            self.session.buffer_text(), self.draft_bar.draft.text, labels=("the note now", "unsaved text")
        )
        self.hooks.open_diff("Unsaved text compared with the note", model, "")

    def discard_draft(self) -> None:
        if self.draft_bar is None:
            return
        if not self.hooks.confirm(
            "Discard unsaved text?",
            "The kept text is deleted. The note stays as it is.",
            "Discard",
            True,
        ):
            return
        self.session.discard_draft()
        self._remove_draft_bar()

    def _on_problem(self, code: str, message: str) -> None:
        """Shown under the sync line until the next save, unless the banner or the line already say it."""
        if code in IN_CONTEXT_ELSEWHERE or not message:
            return
        self.notice.setText(message)
        self.notice.show()
        self.changed.emit()

    def _clear_notice(self, *_args: Any) -> None:
        self.notice.clear()
        self.notice.hide()
