"""Сопоставление Prefetch, UserAssist и индексированного atime с программами."""

from __future__ import annotations

import codecs
import ctypes
import json
import ntpath
import os
import re
import struct
import time
import winreg
from collections import defaultdict
from concurrent.futures import CancelledError
from dataclasses import asdict, dataclass
from pathlib import Path
from sqlite3 import Connection
from threading import Event
from typing import Literal
from uuid import UUID

from .. import config
from ..core.paths import path_comparison, root_filter
from ..core.safety import is_protected
from . import apps

USER_ASSIST = r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"
COLLECTION_KEY = "apps_collection"
_PREFETCH_NAME = re.compile(r"(.+\.exe)-[0-9a-f]{8}\.pf", re.IGNORECASE)
_FILETIME_EPOCH = 116444736000000000


@dataclass(frozen=True, slots=True)
class Usage:
    exe_name: str
    exe_path: str | None
    last_run: float
    run_count: int | None
    source: Literal["prefetch", "userassist", "atime"]


def _check(cancel: Event) -> None:
    if cancel.is_set():
        raise CancelledError()


def read_prefetch(directory: Path, notes: list[str], cancel: Event) -> dict[str, Usage]:
    evidence: dict[str, Usage] = {}
    incomplete = False
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                _check(cancel)
                match = _PREFETCH_NAME.fullmatch(entry.name)
                if not match:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    timestamp = entry.stat(follow_symlinks=False).st_mtime
                except OSError:
                    incomplete = True
                    continue
                name = match[1].casefold()
                if name not in evidence or evidence[name].last_run < timestamp:
                    evidence[name] = Usage(name, None, timestamp, None, "prefetch")
    except OSError:
        incomplete = True
    if incomplete:
        notes.append(
            "Prefetch недоступен полностью или частично. Использованы UserAssist и даты доступа из индекса."
        )
    return evidence


def _known_folder(guid: str) -> str | None:
    try:
        identifier = ctypes.create_string_buffer(UUID(guid).bytes_le)
    except ValueError:
        return None
    location = ctypes.c_wchar_p()
    # KF_FLAG_DONT_VERIFY позволяет получить путь без обращения к содержимому папки.
    code = ctypes.windll.shell32.SHGetKnownFolderPath(
        ctypes.byref(identifier), 0x4000, None, ctypes.byref(location)
    )
    try:
        return location.value if code == 0 else None
    finally:
        if location:
            ctypes.windll.ole32.CoTaskMemFree(location)


def decode_user_assist(name: str, data: bytes, folders: dict[str, str | None]) -> Usage | None:
    if len(data) < 68:
        return None
    count = struct.unpack_from("<I", data, 4)[0]
    ticks = struct.unpack_from("<Q", data, 60)[0]
    if ticks <= _FILETIME_EPOCH or ticks > 2650467743999999999:
        return None
    value = codecs.decode(name, "rot_13")
    if value.upper().startswith("UEME_RUNPATH:"):
        value = value.split(":", 1)[1]
    if value.startswith("{") and (split := value.find("}\\")) != -1:
        guid = value[: split + 1]
        if guid not in folders:
            folders[guid] = _known_folder(guid)
        if not folders[guid]:
            return None
        value = ntpath.join(folders[guid], value[split + 2 :])
    path = apps.local_path(value)
    if path is None or not path.lower().endswith(".exe"):
        return None
    return Usage(
        ntpath.basename(path).casefold(), path, (ticks - _FILETIME_EPOCH) / 10_000_000, count, "userassist"
    )


def _keys(key: winreg.HKEYType) -> list[str]:
    names = []
    while True:
        try:
            names.append(winreg.EnumKey(key, len(names)))
        except OSError as error:
            if getattr(error, "winerror", None) != 259:
                raise
            return names


def read_user_assist(notes: list[str], cancel: Event) -> dict[str, Usage]:
    result: dict[str, Usage] = {}
    folders: dict[str, str | None] = {}
    incomplete = False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, USER_ASSIST) as root:
            for guid in _keys(root):
                _check(cancel)
                try:
                    with winreg.OpenKey(root, guid + r"\Count") as counts:
                        index = 0
                        while True:
                            _check(cancel)
                            try:
                                name, value, kind = winreg.EnumValue(counts, index)
                            except OSError as error:
                                if getattr(error, "winerror", None) == 259:
                                    break
                                raise
                            index += 1
                            if kind != winreg.REG_BINARY or not isinstance(value, bytes):
                                continue
                            usage = decode_user_assist(name, value, folders)
                            if usage is not None and usage.exe_path is not None:
                                path = usage.exe_path.casefold()
                                if path not in result or usage.last_run > result[path].last_run:
                                    result[path] = usage
                except FileNotFoundError:
                    continue
                except OSError:
                    incomplete = True
    except OSError:
        incomplete = True
    if incomplete:
        notes.append("Данные UserAssist текущего пользователя недоступны полностью или частично.")
    return result


