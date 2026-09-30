"""Which links may be opened (SEC11): only http, https and mailto, and only on an explicit user action."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from paperless_notes.core.security.names import has_control_chars

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https", "mailto"})
MAX_LINK_CHARS = 2048
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*):")
_AUTHORITY = re.compile(r"//([^/?#\s]+)")
_ENCODED_BREAK = re.compile(r"%0[aAdD]|%00")
_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")
_ATTACH_KEYS = frozenset({"attach", "attachment", "attachments"})


def _mailto_attaches(rest: str) -> bool:
    """Some mail clients attach local or UNC files named in these query keys."""
    _, _, query = rest.partition("?")
    for part in query.split("&"):
        key = _PERCENT.sub(lambda m: chr(int(m.group(1), 16)), part.split("=", 1)[0])
        if key.strip().casefold() in _ATTACH_KEYS:
            return True
    return False


def link_allowed(url: str) -> bool:
    u = url.strip()
    if not u or len(u) > MAX_LINK_CHARS or has_control_chars(u) or any(c.isspace() for c in u):
        return False
    m = _SCHEME.match(u)
    if m is None:
        return False
    scheme = m.group(1).lower()
    if scheme not in ALLOWED_SCHEMES:
        return False
    rest = u[m.end() :]
    if _ENCODED_BREAK.search(rest):
        return False
    if scheme == "mailto":
        return bool(rest) and not rest.startswith("/") and not _mailto_attaches(rest)
    authority = _AUTHORITY.match(rest)
    if authority is None:
        return False
    host = authority.group(1)
    return "@" not in host and "\\" not in host and host not in (".", "..")


def open_link(url: str, opener: Callable[[str], bool]) -> bool:
    """Open ``url`` with ``opener`` (for example ``QDesktopServices.openUrl``) if the policy allows it."""
    if not link_allowed(url):
        logger.info("Blocked a link with a disallowed or malformed target")
        return False
    return opener(url.strip())
