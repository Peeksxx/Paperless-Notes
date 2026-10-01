"""User settings: one schema-versioned JSON file, typed and range-checked."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Protocol

from paperless_notes.core.jsonstore import read_json, write_json
from paperless_notes.core.session import SessionConfig

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_LIBRARY_ROOTS = 64
MAX_PATH_CHARS = 32_767


@dataclass(frozen=True, slots=True)
class Settings:
    schema_version: int = SCHEMA_VERSION
    autosave_debounce_ms: int = 1500
    autosave_debounce_onedrive_ms: int = 3000
    autosave_max_wait_ms: int = 15000
    draft_interval_ms: int = 500
    external_settle_ms: int = 500
    poll_focused_s: int = 5
    poll_unfocused_s: int = 30
    poll_minimized_s: int = 60
    max_file_mb: int = 10
    history_enabled: bool = True
    history_min_interval_s: int = 300
    history_keep_all_hours: int = 24
    history_hourly_days: int = 7
    history_daily_days: int = 90
    history_max_mb: int = 256
    ui_scale_percent: int = 100
    last_used_directory: str = ""
    run_on_startup: bool = False
    library_roots: tuple[str, ...] = field(default_factory=tuple)
    legacy_imported: bool = False
    theme: str = "system"
    accent: str = "graphite"
    note_font: str = "sans"
    readable_width: int = 720
    custom_frame: bool = True
    reduced_motion: bool = False
    show_hints: bool = True
    check_spelling: bool = True
    dismissed_hints: tuple[str, ...] = field(default_factory=tuple)


RANGES: Mapping[str, tuple[int, int]] = {
    "autosave_debounce_ms": (200, 60_000),
    "autosave_debounce_onedrive_ms": (200, 60_000),
    "autosave_max_wait_ms": (1_000, 300_000),
    "draft_interval_ms": (100, 10_000),
    "external_settle_ms": (100, 5_000),
    "poll_focused_s": (1, 300),
    "poll_unfocused_s": (5, 3_600),
    "poll_minimized_s": (5, 3_600),
    "max_file_mb": (1, 200),
    "history_min_interval_s": (0, 86_400),
    "history_keep_all_hours": (1, 720),
    "history_hourly_days": (0, 365),
    "history_daily_days": (0, 3_650),
    "history_max_mb": (16, 100_000),
    "ui_scale_percent": (75, 200),
    "readable_width": (0, 2_000),
}

CHOICES: Mapping[str, tuple[str, ...]] = {
    "theme": ("system", "light", "dark"),
    "accent": ("graphite", "lime"),
    "note_font": ("sans", "serif", "mono"),
}


def _validate(raw: Mapping[str, Any]) -> Settings:
    defaults = Settings()
    values: dict[str, Any] = {}
    for f in fields(Settings):
        if f.name == "schema_version" or f.name not in raw:
            continue
        value = raw[f.name]
        default = getattr(defaults, f.name)
        ok: bool
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, int):
            low, high = RANGES.get(f.name, (-(2**31), 2**31))
            ok = isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
        elif isinstance(default, str):
            ok = isinstance(value, str) and len(value) <= MAX_PATH_CHARS
            if ok and f.name in CHOICES:
                ok = value in CHOICES[f.name]
        else:
            ok = (
                isinstance(value, list)
                and len(value) <= MAX_LIBRARY_ROOTS
                and all(isinstance(v, str) and 0 < len(v) <= MAX_PATH_CHARS for v in value)
            )
            if ok:
                value = tuple(value)
        if ok:
            values[f.name] = value
        else:
            logger.warning("Setting %s has an invalid value; using the default", f.name)
    unknown = sorted(set(raw) - {f.name for f in fields(Settings)})
    if unknown:
        logger.info("Ignoring %d unknown setting(s)", len(unknown))
    return replace(defaults, **values)


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> Settings:
        data = read_json(self._path)
        if data is None:
            return Settings()
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            logger.warning("Settings schema %r is not supported; using defaults", version)
            return Settings()
        return _validate(data)

    def save(self, settings: Settings) -> None:
        data = asdict(settings)
        data["library_roots"] = list(settings.library_roots)
        data["dismissed_hints"] = list(settings.dismissed_hints)
        write_json(self._path, data)


def session_config(settings: Settings) -> SessionConfig:
    return SessionConfig(
        debounce_s=settings.autosave_debounce_ms / 1000,
        onedrive_debounce_s=settings.autosave_debounce_onedrive_ms / 1000,
        max_wait_s=settings.autosave_max_wait_ms / 1000,
        draft_interval_s=settings.draft_interval_ms / 1000,
        settle_s=settings.external_settle_ms / 1000,
        max_bytes=settings.max_file_mb * 1024 * 1024,
        history_min_interval_s=float(settings.history_min_interval_s),
    )


def with_default_library(settings: Settings, notes_dir: Path) -> Settings:
    """Use the app-owned notes folder when no library has been configured."""
    if settings.library_roots:
        return settings
    return replace(settings, library_roots=(str(notes_dir),))


class LegacyReader(Protocol):
    def read(self, names: tuple[str, ...]) -> dict[str, object]: ...


LEGACY_KEY = r"Software\Paperless\Paperless"
LEGACY_NAMES = ("last_used_directory", "ui_scale_percent")


class LegacyRegistryReader:
    """Reads exactly the named values from the v3 QSettings key; never enumerates it."""

    def read(self, names: tuple[str, ...]) -> dict[str, object]:
        import winreg

        found: dict[str, object] = {}
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, LEGACY_KEY, 0, winreg.KEY_QUERY_VALUE)
        except FileNotFoundError:
            return found
        with key:
            for name in names:
                try:
                    found[name] = winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    continue
        return found


def import_legacy(settings: Settings, reader: LegacyReader) -> Settings:
    """One-time, read-only import of two v3 values. Never raises; a missing key imports nothing."""
    if settings.legacy_imported:
        return settings
    try:
        values = reader.read(LEGACY_NAMES)
    except (OSError, ValueError) as exc:
        logger.warning("Legacy settings import skipped: %s", exc)
        values = {}
    changes: dict[str, Any] = {"legacy_imported": True}
    directory = values.get("last_used_directory")
    if isinstance(directory, str) and 0 < len(directory) <= MAX_PATH_CHARS and os.path.isabs(directory):
        changes["last_used_directory"] = directory
    scale = values.get("ui_scale_percent")
    try:
        scale_value = int(str(scale)) if scale is not None else None
    except ValueError:
        scale_value = None
    low, high = RANGES["ui_scale_percent"]
    if scale_value is not None and low <= scale_value <= high:
        changes["ui_scale_percent"] = scale_value
    return replace(settings, **changes)


def load_settings(path: Path, reader: LegacyReader | None = None) -> Settings:
    """Load, apply the one-time legacy import, and persist if anything changed. Never fails startup."""
    store = SettingsStore(path)
    settings = store.load()
    if reader is not None and not settings.legacy_imported:
        settings = import_legacy(settings, reader)
        try:
            store.save(settings)
        except OSError as exc:
            logger.warning("Saving settings after the legacy import failed: %s", exc)
    return settings
