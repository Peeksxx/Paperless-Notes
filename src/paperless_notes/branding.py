"""Product identity. The About dialog, README, version resource and installer read from here."""

from typing import Final

PRODUCT_NAME: Final = "Paperless Notes"
VERSION: Final = "4.3.0"
EXE_NAME: Final = "Paperless Notes.exe"
AUTHOR_HANDLE: Final = "@peeksxx"
DISCORD_HANDLE: Final = "@peeksxx"
TELEGRAM_HANDLE: Final = "@peeksxx"
TELEGRAM_URL: Final = "https://t.me/peeksxx"
WEBSITE_URL: Final = "https://peeksxx.dev"
FALLBACK_EMAIL: Final = "peekowolf@gmail.com"
CREDIT: Final = "Paperless Notes by @peeksxx (Discord, Telegram) - peeksxx.dev"
COPYRIGHT: Final = "Copyright (c) 2026 @peeksxx. Source available; see LICENSE."


def version_tuple(version: str = VERSION) -> tuple[int, int, int, int]:
    """The four numbers of a Windows version resource; refuses anything but ``major.minor.patch``."""
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not a release version: {version}")
    major, minor, patch = (int(p) for p in parts)
    return major, minor, patch, 0
