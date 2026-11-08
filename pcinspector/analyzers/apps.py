"""Отчёты об использовании программ и папках без найденного владельца."""

from __future__ import annotations

import json
import ntpath
import re
import sqlite3
import time
from difflib import SequenceMatcher
from typing import Any, ClassVar

from ..core.fmt import DAY
from ..core.models import Finding, Report
from .base import P_LIMIT, P_ROOT, Analyzer, Param, register, root_filter
from .rules import APP_LEFTOVER_SYSTEM_NAMES

_COLLECTION_HINT = "Сначала соберите сведения о программах кнопкой «Обновить список» или командой apps."
_CONFIDENCE = {"prefetch": "high", "userassist": "medium", "atime": "low"}
_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "gmbh", "limited"}


def _collection(con: sqlite3.Connection) -> dict[str, Any] | None:
    if con.execute("SELECT 1 FROM sqlite_schema WHERE name='apps'").fetchone() is None:
        return None
    row = con.execute("SELECT value FROM settings WHERE key=?", ("apps_collection",)).fetchone()
    return json.loads(row["value"]) if row else None


@register
class UnusedApps(Analyzer):
    id = "unused_apps"
    title = "Неиспользуемые программы"
    description = "Программы без признаков недавнего запуска. 0 дней показывает весь собранный список."
    params: ClassVar[list[Param]] = [
        Param("days", "int", 180, "Сколько дней без запуска учитывать (0 — все программы)"),
        P_LIMIT,
    ]

    def run(self, con: sqlite3.Connection, days: int, limit: int) -> Report:
        report = Report(self.id, self.title)
        collected = _collection(con)
        if collected is None:
            report.notes.append(_COLLECTION_HINT)
            return report
        report.notes.extend(collected["notes"])
        report.notes.append(
            "Дата доступа к EXE — косвенный признак. Отсутствие даты запуска не доказывает, что программа не используется."
        )
        report.summary = {"collected_at": collected["at"]}
        cutoff = time.time() - days * DAY
        rows = con.execute(
            """
            WITH inventory AS (
                SELECT *,row_number() OVER (
                    PARTITION BY reg_key,name,ifnull(version,''),ifnull(install_location,''),ifnull(uninstall_cmd,'')
                    ORDER BY CASE source WHEN 'hklm' THEN 0 WHEN 'hklm_wow' THEN 1 ELSE 2 END, id
                ) AS copy
                FROM apps WHERE is_system_component=0
            )
            SELECT a.*,u.last_run,u.run_count,u.source AS evidence,u.exe_name
            FROM inventory a LEFT JOIN app_usage u ON u.app_id=a.id AND u.source=(
                SELECT source FROM app_usage WHERE app_id=a.id
                ORDER BY CASE source WHEN 'prefetch' THEN 0 WHEN 'userassist' THEN 1 ELSE 2 END LIMIT 1
            )
            WHERE a.copy=1 AND (
                ?=0 OR ((u.last_run IS NULL OR u.last_run<=?) AND (a.install_date IS NULL OR a.install_date<=?))
            )
            ORDER BY coalesce(nullif(a.measured_size,0),a.estimated_size,0) DESC,a.name COLLATE NOCASE,a.id
            LIMIT ?
            """,
            (days, cutoff, cutoff, limit),
        )
        for row in rows:
            last_run = row["last_run"]
            reason = (
                f"Последний признак запуска {max(0, int((time.time() - last_run) // DAY))} дн. назад"
                if last_run is not None
                else "Дата последнего запуска неизвестна"
            )
            report.findings.append(
                Finding(
                    "app",
                    row["name"],
                    row["measured_size"] or row["estimated_size"] or 0,
                    row["install_location"],
                    safety="review",
                    reason=reason,
                    meta={
                        "app_id": row["id"],
                        "publisher": row["publisher"],
                        "version": row["version"],
                        "last_run": last_run,
                        "run_count": row["run_count"],
                        "confidence": _CONFIDENCE.get(row["evidence"], "low"),
                        "source": row["evidence"],
                        "exe_name": row["exe_name"],
                        "uninstall_cmd": row["uninstall_cmd"],
                        "can_uninstall": bool(
                            row["uninstall_cmd"] and row["uninstall_cmd"] != row["quiet_uninstall_cmd"]
                        ),
                    },
                )
            )
        return report


def _normalize(value: str) -> str:
    words = re.findall(r"[^\W_]+", value.casefold())
    return "".join(word for word in words if word not in _SUFFIXES and not word.isdigit())


def _matches(name: str, known: set[str]) -> bool:
    if not name or name in known:
        return True
    return any(
        2 * min(len(name), len(candidate)) >= 0.8 * (len(name) + len(candidate))
        and SequenceMatcher(None, name, candidate, autojunk=False).ratio() >= 0.8
        for candidate in known
    )


@register
class AppLeftovers(Analyzer):
    id = "app_leftovers"
    title = "Возможные остатки программ"
    description = (
        "Папки первого уровня в AppData и ProgramData без совпадения с названием или издателем программы."
    )
    params: ClassVar[list[Param]] = [
        P_ROOT,
        Param("min_size", "size", 0, "Минимальный размер папки"),
        P_LIMIT,
    ]

    def run(self, con: sqlite3.Connection, root: str, min_size: int, limit: int) -> Report:
        report = Report(self.id, self.title)
        collected = _collection(con)
        if collected is None:
            report.notes.append(_COLLECTION_HINT)
            return report
        if not collected["complete"]:
            report.notes.append("Список программ неполный. Остатки не оценивались.")
            return report
        report.notes.append(
            "Сопоставление по имени приблизительное. Проверьте содержимое папки перед действием."
        )
        known = set()
        for row in con.execute("SELECT name,publisher,install_location FROM apps"):
            for value in (row["name"], row["publisher"], ntpath.basename(row["install_location"] or "")):
                if value and (normalized := _normalize(value)):
                    known.add(normalized)
        excluded = {_normalize(name) for name in APP_LEFTOVER_SYSTEM_NAMES}
        candidates = {}
        for source, base in collected["roots"].items():
            data_filter, data_args = root_filter("path", base)
            scope_filter, scope_args = root_filter("path", root)
            # Условие SQL оставляет только первый уровень даже при явном скане отдельной папки.
            separator = len(base.rstrip("\\")) + 2
            rows = con.execute(
                f"SELECT path,name,total_size,protected FROM dirs WHERE total_size>=? AND {data_filter} "
                f"AND {scope_filter} AND length(path)>=? AND instr(substr(path,?),?)=0 "
                "ORDER BY total_size DESC,id DESC",
                (min_size, *data_args, *scope_args, separator, separator, "\\"),
            )
            added = 0
            for row in rows:
                path = row["path"]
                name = _normalize(row["name"])
                if path.casefold() in candidates or name in excluded or _matches(name, known):
                    continue
                candidates[path.casefold()] = Finding(
                    "dir",
                    row["name"],
                    row["total_size"],
                    path,
                    safety="keep" if source == "PROGRAMDATA" or row["protected"] else "review",
                    reason="Не найдено соответствие установленной программе или издателю",
                    meta={"data_root": source},
                )
                added += 1
                if added >= limit:
                    break
        report.findings = sorted(candidates.values(), key=lambda item: item.size, reverse=True)[:limit]
        return report
