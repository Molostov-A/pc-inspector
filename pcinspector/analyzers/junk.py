"""Поиск кэшей, временных файлов и других кандидатов по правилам из rules.py."""

from __future__ import annotations

import heapq
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..core.models import Finding, Report
from .base import P_LIMIT, P_ROOT, Analyzer, Param, register, root_filter
from .rules import DIR_NAME_RULES, DIR_PATH_RULES, FILE_RULES

# При совпадении пути побеждает правило, стоящее раньше: точные пути идут перед именами.
RULES = (*DIR_PATH_RULES, *DIR_NAME_RULES, *FILE_RULES)
DIR_COLUMNS = "d.path, d.name, d.total_size AS size, d.newest_mtime AS mtime, d.total_files AS files"


@dataclass
class _Total:
    size: int = 0
    count: int = 0
    top: list[tuple[int, int, Finding]] = field(default_factory=list)

    def add(self, item: Finding, limit: int) -> None:
        self.size += item.size
        self.count += 1
        entry = (item.size, -self.count, item)
        if len(self.top) < limit:
            heapq.heappush(self.top, entry)
        else:
            heapq.heappushpop(self.top, entry)

    def largest(self) -> list[Finding]:
        return [entry[2] for entry in sorted(self.top, reverse=True)]


def _candidates(con: sqlite3.Connection, root: str, min_size: int) -> Iterator[tuple[int, Finding]]:
    queries: list[str] = []
    args: list[Any] = []
    dir_condition, dir_args = root_filter("d.path", root)
    file_condition, file_args = root_filter("path", root)
    directory_query = (
        f"SELECT ? AS rule, 1 AS is_dir, {DIR_COLUMNS} FROM dirs d "
        f"WHERE d.protected = 0 AND d.total_size >= ? AND {dir_condition}"
    )

    for index, rule in enumerate(RULES):
        if "exts" in rule:
            marks = ",".join("?" * len(rule["exts"]))
            sql = (
                "SELECT ? AS rule, 0 AS is_dir, path, name, size, mtime, NULL AS files FROM files_v "
                f"WHERE protected = 0 AND size >= ? AND size > 0 AND {file_condition} "
                f"AND ext IN ({marks}) AND path LIKE ?"
            )
            values = [index, min_size, *file_args, *rule["exts"], rule["dir_like"]]
        else:
            sql = directory_query
            values = [index, min_size, *dir_args]
            if "like" in rule:
                sql += " AND d.path LIKE ?"
                values.append(rule["like"])
            else:
                marks = ",".join("?" * len(rule["names"]))
                sql += f" AND d.name COLLATE NOCASE IN ({marks})"
                values.extend(rule["names"])
            if "path_like" in rule:
                sql += " AND d.path LIKE ?"
                values.append(rule["path_like"])
            if "require_file" in rule:
                sql += (
                    " AND EXISTS (SELECT 1 FROM files f WHERE f.dir_id = d.id AND f.name = ? COLLATE NOCASE)"
                )
                values.append(rule["require_file"])
            if "sibling_ext" in rule:
                marks = ",".join("?" * len(rule["sibling_ext"]))
                sql += (
                    f" AND EXISTS (SELECT 1 FROM files f WHERE f.dir_id = d.parent_id AND f.ext IN ({marks}))"
                )
                values.extend(rule["sibling_ext"])
        queries.append(sql)
        args.extend(values)

    # Разделитель сортируется раньше символов имён: поддерево идёт сразу после своей папки.
    union = " UNION ALL ".join(queries)
    rows = con.execute(
        f"SELECT * FROM ({union}) ORDER BY replace(pci_casefold(path), ?, char(1)), rule, size DESC",
        (*args, os.sep),
    )
    previous = prefix = ""
    for row in rows:
        path = row["path"].casefold()
        if path == previous or (prefix and path.startswith(prefix)):
            continue
        previous = path
        rule = RULES[row["rule"]]
        meta = {"hint": rule["hint"], "action": rule.get("action", "recycle")}
        if row["is_dir"]:
            prefix = path.rstrip(os.sep) + os.sep
            meta.update(files=row["files"], newest_mtime=row["mtime"])
        else:
            meta["mtime"] = row["mtime"]
        yield (
            row["rule"],
            Finding(
                "dir" if row["is_dir"] else "file",
                row["name"],
                row["size"],
                row["path"],
                safety=rule["safety"],
                category=rule["id"],
                reason=rule["title"],
                meta=meta,
            ),
        )


@register
class Junk(Analyzer):
    id = "junk"
    title = "Мусор: кэши, временные файлы, зависимости"
    description = "Находит кэши, временные файлы, зависимости, дампы и установщики в Загрузках."
    params: ClassVar[list[Param]] = [
        P_ROOT,
        Param("min_size", "size", "1M", "Не показывать находки меньше"),
        P_LIMIT,
        Param("group", "bool", True, "Сгруппировать по правилам (false — список путей)"),
    ]

    def run(self, con: sqlite3.Connection, root: str, min_size: int, limit: int, group: bool) -> Report:
        report = Report(self.id, self.title, summary={"safe_size": 0, "review_size": 0, "items": 0})
        flat = _Total()
        groups: dict[int, _Total] = {}
        for index, item in _candidates(con, root, min_size):
            report.summary[f"{item.safety}_size"] += item.size
            report.summary["items"] += 1
            if group:
                groups.setdefault(index, _Total()).add(item, 10)
            else:
                flat.add(item, limit)

        if not group:
            report.findings = flat.largest()
            return report
        for index, total in sorted(groups.items(), key=lambda pair: pair[1].size, reverse=True)[:limit]:
            rule = RULES[index]
            report.findings.append(
                Finding(
                    "group",
                    rule["title"],
                    total.size,
                    safety=rule["safety"],
                    category=rule["id"],
                    reason=f"{total.count} шт. — {rule['hint']}",
                    meta={
                        "count": total.count,
                        "top": [
                            {"path": item.path, "size": item.size, "action": item.meta["action"]}
                            for item in total.largest()
                        ],
                    },
                )
            )
        return report
