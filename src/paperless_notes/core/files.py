"""Safe note and folder operations for the library: create, rename, move and delete to the Recycle Bin.

Nothing is ever overwritten: creation and renames fail when the target exists. Every name goes through the
note or folder validator, including OneDrive's rules and path budget when the folder is synced. Callers
release open sessions before renaming or moving a note and reopen them afterwards.
"""

from __future__ import annotations

import logging
import ntpath
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from paperless_notes.core import onedrive, pathid
from paperless_notes.core.fsops import Expect, ExternalChangeError, FileSystem, atomic_save
from paperless_notes.core.recycle import RecycleError, recycle
from paperless_notes.core.security.filenames import (
    KNOWN_EXTENSIONS,
    NameCheck,
    validate_folder_name,
    validate_note_name,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FileResult:
    ok: bool
    path: str | None = None
    problem: str | None = None


def is_note_file(name: str) -> bool:
    return name.casefold().endswith(KNOWN_EXTENSIONS) and not onedrive.is_temp_artifact(name)


class FileService:
    def __init__(
        self,
        fs: FileSystem,
        onedrive_roots: Sequence[str] = (),
        recycle_fn: Callable[[str, int | None], None] = recycle,
        rename_dir: Callable[[str, str], None] = os.rename,
        make_dir: Callable[[str], None] = os.mkdir,
        is_dir: Callable[[str], bool] = os.path.isdir,
    ) -> None:
        self._fs = fs
        self._roots = tuple(onedrive_roots)
        self._recycle = recycle_fn
        self._rename_dir = rename_dir
        self._make_dir = make_dir
        self._is_dir = is_dir

    def is_dir(self, path: str) -> bool:
        return self._is_dir(path)

    def in_onedrive(self, path: str) -> bool:
        return onedrive.in_sync_root(path, self._roots)

    def _names(self, folder: str) -> list[str]:
        try:
            return self._fs.list_dir(folder)
        except FileNotFoundError:
            return []

    def check_note_name(self, folder: str, raw_name: str) -> NameCheck:
        return validate_note_name(raw_name, self._names(folder), folder, self.in_onedrive(folder))

    def check_folder_name(self, parent: str, raw_name: str) -> NameCheck:
        return validate_folder_name(raw_name, self._names(parent), parent, self.in_onedrive(parent))

    def check_rename(self, path: str, raw_name: str) -> NameCheck:
        """The final name a rename would use, keeping a note's extension when none is typed."""
        folder, current = ntpath.split(pathid.normalize(path))
        others = [n for n in self._names(folder) if n.casefold() != current.casefold()]
        if self._is_dir(path):
            return validate_folder_name(raw_name, others, folder, self.in_onedrive(folder))
        wanted = raw_name.strip(" ")
        extension = ntpath.splitext(current)[1]
        if not wanted.casefold().endswith(KNOWN_EXTENSIONS) and extension.casefold() in KNOWN_EXTENSIONS:
            wanted += extension
        return validate_note_name(wanted, others, folder, self.in_onedrive(folder))

    def create_note(self, folder: str, raw_name: str) -> FileResult:
        check = self.check_note_name(folder, raw_name)
        if not check.ok:
            return FileResult(False, None, check.problem)
        path = ntpath.join(folder, check.name)
        try:
            atomic_save(self._fs, path, b"", Expect.ABSENT, 1)
        except (OSError, ExternalChangeError) as exc:
            logger.warning("Creating a note failed: %s", exc)
            return FileResult(
                False, None, "The note could not be created. A file with that name may already exist."
            )
        return FileResult(True, path)

    def create_folder(self, parent: str, raw_name: str) -> FileResult:
        check = self.check_folder_name(parent, raw_name)
        if not check.ok:
            return FileResult(False, None, check.problem)
        path = ntpath.join(parent, check.name)
        try:
            self._make_dir(path)
        except OSError as exc:
            logger.warning("Creating a folder failed: %s", exc)
            return FileResult(False, None, "The folder could not be created.")
        return FileResult(True, path)

    def rename(self, path: str, raw_name: str) -> FileResult:
        folder, current = ntpath.split(pathid.normalize(path))
        check = self.check_rename(path, raw_name)
        if not check.ok:
            return FileResult(False, None, check.problem)
        if check.name == current:
            return FileResult(True, path)
        return self._move(path, ntpath.join(folder, check.name), same_folder=True)

    def move(self, path: str, destination: str) -> FileResult:
        name = ntpath.basename(pathid.normalize(path))
        in_onedrive = self.in_onedrive(destination)
        if self._is_dir(path):
            if pathid.is_within(destination, path) or pathid.same_path(destination, path):
                return FileResult(False, None, "A folder cannot be moved into itself.")
            check = validate_folder_name(name, self._names(destination), destination, in_onedrive)
        else:
            check = validate_note_name(name, self._names(destination), destination, in_onedrive)
        if not check.ok:
            return FileResult(False, None, check.problem)
        return self._move(path, ntpath.join(destination, check.name), same_folder=False)

    def _move(self, source: str, target: str, same_folder: bool) -> FileResult:
        case_only = same_folder and pathid.same_path(source, target)
        try:
            if self._is_dir(source):
                if not case_only and self._is_dir(target):
                    return FileResult(False, None, "Something with this name already exists there.")
                self._rename_dir(source, target)
            elif case_only:
                temp = target + ".renaming"
                self._fs.rename_new(source, temp)
                self._fs.rename_new(temp, target)
            else:
                self._fs.rename_new(source, target)
        except FileExistsError:
            return FileResult(False, None, "Something with this name already exists there.")
        except OSError as exc:
            logger.warning("Rename or move failed: %s", exc)
            return FileResult(
                False, None, "The file could not be renamed or moved. It may be open elsewhere."
            )
        return FileResult(True, target)

    def delete(self, path: str, hwnd: int | None = None) -> FileResult:
        try:
            self._recycle(path, hwnd)
        except RecycleError as exc:
            logger.info("Delete to the Recycle Bin did not complete: %s", exc)
            return FileResult(False, None, "The item was not moved to the Recycle Bin.")
        return FileResult(True, path)
