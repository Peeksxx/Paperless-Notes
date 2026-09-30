"""Short labels shared by the palette, the home screen and the library: note titles, folders relative to
their library folder, and relative times."""

from __future__ import annotations

import ntpath
import time

from paperless_notes.core import pathid

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def note_title(path: str) -> str:
    """The file name without a ``.md`` or ``.markdown`` extension; other extensions stay visible."""
    name = ntpath.basename(path.rstrip("\\"))
    stem, ext = ntpath.splitext(name)
    return stem if ext.casefold() in (".md", ".markdown") and stem else name


def location_of(path: str, roots: list[str]) -> str:
    """The folder of a note relative to the library folder holding it (or the full folder)."""
    folder = ntpath.dirname(path)
    for root in roots:
        if pathid.is_within(folder, root) or pathid.same_path(folder, root):
            base = ntpath.basename(root.rstrip("\\")) or root
            rest = folder[len(pathid.normalize(root).rstrip("\\")) :].strip("\\")
            return base + ("\\" + rest if rest else "")
    return folder


def relative_time(mtime_ns: int, now: float | None = None) -> str:
    """Labels such as just now, 5 min ago, 3 h ago, yesterday, 4 days ago, then a date."""
    current = time.time() if now is None else now
    then = mtime_ns / 1e9
    seconds = current - then
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    local_now = time.localtime(current)
    local_then = time.localtime(then)
    days = (
        time.mktime((local_now.tm_year, local_now.tm_mon, local_now.tm_mday, 0, 0, 0, 0, 0, -1))
        - time.mktime((local_then.tm_year, local_then.tm_mon, local_then.tm_mday, 0, 0, 0, 0, 0, -1))
    ) // 86400
    if days <= 0:
        return f"{int(seconds // 3600)} h ago"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{int(days)} days ago"
    date = f"{local_then.tm_mday} {_MONTHS[local_then.tm_mon - 1]}"
    return date if local_then.tm_year == local_now.tm_year else f"{date} {local_then.tm_year}"


def day_group(mtime_ns: int, now: float | None = None) -> str:
    """Today, Yesterday, This week or Earlier, by local calendar day."""
    label = relative_time(mtime_ns, now)
    if label == "just now" or label.endswith(("min ago", "h ago")):
        return "Today"
    if label == "yesterday":
        return "Yesterday"
    if label.endswith("days ago"):
        return "This week"
    return "Earlier"
