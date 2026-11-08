"""Сравнение размеров папок между двумя последовательными снимками."""

from __future__ import annotations

import sqlite3
from typing import ClassVar

from ..core import db
from ..core.models import Finding, Report
from .base import P_LIMIT, P_ROOT, Analyzer, Param, register, root_filter


@register
class Growth(Analyzer):
    id = "growth"
    title = "Рост папок"
    description = "Показывает, какие папки сильнее всего выросли или уменьшились с прошлого скана."
    params: ClassVar[list[Param]] = [
        P_ROOT,
        P_LIMIT,
        Param("min_delta", "size", "500M", "Минимальное изменение в любую сторону"),
        Param("since", "choice", "last", "Снимок для сравнения", ("last",)),
    ]

    def run(self, con: sqlite3.Connection, *, root: str, limit: int, min_delta: int, since: str) -> Report:
        report = Report(self.id, self.title)
        if not db.has_table(con, "snapshots"):
            report.notes.append("Нет двух снимков одного корня. Просканируйте его повторно.")
            return report
        scope, values = root_filter("path", root)
        rows = con.execute(
            f"""
            WITH ranked AS (
                SELECT *, row_number() OVER (PARTITION BY root_key ORDER BY taken_at DESC, id DESC) AS rn
                FROM snapshots
            ), pairs AS (
                SELECT a.id AS latest, b.id AS previous, a.taken_at AS latest_at, b.taken_at AS previous_at,
                       a.error_count + b.error_count AS errors
                FROM ranked a JOIN ranked b ON b.root_key=a.root_key AND b.rn=2 WHERE a.rn=1
            ), changes AS (
                SELECT c.path, c.total_size AS after, b.total_size AS before, p.*
                FROM pairs p JOIN snapshot_dirs c ON c.snapshot_id=p.latest
                LEFT JOIN snapshot_dirs b ON b.snapshot_id=p.previous AND b.path_key=c.path_key
                UNION ALL
                SELECT b.path, NULL, b.total_size, p.*
                FROM pairs p JOIN snapshot_dirs b ON b.snapshot_id=p.previous
                LEFT JOIN snapshot_dirs c ON c.snapshot_id=p.latest AND c.path_key=b.path_key
                WHERE c.path_key IS NULL
            ), deltas AS (
                SELECT *, coalesce(after,0)-coalesce(before,0) AS delta FROM changes WHERE {scope}
            )
            SELECT * FROM deltas WHERE delta != 0 AND abs(delta)>=?
            ORDER BY abs(delta) DESC, path LIMIT ?
            """,
            (*values, min_delta, limit),
        )
        for row in rows:
            report.findings.append(
                Finding(
                    "dir",
                    row["path"],
                    abs(row["delta"]),
                    row["path"],
                    reason="Папка выросла" if row["delta"] > 0 else "Папка уменьшилась",
                    meta={
                        "delta": row["delta"],
                        "before_size": row["before"],
                        "after_size": row["after"],
                        "previous_at": row["previous_at"],
                        "latest_at": row["latest_at"],
                    },
                )
            )
            if row["errors"] and "В сравниваемых сканах были ошибки чтения." not in report.notes:
                report.notes.append("В сравниваемых сканах были ошибки чтения.")
        report.notes.append(
            "Для сравнения нужны два скана одного корня. Снимки содержат папки до четвёртого уровня "
            "от 50 МиБ. Если папка есть только в одном снимке, изменение размера приблизительное."
        )
        return report
