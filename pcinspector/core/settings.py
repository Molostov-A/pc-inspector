"""Настройки пользователя, общие для сканера, отчётов и интерфейсов."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import TypedDict

from .. import config
from . import db, safety

DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


class Settings(TypedDict):
    scan_roots: list[str]
    exclude_paths: list[str]
    projects_root: str
    stale_days: int
    schedule_enabled: bool
    schedule_day: str
    schedule_time: str
    quarantine_path: str


def load(con: sqlite3.Connection) -> Settings:
    result = Settings(
        scan_roots=[],
        exclude_paths=[],
        projects_root="",
        stale_days=365,
        schedule_enabled=False,
        schedule_day="SUN",
        schedule_time="03:00",
        quarantine_path="",
    )
    if db.has_table(con, "settings"):
        for key, value in con.execute(
            "SELECT key,value FROM settings WHERE key IN (?,?,?,?,?,?,?,?)",
            tuple(result),
        ):
            result[key] = json.loads(value)
    return result


def quarantine_path(con: sqlite3.Connection) -> Path | None:
    if config.QUARANTINE_DIR is not None:
        return config.QUARANTINE_DIR
    row = con.execute("SELECT value FROM settings WHERE key='quarantine_path'").fetchone()
    value = json.loads(row[0]) if row else ""
    return Path(value) if value else None


def save(con: sqlite3.Connection, value: Settings) -> Settings:
    if not 1 <= value["stale_days"] <= 3650:
        raise ValueError("Укажите от 1 до 3650 дней для давних файлов")
    if value["schedule_day"] not in DAYS or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d", value["schedule_time"]
    ):
        raise ValueError("Укажите день недели и время в формате ЧЧ:ММ")
    if len(value["scan_roots"]) > 100 or len(value["exclude_paths"]) > 500:
        raise ValueError("Допустимо до 100 корней и 500 исключений")
    result = Settings(**value)
    for name in ("scan_roots", "exclude_paths"):
        paths: dict[str, str] = {}
        for raw in value[name]:
            path = safety.settings_path(raw, allow_relative=name == "exclude_paths")
            paths.setdefault(path.casefold(), path)
        result[name] = list(paths.values())
    if value["projects_root"]:
        result["projects_root"] = safety.settings_path(value["projects_root"])
    if value["quarantine_path"]:
        result["quarantine_path"] = str(
            safety.checked_action_path(
                value["quarantine_path"],
                destination=True,
                managed=True,
            )
        )
    with con:
        con.executemany(
            "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ((key, json.dumps(item, ensure_ascii=False)) for key, item in result.items()),
        )
    return result
