"""Единая проверка защищённых путей сканера и будущих действий."""

from __future__ import annotations

import ntpath
import os
import stat
import string
from pathlib import Path, PureWindowsPath

from .. import config
from ..config import DATA_DIR, PROJECT_ROOT

QUARANTINE_NAME = ".pci-quarantine"

SYSTEM_DIRS = frozenset(
    {
        "windows",
        "program files",
        "program files (x86)",
        "programdata",
        "system volume information",
        "$recycle.bin",
        "recovery",
        "boot",
        "$windows.~bt",
        "$windows.~ws",
    }
)
PROFILE_DIRS = frozenset(
    {
        "desktop",
        "documents",
        "downloads",
        "pictures",
        "music",
        "videos",
        "appdata",
        "onedrive",
        "favorites",
        "contacts",
        "links",
        "saved games",
        "searches",
    }
)


def _windows_path(path: str | os.PathLike[str]) -> PureWindowsPath:
    value = os.fspath(path).replace("/", "\\")
    if value[:8].lower() == "\\\\?\\unc\\":
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\") and ntpath.splitdrive(value[4:])[0].endswith(":"):
        value = value[4:]
    return PureWindowsPath(ntpath.normpath(value))


def is_protected(path: str | os.PathLike[str]) -> bool:
    """Проверить, входит ли путь в системную зону верхнего уровня тома."""
    candidate = _windows_path(path)
    return (
        candidate.is_absolute()
        and len(candidate.parts) > 1
        and candidate.parts[1].rstrip(" .").lower() in SYSTEM_DIRS
    )


def _validate(candidate: PureWindowsPath, *, managed: bool = False) -> None:
    if (
        not candidate.is_absolute()
        or len(candidate.drive) != 2
        or candidate.drive[0] not in string.ascii_letters
        or len(candidate.parts) == 1
    ):
        raise ValueError("Укажите абсолютный путь на локальном диске, отличный от корня")
    if is_protected(candidate):
        raise ValueError("Путь находится в защищённой системной зоне")
    if not managed and candidate.parts[1].lower() == QUARANTINE_NAME:
        raise ValueError("Путь находится внутри управляемого карантина")
    locations = [PROJECT_ROOT, DATA_DIR]
    if not managed and config.QUARANTINE_DIR is not None:
        locations.append(config.QUARANTINE_DIR)
    for location in locations:
        protected = _windows_path(location)
        if candidate.is_relative_to(protected) or protected.is_relative_to(candidate):
            raise ValueError("Путь пересекается с папкой PC Inspector или его данными")
    profile = _windows_path(os.environ.get("USERPROFILE") or Path.home())
    if profile.is_relative_to(candidate) or (
        candidate.parent == profile and candidate.name.lower() in PROFILE_DIRS
    ):
        raise ValueError("Нельзя использовать профиль пользователя или его основные папки целиком")


def _validate_syntax(raw: str, candidate: PureWindowsPath) -> None:
    if (
        "\0" in raw
        or candidate.is_reserved()
        or any(
            any(char in part for char in '<>:"|?*') or part != part.rstrip(" .")
            for part in candidate.parts[1:]
        )
    ):
        raise ValueError("Путь содержит недопустимые компоненты Windows")


def validate_action_path(path: str | os.PathLike[str], *, managed: bool = False) -> None:
    """Отклонить небезопасный путь с объяснением в ValueError."""
    raw = os.fspath(path)
    candidate = _windows_path(raw)
    _validate_syntax(raw, candidate)
    _validate(candidate, managed=managed)
    if os.name == "nt":
        try:
            resolved = Path(raw).resolve()
        except (OSError, RuntimeError) as ex:
            raise ValueError("Не удалось проверить фактический путь") from ex
        _validate(_windows_path(resolved), managed=managed)


def local_path_without_links(path: str) -> str:
    """Проверить путь Windows, не переходя по ссылкам и на сетевые тома."""
    if os.name != "nt":
        raise ValueError("Проверка локальных томов доступна только в Windows")

    import ctypes

    candidate = _windows_path(path)
    if (
        not candidate.is_absolute()
        or len(candidate.drive) != 2
        or candidate.drive[0] not in string.ascii_letters
        or ctypes.windll.kernel32.GetDriveTypeW(candidate.anchor) not in (2, 3)
    ):
        raise ValueError("Укажите путь на локальном диске")
    for part in (*reversed(candidate.parents), candidate):
        info = os.lstat(part)
        if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Путь проходит через точку повторной обработки")
    return str(candidate)


def checked_action_path(path: str, *, destination: bool = False, managed: bool = False) -> Path:
    """Проверить обычный путь или родительскую папку нового назначения."""
    candidate = _windows_path(path)
    _validate_syntax(path, candidate)
    _validate(candidate, managed=managed)
    local_path_without_links(str(candidate.parent if destination else candidate))
    if destination:
        resolved = Path(candidate.parent).resolve(strict=True) / candidate.name
        _validate(_windows_path(resolved), managed=managed)
    else:
        validate_action_path(path, managed=managed)
    return Path(candidate)


def paths_overlap(left: str, right: str) -> bool:
    first, second = _windows_path(left), _windows_path(right)
    return first.is_relative_to(second) or second.is_relative_to(first)


def quarantine_root(source: Path) -> Path:
    """Выбрать каталог карантина на томе источника."""
    default = Path(source.anchor) / QUARANTINE_NAME
    root = config.QUARANTINE_DIR or default
    if root == default:
        local_path_without_links(root.anchor)
    else:
        root = checked_action_path(str(root), destination=True, managed=True)
    if root.drive.lower() != source.drive.lower():
        raise ValueError("Карантин должен находиться на том же диске")
    return root