def _indexed(con: Connection, app: apps.RegisteredApp) -> tuple[int | None, dict[str, float | None]]:
    executables: dict[str, float | None] = {}
    measured = None
    location = app.install_location
    if (
        location
        and location != ntpath.splitdrive(location)[0] + "\\"
        and not (ntpath.dirname(location) == ntpath.splitdrive(location)[0] + "\\" and is_protected(location))
    ):
        column, location_key = path_comparison("path", location)
        directory = con.execute(
            f"SELECT id,total_size FROM dirs WHERE {column}=? COLLATE NOCASE ORDER BY id DESC LIMIT 1",
            (location_key,),
        ).fetchone()
        if directory is not None:
            measured = directory["total_size"]
            rows = con.execute(
                """
                WITH RECURSIVE subtree(id) AS (
                    SELECT ? UNION ALL SELECT d.id FROM dirs d JOIN subtree p ON d.parent_id=p.id
                )
                SELECT d.path,f.name,f.atime FROM subtree JOIN dirs d USING(id)
                JOIN files f ON f.dir_id=d.id WHERE f.ext='exe'
                """,
                (directory["id"],),
            )
        else:
            condition, args = root_filter("d.path", location)
            rows = con.execute(
                f"SELECT d.path,f.name,f.atime FROM files f JOIN dirs d ON d.id=f.dir_id "
                f"WHERE f.ext='exe' AND {condition}",
                args,
            )
        for path, name, atime in rows:
            executable = ntpath.join(path, name)
            previous = executables.get(executable)
            executables[executable] = max(previous or 0, atime or 0) or None
    if app.display_icon:
        path, name = ntpath.split(app.display_icon)
        column, key = path_comparison("d.path", path)
        entry = con.execute(
            f"SELECT f.atime FROM dirs d JOIN files f ON f.dir_id=d.id WHERE {column}=? COLLATE NOCASE "
            "AND pci_casefold(f.name)=? ORDER BY f.id DESC LIMIT 1",
            (key, name.casefold()),
        ).fetchone()
        executables.setdefault(app.display_icon, entry["atime"] if entry else None)
    return measured, executables


def _signals(
    executables: dict[str, float | None],
    names: dict[str, set[str]],
    prefetch: dict[str, Usage],
    user_assist: dict[str, Usage],
) -> list[Usage]:
    best: dict[str, Usage] = {}
    for path, atime in executables.items():
        name = ntpath.basename(path).casefold()
        fetched = prefetch.get(name)
        signals = (
            Usage(name, path, fetched.last_run, None, "prefetch")
            if fetched and len(names[name]) == 1
            else None,
            user_assist.get(path.casefold()),
            Usage(name, path, atime, None, "atime") if atime and atime > 0 else None,
        )
        for evidence in signals:
            if evidence is not None and (
                evidence.source not in best or evidence.last_run > best[evidence.source].last_run
            ):
                best[evidence.source] = evidence
    return list(best.values())


def collect(con: Connection, cancel: Event) -> tuple[int, list[str]]:
    inventory = apps.read_registry(cancel)
    _check(cancel)
    prefetch = read_prefetch(config.WINDOWS_DIR / "Prefetch", inventory.notes, cancel)
    user_assist = read_user_assist(inventory.notes, cancel)
    names: dict[str, set[str]] = defaultdict(set)
    prepared = []
    with con:
        con.execute("BEGIN")
        for app in inventory.apps:
            _check(cancel)
            measured, executables = _indexed(con, app)
            for path in executables:
                names[ntpath.basename(path).casefold()].add(path.casefold())
            prepared.append((app, measured, executables))

    with con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "CREATE TEMP TABLE IF NOT EXISTS collected_apps(source TEXT, reg_key TEXT COLLATE NOCASE, PRIMARY KEY(source,reg_key))"
        )
        con.execute("DELETE FROM collected_apps")
        for app, measured, executables in prepared:
            _check(cancel)
            params = {**asdict(app), "measured_size": measured}
            [app_id] = con.execute(
                """
                INSERT INTO apps(source,reg_key,name,publisher,version,install_date,install_location,
                    display_icon,estimated_size,measured_size,uninstall_cmd,quiet_uninstall_cmd)
                VALUES (:source,:reg_key,:name,:publisher,:version,:install_date,:install_location,
                    :display_icon,:estimated_size,:measured_size,:uninstall_cmd,:quiet_uninstall_cmd)
                ON CONFLICT(source,reg_key) DO UPDATE SET name=excluded.name,publisher=excluded.publisher,
                    version=excluded.version,install_date=excluded.install_date,install_location=excluded.install_location,
                    display_icon=excluded.display_icon,estimated_size=excluded.estimated_size,measured_size=excluded.measured_size,
                    uninstall_cmd=excluded.uninstall_cmd,quiet_uninstall_cmd=excluded.quiet_uninstall_cmd
                RETURNING id
                """,
                params,
            ).fetchone()
            con.execute("INSERT INTO collected_apps VALUES (?,?)", (app.source, app.reg_key))
            con.execute("DELETE FROM app_usage WHERE app_id=?", (app_id,))
            con.executemany(
                "INSERT INTO app_usage(app_id,exe_name,exe_path,last_run,run_count,source) VALUES (?,?,?,?,?,?)",
                (
                    (app_id, item.exe_name, item.exe_path, item.last_run, item.run_count, item.source)
                    for item in _signals(executables, names, prefetch, user_assist)
                ),
            )
        for source in inventory.complete:
            con.execute(
                "DELETE FROM apps WHERE source=? AND NOT EXISTS (SELECT 1 FROM collected_apps c "
                "WHERE c.source=apps.source AND c.reg_key=apps.reg_key COLLATE NOCASE)",
                (source,),
            )
        _check(cancel)
        metadata = {
            "complete": len(inventory.complete) == len(apps.REGISTRY_VIEWS),
            "notes": inventory.notes,
            "at": time.time(),
            "roots": {
                name: path
                for name, value in config.app_data_roots().items()
                if (path := apps.local_path(value))
            },
        }
        con.execute(
            "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (COLLECTION_KEY, json.dumps(metadata, ensure_ascii=False)),
        )
        count = con.execute("SELECT count(*) FROM apps").fetchone()[0]
    return count, inventory.notes
