"""Чтение записей установленных программ из трёх представлений реестра."""

from __future__ import annotations

import ntpath
import re
import winreg
from dataclasses import dataclass, field
from datetime import datetime
from threading import Event
from typing import Any, Literal

from .. import config

AppSource = Literal["hklm", "hklm_wow", "hkcu"]
UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
REGISTRY_VIEWS: dict[AppSource, tuple[int, int]] = {
    "hklm": (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
    "hklm_wow": (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
    "hkcu": (winreg.HKEY_CURRENT_USER, 0),
}
_FIELDS = (
    "DisplayName",
    "Publisher",
    "DisplayVersion",
    "InstallDate",
    "InstallLocation",
    "DisplayIcon",
    "EstimatedSize",
    "UninstallString",
    "QuietUninstallString",
    "SystemComponent",
    "ParentKeyName",
    "ReleaseType",
    "NoRemove",
)


@dataclass(frozen=True, slots=True)
class RegisteredApp:
    source: AppSource
    reg_key: str
    name: str
    publisher: str | None
    version: str | None
    install_date: float | None
    install_location: str | None
    display_icon: str | None
    estimated_size: int | None
    uninstall_cmd: str | None
    quiet_uninstall_cmd: str | None


@dataclass
class Inventory:
    apps: list[RegisteredApp] = field(default_factory=list)
    complete: set[AppSource] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def local_path(value: str) -> str | None:
    path = ntpath.normpath(config.expand_windows_vars(value.strip().strip('"')))
    drive, tail = ntpath.splitdrive(path)
    if len(drive) == 2 and drive[0].isascii() and drive[0].isalpha() and tail.startswith("\\"):
        return path
    return None


def _display_icon(value: str | None) -> str | None:
    if value is None:
        return None
    executable = re.sub(r",\s*-?\d+\s*$", "", value.strip().strip('"')).strip().strip('"')
    path = local_path(executable)
    return path if path is not None and path.lower().endswith(".exe") else None


def _date(value: str | None) -> float | None:
    if not value or len(value) != 8:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").timestamp()
    except (ValueError, OSError, OverflowError):
        return None


def _size(value: Any) -> int | None:
    try:
        size = int(value) * 1024
    except (TypeError, ValueError, OverflowError):
        return None
    return size if 0 <= size <= 2**63 - 1 else None


def _read_values(key: winreg.HKEYType) -> dict[str, Any]:
    values = {}
    for name in _FIELDS:
        try:
            values[name] = winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            continue
    return values


def _record(source: AppSource, name: str, values: dict[str, Any]) -> RegisteredApp | None:
    title = _text(values.get("DisplayName"))
    release = (_text(values.get("ReleaseType")) or "").casefold()
    if (
        not title
        or str(values.get("SystemComponent")) == "1"
        or values.get("ParentKeyName")
        or any(word in release for word in ("update", "hotfix", "service pack"))
    ):
        return None
    return RegisteredApp(
        source=source,
        reg_key=name,
        name=title,
        publisher=_text(values.get("Publisher")),
        version=_text(values.get("DisplayVersion")),
        install_date=_date(_text(values.get("InstallDate"))),
        install_location=local_path(_text(values.get("InstallLocation")) or ""),
        display_icon=_display_icon(_text(values.get("DisplayIcon"))),
        estimated_size=_size(values.get("EstimatedSize")),
        uninstall_cmd=None if str(values.get("NoRemove")) == "1" else _text(values.get("UninstallString")),
        quiet_uninstall_cmd=_text(values.get("QuietUninstallString")),
    )


def read_registry(cancel: Event) -> Inventory:
    result = Inventory()
    for source, (hive, view) in REGISTRY_VIEWS.items():
        if cancel.is_set():
            break
        incomplete = False
        try:
            with winreg.OpenKey(hive, UNINSTALL_KEY, 0, winreg.KEY_READ | view) as root:
                index = 0
                while not cancel.is_set():
                    try:
                        name = winreg.EnumKey(root, index)
                    except OSError as error:
                        if getattr(error, "winerror", None) == 259:
                            break
                        raise
                    index += 1
                    try:
                        with winreg.OpenKey(root, name, 0, winreg.KEY_READ | view) as key:
                            app = _record(source, name, _read_values(key))
                    except OSError:
                        incomplete = True
                        continue
                    if app is not None:
                        result.apps.append(app)
        except FileNotFoundError:
            # На чистой установке представление Uninstall может отсутствовать.
            pass
        except OSError:
            incomplete = True
        if incomplete:
            result.notes.append(f"Не все записи реестра {source} доступны. Предыдущие сведения сохранены.")
        elif not cancel.is_set():
            result.complete.add(source)
    return result


def current_app(source: AppSource, reg_key: str) -> RegisteredApp | None:
    if not reg_key or "\\" in reg_key or "/" in reg_key:
        return None
    hive, view = REGISTRY_VIEWS[source]
    try:
        with winreg.OpenKey(hive, UNINSTALL_KEY + "\\" + reg_key, 0, winreg.KEY_READ | view) as key:
            return _record(source, reg_key, _read_values(key))
    except FileNotFoundError:
        return None
