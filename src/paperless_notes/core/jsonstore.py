"""Small JSON files in the local state folder: capped reads, atomic writes, no exceptions on bad input."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from paperless_notes.core.fsops import write_local_file

logger = logging.getLogger(__name__)

MAX_STATE_BYTES = 4 * 1024 * 1024


def read_json(path: Path) -> dict[str, Any] | None:
    """The file's top-level object, or None when missing, oversized or malformed (logged)."""
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            logger.warning("Ignoring oversized state file %s", path.name)
            return None
        data = json.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        logger.warning("Ignoring unreadable state file %s: %s", path.name, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("Ignoring state file %s: not an object", path.name)
        return None
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    write_local_file(str(path), payload.encode("utf-8"))
