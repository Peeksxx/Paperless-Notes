"""Validation of names for new or renamed notes, including OneDrive's sync rules."""

from __future__ import annotations

import ntpath
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from paperless_notes.core.security.names import (
    INVALID_NAME_CHARS,
    ONEDRIVE_MAX_PATH_UNITS,
    has_control_chars,
    is_device_name,
    onedrive_name_problem,
    utf16_units,
)

NOTE_EXTENSION = ".md"
KNOWN_EXTENSIONS = (".md", ".markdown", ".txt")
MAX_NAME_UNITS = 200
MAX_PATH_UNITS = 32_000


@dataclass(frozen=True, slots=True)
class NameCheck:
    ok: bool
    name: str
    problem: str | None = None


def validate_note_name(
    raw: str, existing: Iterable[str] = (), folder: str | None = None, in_onedrive: bool = False
) -> NameCheck:
    """Return the final file name (with extension) or the reason it cannot be used.

    ``folder`` enables the total path budget; ``in_onedrive`` adds OneDrive's rules and its path limit.
    """
    name = unicodedata.normalize("NFC", raw).strip(" ")
    if not name:
        return NameCheck(False, "", "The name is empty.")
    if has_control_chars(name):
        return NameCheck(False, name, "The name contains control characters.")
    bad = sorted({c for c in name if c in INVALID_NAME_CHARS})
    if bad:
        return NameCheck(False, name, "The name cannot contain " + " ".join(bad))
    if name.endswith((".", " ")):
        return NameCheck(False, name, "The name cannot end with a dot or a space.")
    if name.startswith("~$"):
        return NameCheck(False, name, "Names starting with ~$ are reserved for temporary files.")
    if not name.casefold().endswith(KNOWN_EXTENSIONS):
        name += NOTE_EXTENSION
    extension = next(e for e in KNOWN_EXTENSIONS if name.casefold().endswith(e))
    stem = name[: -len(extension)]
    if not stem.strip(" ."):
        return NameCheck(False, name, "The name is empty.")
    if stem.endswith((".", " ")):
        return NameCheck(False, name, "The name cannot end with a dot or a space.")
    if is_device_name(name):
        return NameCheck(False, name, "That name is reserved by Windows.")
    if utf16_units(name) > MAX_NAME_UNITS:
        return NameCheck(False, name, f"The name is longer than {MAX_NAME_UNITS} characters.")
    if in_onedrive:
        problem = onedrive_name_problem(name)
        if problem is not None:
            return NameCheck(False, name, problem)
    if folder is not None:
        full = ntpath.join(folder, name)
        limit = ONEDRIVE_MAX_PATH_UNITS if in_onedrive else MAX_PATH_UNITS
        if utf16_units(full) > limit:
            where = "OneDrive allows" if in_onedrive else "Windows allows"
            return NameCheck(False, name, f"The full path would be longer than {where} ({limit} characters).")
    wanted = unicodedata.normalize("NFC", name).casefold()
    if any(unicodedata.normalize("NFC", other).casefold() == wanted for other in existing):
        return NameCheck(False, name, "A note with this name already exists here.")
    return NameCheck(True, name)


def validate_folder_name(
    raw: str, existing: Iterable[str] = (), parent: str | None = None, in_onedrive: bool = False
) -> NameCheck:
    """Like :func:`validate_note_name` for a folder: the same Windows and OneDrive rules, no extension."""
    name = unicodedata.normalize("NFC", raw).strip(" ")
    problem: str | None = None
    if not name.strip("."):
        problem = "The name is empty."
    elif has_control_chars(name):
        problem = "The name contains control characters."
    elif any(c in INVALID_NAME_CHARS for c in name):
        problem = "The name cannot contain " + " ".join(sorted({c for c in name if c in INVALID_NAME_CHARS}))
    elif name.endswith((".", " ")):
        problem = "The name cannot end with a dot or a space."
    elif name.startswith("~$"):
        problem = "Names starting with ~$ are reserved for temporary files."
    elif is_device_name(name):
        problem = "That name is reserved by Windows."
    elif utf16_units(name) > MAX_NAME_UNITS:
        problem = f"The name is longer than {MAX_NAME_UNITS} characters."
    elif in_onedrive:
        problem = onedrive_name_problem(name)
    if problem is None and parent is not None:
        limit = ONEDRIVE_MAX_PATH_UNITS if in_onedrive else MAX_PATH_UNITS
        if utf16_units(ntpath.join(parent, name)) > limit:
            where = "OneDrive allows" if in_onedrive else "Windows allows"
            problem = f"The full path would be longer than {where} ({limit} characters)."
    wanted = name.casefold()
    if problem is None and any(unicodedata.normalize("NFC", o).casefold() == wanted for o in existing):
        problem = "Something with this name already exists here."
    return NameCheck(problem is None, name, problem)


def sync_name_warning(path: str, in_onedrive: bool) -> str | None:
    """Plain warning for an existing note whose name or path OneDrive will not sync."""
    if not in_onedrive:
        return None
    problem = onedrive_name_problem(ntpath.basename(path))
    if problem is None and utf16_units(path) > ONEDRIVE_MAX_PATH_UNITS:
        problem = f"The full path is longer than {ONEDRIVE_MAX_PATH_UNITS} characters."
    return None if problem is None else f"This file name will not sync to OneDrive. {problem}"
