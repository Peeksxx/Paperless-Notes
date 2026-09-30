"""Windows and OneDrive file-name rules shared by the resource policy and the note-name validator.

OneDrive rules verified 2026-09-29 against Microsoft's "Restrictions and limitations in OneDrive and
SharePoint": blocked characters, the names .lock, CON, PRN, AUX, NUL, COM0 to COM9, LPT0 to LPT9, _vti_,
desktop.ini, any name starting with ~$, and leading or trailing spaces.

Path length, from Microsoft's "What are file path length limits" page: the decoded path in OneDrive may not
exceed 400 characters, and the local path may reach 520 (up to 400 below the OneDrive folder plus up to 120
for the folder itself). Product policy: the validator applies 400 to the whole local path, which is
stricter than both documented limits, because the note's path inside the cloud (for example the library a
shared folder maps to) cannot be known locally, and staying under 400 locally guarantees the cloud path does.
"""

from __future__ import annotations

_DIGITS = "0123456789\N{SUPERSCRIPT ONE}\N{SUPERSCRIPT TWO}\N{SUPERSCRIPT THREE}"

RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    | {f"COM{n}" for n in _DIGITS}
    | {f"LPT{n}" for n in _DIGITS}
)
INVALID_NAME_CHARS = frozenset('<>:"/\\|?*')
ONEDRIVE_BLOCKED_NAMES = frozenset({".lock", "desktop.ini"})
ONEDRIVE_MAX_PATH_UNITS = 400
NTFS_MAX_NAME_UNITS = 255


def is_device_name(name: str) -> bool:
    """True for CON, NUL.txt, com0.md and the like, which Windows maps to devices."""
    stem = name.split(".", 1)[0].rstrip(" ").upper()
    return stem in RESERVED_DEVICE_NAMES


def has_control_chars(name: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in name)


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def onedrive_name_problem(name: str) -> str | None:
    """Why OneDrive would refuse to sync ``name``, or None."""
    if name.startswith("~$"):
        return "OneDrive does not sync names that start with ~$."
    if name.casefold() in ONEDRIVE_BLOCKED_NAMES:
        return f"OneDrive does not sync files named {name}."
    if "_vti_" in name.casefold():
        return "OneDrive does not sync names that contain _vti_."
    if name != name.strip(" "):
        return "OneDrive does not sync names that start or end with a space."
    if is_device_name(name):
        return "Windows reserves this name."
    if any(c in INVALID_NAME_CHARS for c in name):
        return "The name contains a character OneDrive does not allow."
    return None
