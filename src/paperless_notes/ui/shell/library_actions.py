"""Sidebar requests turned into safe file operations: names are checked before anything is written, open
notes are saved and released before a rename or move and reopened afterwards, and delete asks first."""

from __future__ import annotations

import ntpath
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication

from paperless_notes.core import pathid
from paperless_notes.core.files import FileResult, FileService
from paperless_notes.ui.shell.dialogs import Prompter
from paperless_notes.ui.shell.sidebar import Sidebar
from paperless_notes.ui.shell.workbench import Workbench


class LibraryActions(QObject):
    roots_changed = Signal(list)
    files_changed = Signal(list)

    def __init__(
        self,
        files: FileService,
        prompter: Prompter,
        workbench: Workbench,
        sidebar: Sidebar,
        toast: Callable[[str], None],
        hwnd: Callable[[], int | None] = lambda: None,
    ) -> None:
        super().__init__()
        self.files = files
        self.prompter = prompter
        self.workbench = workbench
        self.sidebar = sidebar
        self.toast = toast
        self._hwnd = hwnd
        sidebar.open_requested.connect(workbench.open_path)
        sidebar.new_note_requested.connect(self.new_note)
        sidebar.new_folder_requested.connect(self.new_folder)
        sidebar.add_root_requested.connect(self.add_root)
        sidebar.remove_root_requested.connect(self.remove_root)
        sidebar.rename_requested.connect(self.rename)
        sidebar.move_requested.connect(self.move)
        sidebar.delete_requested.connect(self.delete)
        sidebar.copy_path_requested.connect(self.copy_path)
        workbench.rename_requested.connect(self.rename_to)

    def _folder_or_ask(self, folder: str, title: str) -> str | None:
        if folder:
            return folder
        start = self.sidebar.roots()[0] if self.sidebar.roots() else ""
        return self.prompter.choose_folder(title, start)

    def new_note(self, folder: str = "") -> str | None:
        target = self._folder_or_ask(
            folder or self.sidebar.current_folder() or "", "Choose a folder for the note"
        )
        if target is None:
            return None
        name = self.prompter.ask_name(
            "New note",
            f"Name of the new note in {ntpath.basename(target) or target}",
            lambda raw: self.files.check_note_name(target, raw),
            "Untitled",
        )
        if name is None:
            return None
        result = self.files.create_note(target, name)
        if not result.ok or result.path is None:
            self.toast(result.problem or "The note could not be created.")
            return None
        self.sidebar.refresh()
        self.sidebar.select(result.path)
        self.files_changed.emit([result.path])
        self.workbench.open_path(result.path)
        return result.path

    def new_folder(self, parent: str = "") -> str | None:
        target = self._folder_or_ask(
            parent or self.sidebar.current_folder() or "", "Choose where to add a folder"
        )
        if target is None:
            return None
        name = self.prompter.ask_name(
            "New folder", "Name of the new folder", lambda raw: self.files.check_folder_name(target, raw)
        )
        if name is None:
            return None
        result = self.files.create_folder(target, name)
        if not result.ok or result.path is None:
            self.toast(result.problem or "The folder could not be created.")
            return None
        self.sidebar.refresh()
        self.sidebar.select(result.path)
        return result.path

    def add_root(self) -> str | None:
        folder = self.prompter.choose_folder("Add a folder to the library", "")
        if not folder:
            return None
        normalized = pathid.normalize(folder)
        roots = self.sidebar.roots()
        if not any(pathid.same_path(normalized, r) for r in roots):
            roots.append(normalized)
            self.sidebar.set_roots(roots)
            self.roots_changed.emit(roots)
        return normalized

    def remove_root(self, root: str) -> None:
        if not self.prompter.confirm(
            "Remove from library?",
            f"{root} is no longer shown in the sidebar. The folder and its notes stay on this PC.",
            "Remove from library",
        ):
            return
        roots = [r for r in self.sidebar.roots() if not pathid.same_path(r, root)]
        self.sidebar.set_roots(roots)
        self.roots_changed.emit(roots)

    def rename(self, path: str) -> str | None:
        stem = ntpath.basename(path) if self.files.is_dir(path) else ntpath.splitext(ntpath.basename(path))[0]
        name = self.prompter.ask_name(
            "Rename", "New name", lambda raw: self.files.check_rename(path, raw), stem, "Rename"
        )
        return None if name is None else self.rename_to(path, name)

    def rename_to(self, path: str, name: str) -> str | None:
        check = self.files.check_rename(path, name)
        if not check.ok:
            self.toast(check.problem or "That name cannot be used.")
            return None
        return self._relocate(path, lambda: self.files.rename(path, name), "Renamed")

    def move(self, path: str) -> str | None:
        destination = self.prompter.choose_folder("Move to folder", ntpath.dirname(path))
        if not destination:
            return None
        return self._relocate(path, lambda: self.files.move(path, destination), "Moved")

    def _relocate(self, path: str, operation: Callable[[], FileResult], verb: str) -> str | None:
        released = self.workbench.release_notes(path)
        if released is None:
            self.toast("An open note could not be saved first, so nothing was renamed or moved.")
            return None
        result = operation()
        new_path = result.path if result.ok else None
        if new_path is None:
            self.workbench.rebind(path, path)
            self.toast(result.problem or "Nothing was changed.")
            return None
        self.workbench.rebind(path, new_path)
        self.files_changed.emit([path, new_path])
        self.sidebar.refresh()
        self.sidebar.select(new_path)
        self.toast(f"{verb} to {ntpath.basename(new_path)}")
        return new_path

    def delete(self, path: str) -> bool:
        kind = "folder and everything in it" if self.files.is_dir(path) else "note"
        if not self.prompter.confirm(
            "Delete to Recycle Bin?",
            f"The {kind} {ntpath.basename(path)} is moved to the Recycle Bin. You can restore it from there.",
            "Move to Recycle Bin",
            danger=True,
        ):
            return False
        if not self.workbench.close_notes(path):
            return False
        result = self.files.delete(path, self._hwnd())
        if not result.ok:
            self.toast(result.problem or "The item was not moved to the Recycle Bin.")
            return False
        self.files_changed.emit([path])
        self.sidebar.refresh()
        self.toast(f"Moved {ntpath.basename(path)} to the Recycle Bin")
        return True

    def copy_path(self, path: str) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(path)
            self.toast("Path copied")
