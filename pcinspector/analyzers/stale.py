"""Давно не используемые файлы и «забытые» папки.

Оговорка: Windows часто не обновляет дату последнего доступа (atime), а антивирусы,
наоборот, могут её обновлять. Поэтому «последнее использование» = max(mtime, atime) —
это осторожная оценка: лучше пропустить забытый файл, чем пометить нужный.
"""

from __future__ import annotations

import sqlite3
import time
from typing import ClassVar

from ..core.fmt import DAY, age_days, ts_date
from ..core.models import Finding, Report
from .base import P_LIMIT, P_MIN_SIZE, P_ROOT, Analyzer, Param, register, root_filter
from .rules import category

P_DAYS = Param("days", "int", 365, "Сколько дней без изменений считать «давно»", setting="stale_days")
NOTE_ATIME = "Дата доступа на Windows ненадёжна; использовалась max(дата изменения, дата доступа)."


@register
class StaleFiles(Analyzer):
    id = "stale_files"
    title = "Крупные файлы, давно не использовавшиеся"
    description = "Большие файлы вне системных папок, которые не менялись и не открывались N дней."
    params: ClassVar[list[Param]] = [P_DAYS, P_MIN_SIZE, P_ROOT, P_LIMIT]

    def run(self, con: sqlite3.Connection, days: int, min_size: int, root: str, limit: int) -> Report:
        rep = Report(self.id, self.title, notes=[NOTE_ATIME])
        cutoff = time.time() - days * DAY
        cond, args = root_filter("path", root)
        sql = (
            f"SELECT * FROM files_v WHERE size >= ? AND protected = 0 "
            f"AND MAX(COALESCE(mtime,0), COALESCE(atime,0)) < ? AND {cond} "
            f"ORDER BY size DESC LIMIT ?"
        )
        for f in con.execute(sql, (min_size, cutoff, *args, limit)):
            last = max(f["mtime"] or 0, f["atime"] or 0)
            rep.findings.append(
                Finding(
                    "file",
                    f["name"],
                    f["size"],
                    f["path"],
                    safety="review",
                    category=category(f["ext"]),
                    reason=f"не использовался {age_days(last)} дн. (с {ts_date(last)})",
                    meta={"mtime": f["mtime"], "atime": f["atime"], "ext": f["ext"]},
                )
            )
        return rep


@register
class ForgottenDirs(Analyzer):
    id = "forgotten_dirs"
    title = "Забытые папки"
    description = (
        "Папки вне системных зон, в которых ни один файл не менялся N дней. "
        "Показывается самая верхняя такая папка, вложенные не дублируются."
    )
    params: ClassVar[list[Param]] = [P_DAYS, P_MIN_SIZE, P_ROOT, P_LIMIT]

    def run(self, con: sqlite3.Connection, days: int, min_size: int, root: str, limit: int) -> Report:
        rep = Report(self.id, self.title, notes=["Учитывается дата изменения файлов (mtime)."])
        cutoff = time.time() - days * DAY
        cond, args = root_filter("d.path", root)
        parent_cond, parent_args = root_filter("p.path", root)
        sql = f"""
            SELECT d.* FROM dirs d LEFT JOIN dirs p ON p.id = d.parent_id
            WHERE d.depth > 0 AND d.protected = 0 AND d.total_size >= ? AND d.newest_mtime < ?
              AND NOT (p.depth > 0 AND p.protected = 0 AND p.total_size >= ?
                       AND p.newest_mtime < ? AND {parent_cond})
              AND {cond}
            ORDER BY d.total_size DESC LIMIT ?"""
        for d in con.execute(sql, (min_size, cutoff, min_size, cutoff, *parent_args, *args, limit)):
            rep.findings.append(
                Finding(
                    "dir",
                    d["name"],
                    d["total_size"],
                    d["path"],
                    safety="review",
                    reason=f"последнее изменение {ts_date(d['newest_mtime'])} "
                    f"({age_days(d['newest_mtime'])} дн. назад), {d['total_files']:,} файлов",
                    meta={"newest_mtime": d["newest_mtime"], "files": d["total_files"]},
                )
            )
        return rep
