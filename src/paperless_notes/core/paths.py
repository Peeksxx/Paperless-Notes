"""Locations of machine-local application state (never roaming, never synced)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from paperless_notes.core import pathid
from paperless_notes.core.onedrive import roots_from_env

APP_DIR_NAME = "Paperless Notes"
DATA_DIR_ENV = "PAPERLESS_NOTES_DATA_DIR"


class UnsafeDataDirError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.json"

    @property
    def session_file(self) -> Path:
        return self.root / "session.json"

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def drafts_dir(self) -> Path:
        return self.root / "drafts"

    @property
    def history_dir(self) -> Path:
        return self.root / "history"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def notes_dir(self) -> Path:
        """The app-owned library used until the user adds another library folder."""
        return self.root / "Notes"

    @property
    def ipc_dir(self) -> Path:
        return self.root / "ipc"

    @property
    def index_dir(self) -> Path:
        return self.root / "index"

    @property
    def dictionary_file(self) -> Path:
        return self.root / "dictionary.json"

    @property
    def ledger_dir(self) -> Path:
        return self.root / "ledger"

    @property
    def evidence_dir(self) -> Path:
        return self.root / "evidence"

    @property
    def calibration_file(self) -> Path:
        return self.root / "calibration.json"

    @property
    def lock_file(self) -> Path:
        return self.root / "instance.lock"

    def ensure(self) -> None:
        for d in (
            self.root,
            self.notes_dir,
            self.drafts_dir,
            self.history_dir,
            self.logs_dir,
            self.ipc_dir,
            self.ledger_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def resolve_paths(env: Mapping[str, str] | None = None) -> AppPaths:
    """Pick the state folder: the test override, else %LOCALAPPDATA%\\Paperless Notes."""
    env = os.environ if env is None else env
    override = env.get(DATA_DIR_ENV, "").strip()
    if override:
        root = Path(override)
    else:
        local = env.get("LOCALAPPDATA", "").strip()
        root = (Path(local) if local else Path.home() / "AppData" / "Local") / APP_DIR_NAME
    for unsafe in (env.get("APPDATA", "").strip(), *roots_from_env(env)):
        if unsafe and pathid.is_within(root, unsafe):
            raise UnsafeDataDirError(f"App state must not live in a roaming or synced folder: {root}")
    return AppPaths(root)
