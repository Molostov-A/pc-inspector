"""Отчёт о полных совпадениях из кэша хэшей SQLite."""

from __future__ import annotations

import os
import sqlite3
from typing import ClassVar

from ..core.models import Finding, Report
from ..core.scanner import CLOUD, REPARSE
from .base import P_LIMIT, P_ROOT, Analyzer, Param, index_path, register, root_filter

_MAX_COPIES = 200
_START_HINT = "Запустите «Найти дубликаты» в интерфейсе или команду hash для выбранного корня и размера."
_CURRENT = """
FROM file_hashes h
JOIN files f ON f.id=(
    SELECT candidate.id FROM dirs parent JOIN files candidate ON candidate.dir_id=parent.id
    WHERE parent.path=h.directory COLLATE NOCASE AND candidate.name=h.name COLLATE NOCASE
    ORDER BY candidate.id DESC LIMIT 1
)
JOIN dirs d ON d.id=f.dir_id
WHERE h.full_hash IS NOT NULL AND d.protected=0 AND (f.attrs & ?)=0
    AND f.size=h.size AND f.mtime=h.mtime AND f.ctime=h.ctime
"""
_PREFERENCE = r"""
    ctime,
    CASE WHEN path LIKE '%\Downloads\%' OR path LIKE '%\Desktop\%' OR path LIKE '%\Temp\%'
        THEN 1 ELSE 0 END,
    path COLLATE NOCASE, path
"""


def _run_notes(con: sqlite3.Connection, root: str, min_size: int) -> list[str]:
    requested = root.casefold()
    latest_scan, roots = con.execute("SELECT max(CAST(scanned_at AS REAL)),count(*) FROM roots").fetchone()
    if not roots:
        return ["Индекс пуст. Сначала просканируйте нужные папки или диски."]
    runs = con.execute(
        "SELECT root,errors,started_at FROM hash_runs WHERE min_size<=? ORDER BY started_at DESC",
        (min_size,),
    )
    for run in runs:
        scope = run["root"].casefold()
        prefix = scope.rstrip(os.sep) + os.sep
        if not scope or requested == scope or requested.startswith(prefix):
            notes = []
            if latest_scan is not None and run["started_at"] < latest_scan:
                notes.append(
                    "Индекс обновился после расчёта хэшей. Для полной проверки запустите поиск заново."
                )
            if run["errors"]:
                notes.append(f"При последнем поиске пропущены файлы с ошибками: {run['errors']}.")
            return notes
    return [_START_HINT]


@register
class Duplicates(Analyzer):
    id = "duplicates"
    title = "Дубликаты файлов"
    description = "Группы файлов с одинаковым полным хэшем. Сначала запустите хэширование."
    params: ClassVar[list[Param]] = [
        P_ROOT,
        Param("min_size", "size", "1M", "Минимальный размер файла"),
        P_LIMIT,
    ]

    def run(self, con: sqlite3.Connection, root: str, min_size: int, limit: int) -> Report:
        report = Report(self.id, self.title)
        root = os.path.abspath(index_path(root)) if root else ""
        if (
            con.execute("SELECT 1 FROM sqlite_schema WHERE name='file_hashes' AND type='table'").fetchone()
            is None
        ):
            report.notes.append(_START_HINT)
            return report
        report.notes.extend(_run_notes(con, root, min_size))
        condition, args = root_filter("h.path", root)
        groups = con.execute(
            f"""
            SELECT h.size,h.full_hash,count(DISTINCT h.identity) AS copies
            {_CURRENT} AND h.size>=? AND {condition}
            GROUP BY h.size,h.full_hash HAVING copies>1
            ORDER BY h.size*(copies-1) DESC,h.full_hash LIMIT ?
            """,
            (CLOUD | REPARSE, min_size, *args, limit),
        ).fetchall()
        for group in groups:
            rows = con.execute(
                f"""
                WITH valid AS (
                    SELECT h.path,h.name,h.mtime,h.ctime,h.identity {_CURRENT}
                    AND h.size=? AND h.full_hash=? AND {condition}
                ), distinct_objects AS (
                    SELECT *,row_number() OVER (PARTITION BY identity ORDER BY {_PREFERENCE}) AS position
                    FROM valid
                )
                SELECT path,name,mtime,ctime FROM distinct_objects WHERE position=1
                ORDER BY {_PREFERENCE} LIMIT ?
                """,
                (CLOUD | REPARSE, group["size"], group["full_hash"], *args, _MAX_COPIES),
            ).fetchall()
            if not rows:
                continue
            copies = [{**dict(row), "keep_suggested": index == 0} for index, row in enumerate(rows)]
            report.findings.append(
                Finding(
                    "group",
                    rows[0]["name"],
                    group["size"] * (group["copies"] - 1),
                    safety="review",
                    category="duplicates",
                    reason=f"{group['copies']} независимых копий с одинаковым содержимым",
                    meta={"copies": copies, "count": group["copies"], "file_size": group["size"]},
                )
            )
        if any(item.meta["count"] > _MAX_COPIES for item in report.findings):
            report.notes.append(f"В одной группе показаны до {_MAX_COPIES} путей.")
        return report
