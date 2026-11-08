"""Запуск анализаторов, содержимое папок и настройки."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import asdict
from typing import Any

from ..analyzers import REGISTRY, get
from ..analyzers.overview import directory_finding, find_directory
from ..core import settings
from ..core.models import Finding, Report
from .workspace import DbPath, read, state


def list_analyzers() -> list[dict[str, Any]]:
    return [
        {
            "id": analyzer.id,
            "title": analyzer.title,
            "description": analyzer.description,
            "params": [asdict(param) for param in analyzer.params],
        }
        for analyzer in REGISTRY.values()
    ]


def analyzer_catalog() -> list[dict[str, Any]]:
    """Добавить JSON Schema к метаданным каждого анализатора."""
    types = {"int": "integer", "str": "string", "bool": "boolean", "choice": "string"}
    entries = list_analyzers()
    for entry in entries:
        properties = {}
        for param in entry["params"]:
            kind = param["type"]
            schema = {"description": param["help"], "default": param["default"]}
            if kind == "size":
                schema["anyOf"] = [{"type": "integer", "minimum": 0}, {"type": "string"}]
            else:
                schema["type"] = types[kind]
            if kind == "choice":
                schema["enum"] = param["choices"]
            properties[param["name"]] = schema
        entry["schema"] = {"type": "object", "properties": properties, "additionalProperties": False}
    return entries


def run_analyzer(analyzer_id: str, *, db_path: DbPath = None, **params: Any) -> Report:
    analyzer = get(analyzer_id)
    with read(db_path) as con:
        return analyzer(con, **params)


def get_settings(*, db_path: DbPath = None) -> settings.Settings:
    with read(db_path) as con:
        return settings.load(con)


def save_settings(value: settings.Settings, *, db_path: DbPath = None) -> settings.Settings:
    with state(db_path) as con:
        return settings.save(con, value)


def list_roots(*, db_path: DbPath = None) -> Report:
    return run_analyzer("roots", db_path=db_path)


def tree(path: str, limit: int = 100, *, db_path: DbPath = None) -> Report:
    return run_analyzer("tree", path=path, limit=limit, db_path=db_path)


def _folder_files(con: sqlite3.Connection, directory_id: int, limit: int) -> Iterable[sqlite3.Row]:
    return con.execute(
        "SELECT name, path, size, mtime, protected FROM files_v WHERE dir_id = ? "
        "ORDER BY size DESC, name LIMIT ?",
        (directory_id, limit),
    )


def folder_contents(path: str, limit: int, *, db_path: DbPath = None) -> Report:
    """Дочерние папки и крупнейшие файлы непосредственно в выбранной папке."""
    with read(db_path) as con:
        report = get("tree")(con, path=path, limit=limit)
        parent = find_directory(con, path) if path else None
        if parent is None:
            return report
        report.findings = [item for item in report.findings if item.kind == "dir"]
        for row in _folder_files(con, parent["id"], limit):
            report.findings.append(
                Finding(
                    "file",
                    row["name"],
                    row["size"],
                    row["path"],
                    safety="keep" if row["protected"] else "info",
                    meta={"files": 1, "newest_mtime": row["mtime"]},
                )
            )
        if parent["own_files"] > limit:
            report.notes.append(f"Показаны {limit} крупнейших файлов из {parent['own_files']}.")
        return report


def largest_folders(*, db_path: DbPath = None) -> Report:
    """Пять крупнейших дочерних папок просканированных корней."""
    with read(db_path) as con:
        rows = con.execute(
            "SELECT * FROM dirs WHERE depth = 1 AND total_size > 0 ORDER BY total_size DESC, path LIMIT 5"
        )
        return Report("tree", "Крупные папки", findings=[directory_finding(row) for row in rows])
