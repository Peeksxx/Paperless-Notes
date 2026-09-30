"""Run-at-logon registration (SEC4): quoted path, least-privilege access, frozen builds only, removable."""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Paperless Notes"


class RunKeyBackend(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class WinRegRunKey:
    """``HKCU\\...\\Run`` through ``winreg``; opened with only the access each call needs."""

    def get(self, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
                value, kind = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        return value if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str) else None

    def set(self, name: str, value: str) -> None:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete(self, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            return


class StartupRegistration:
    def __init__(self, backend: RunKeyBackend, exe_path: str | None, frozen: bool) -> None:
        self._backend = backend
        self._exe = exe_path
        self._frozen = frozen

    @property
    def available(self) -> bool:
        """Registration is only meaningful for the installed executable, never for python.exe."""
        return self._frozen and bool(self._exe)

    def command(self) -> str:
        return f'"{self._exe}"'

    def is_enabled(self) -> bool:
        return self.available and self._backend.get(VALUE_NAME) == self.command()

    def enable(self) -> bool:
        if not self.available:
            return False
        if self._backend.get(VALUE_NAME) != self.command():
            self._backend.set(VALUE_NAME, self.command())
            logger.info("Registered to start at logon")
        return True

    def disable(self) -> None:
        if self._backend.get(VALUE_NAME) is not None:
            self._backend.delete(VALUE_NAME)
            logger.info("Removed the start-at-logon entry")

    def sync(self, wanted: bool) -> bool:
        """Make the registry match the setting. Returns whether the app is registered afterwards."""
        try:
            if wanted:
                return self.enable()
            self.disable()
        except OSError as exc:
            logger.warning("Updating the start-at-logon entry failed: %s", exc)
        return self.is_enabled() if wanted else False
