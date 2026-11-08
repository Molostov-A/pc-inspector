"""Проверка, что Корзина Windows примет объект, а не удалит его безвозвратно.

Оболочка Windows молча удаляет объект мимо Корзины, если он больше её квоты, на томе включено
немедленное удаление или путь слишком длинный. Если настройки прочитать нельзя, отправка запрещается.
"""

from __future__ import annotations

import ctypes
import shutil
import winreg
from pathlib import Path

from . import snapshot

BITBUCKET_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume"
POLICY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"
MAX_SHELL_PATH = 259
# Запас на округление по кластерам и служебные файлы $I.
QUOTA_SHARE = 0.9
ADVICE = "Выберите действие «В карантин»."


def _volume_guid(anchor: str) -> str:
    buffer = ctypes.create_unicode_buffer(64)
    if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(anchor, buffer, len(buffer)):
        raise OSError("Не удалось определить том")
    return buffer.value[buffer.value.index("{") :].rstrip("\\")


def _policy(name: str) -> int | None:
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, POLICY_KEY) as key:
                return int(winreg.QueryValueEx(key, name)[0])
        except FileNotFoundError:
            continue
    return None


def quota(anchor: str) -> int:
    """Вернуть максимальный размер Корзины тома в байтах."""
    try:
        if _policy("NoRecycleFiles"):
            raise ValueError(f"Политика Windows отключает Корзину. {ADVICE}")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{BITBUCKET_KEY}\{_volume_guid(anchor)}") as key:
            if winreg.QueryValueEx(key, "NukeOnDelete")[0]:
                raise ValueError(f"На этом диске Корзина отключена. {ADVICE}")
            limit = int(winreg.QueryValueEx(key, "MaxCapacity")[0]) * 1024**2
        percent = _policy("RecycleBinSize")
    except OSError as ex:
        raise ValueError(f"Не удалось проверить настройки Корзины. {ADVICE}") from ex
    return limit if percent is None else min(limit, shutil.disk_usage(anchor).total * percent // 100)


def ensure_recyclable(path: str) -> None:
    """Отклонить объект, который Windows может удалить мимо Корзины."""
    target = Path(path)
    total = 0
    for entry_path, _kind, size, _mtime in snapshot.live_entries(target):
        if len(entry_path) > MAX_SHELL_PATH:
            raise ValueError(f"Путь внутри объекта слишком длинный для Корзины. {ADVICE}")
        total += size
    if total > quota(target.anchor) * QUOTA_SHARE:
        raise ValueError(f"Объект не помещается в Корзину этого диска. {ADVICE}")
