"""Общий фасад сканирования, анализа и чтения индекса для интерфейсов."""

from __future__ import annotations

import math
import os
import re
import sqlite3
import subprocess
from collections.abc import Iterable
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

from . import config
from .analyzers import REGISTRY, get
from .analyzers.base import index_path, path_comparison
from .analyzers.overview import directory_finding, find_directory
from .core import db, jobs, safety, scanner
from .core.models import Finding, Report


def _db_path(db_path: Path | str | None) -> Path:
    return Path(db_path if db_path is not None else os.environ.get("PCI_DB", config.DB_PATH)).absolute()


def start_scan(paths: list[str], *, db_path: Path | str | None = None) -> str:
    roots = {}
    for path in paths or scanner.local_ntfs_roots():
        root = scanner.normalize_root(path)
        roots.setdefault(root.casefold(), root)
    if not roots:
        raise ValueError("Нет доступных локальных дисков NTFS. Укажите папку сканирования.")
    return jobs.start_scan(list(roots.values()), _db_path(db_path))


def job_status(job_id: str) -> jobs.JobStatus:
    return jobs.job_status(job_id)


def cancel_job(job_id: str) -> bool:
    return jobs.cancel_job(job_id)


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


def _read_index(db_path: Path | str | None) -> sqlite3.Connection:
    path = _db_path(db_path)
    try:
        return db.connect(path, readonly=True)
    except sqlite3.OperationalError:
        if path.exists():
            raise
        return db.empty_index()


def run_analyzer(analyzer_id: str, *, db_path: Path | str | None = None, **params: Any) -> Report:
    analyzer = get(analyzer_id)
    with closing(_read_index(db_path)) as con:
        return analyzer(con, **params)


def list_roots(*, db_path: Path | str | None = None) -> Report:
    return run_analyzer("roots", db_path=db_path)


def tree(path: str, limit: int = 100, *, db_path: Path | str | None = None) -> Report:
    return run_analyzer("tree", path=path, limit=limit, db_path=db_path)


def _folder_files(con: sqlite3.Connection, directory_id: int, limit: int) -> Iterable[sqlite3.Row]:
    return con.execute(
        "SELECT name, path, size, mtime, protected FROM files_v WHERE dir_id = ? "
        "ORDER BY size DESC, name LIMIT ?",
        (directory_id, limit),
    )


def folder_contents(path: str, limit: int, *, db_path: Path | str | None = None) -> Report:
    """Дочерние папки и крупнейшие файлы непосредственно в выбранной папке."""
    with closing(_read_index(db_path)) as con, con:
        con.execute("BEGIN")
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


def largest_folders(*, db_path: Path | str | None = None) -> Report:
    """Пять крупнейших дочерних папок просканированных корней."""
    with closing(_read_index(db_path)) as con:
        rows = con.execute(
            "SELECT * FROM dirs WHERE depth = 1 AND total_size > 0 ORDER BY total_size DESC, path LIMIT 5"
        )
        return Report("tree", "Крупные папки", findings=[directory_finding(row) for row in rows])


def open_in_explorer(path: str, *, db_path: Path | str | None = None) -> None:
    """Открыть папку или выделить файл в Проводнике без запуска файла."""
    with closing(_read_index(db_path)) as con:
        row = find_directory(con, path)
        directory = row is not None
        if row is None:
            column, normalized = path_comparison("path", index_path(path))
            row = con.execute(
                f"SELECT path FROM files_v WHERE {column} = ? COLLATE NOCASE LIMIT 1",
                (normalized,),
            ).fetchone()
    if row is None:
        raise FileNotFoundError(path)
    indexed_path = safety.local_path_without_links(row["path"])
    if directory and not Path(indexed_path).is_dir():
        raise FileNotFoundError(path)
    options = [] if directory else ["/select,"]
    subprocess.Popen([str(config.EXPLORER_EXE), *options, indexed_path])


def list_drives(*, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    indexed = {item.path.casefold() for item in list_roots(db_path=db_path).findings if item.path}
    return [{**drive, "scanned": drive["path"].casefold() in indexed} for drive in scanner.local_drives()]


class TreemapNode(TypedDict):
    name: str
    value: int
    path: str | None
    children: list[TreemapNode]
    kind: NotRequired[Literal["file"]]


def _treemap_children(
    con: sqlite3.Connection,
    parent: sqlite3.Row,
    depth: int,
    include_files: bool,
) -> list[TreemapNode]:
    rows = con.execute(
        "SELECT * FROM dirs WHERE parent_id = ? AND total_size > 0 ORDER BY total_size DESC",
        (parent["id"],),
    )
    nodes = _treemap_nodes(con, rows, parent["total_size"], depth, include_files)
    own = parent["own_size"]
    if include_files and own:
        for row in _folder_files(con, parent["id"], 30):
            if row["size"] <= 0 or row["size"] * 200 < parent["total_size"]:
                break
            own -= row["size"]
            nodes.append(
                TreemapNode(name=row["name"], value=row["size"], path=row["path"], children=[], kind="file")
            )
    if own:
        if own * 200 >= parent["total_size"]:
            nodes.append(
                TreemapNode(
                    name="Остальные файлы" if include_files else "Файлы в папке",
                    value=own,
                    path=None,
                    children=[],
                )
            )
        else:
            other = next((node for node in nodes if node["name"] == "Прочее" and node["path"] is None), None)
            if other is None:
                nodes.append(TreemapNode(name="Прочее", value=own, path=None, children=[]))
            else:
                other["value"] += own
    return nodes


def _treemap_nodes(
    con: sqlite3.Connection,
    rows: Iterable[sqlite3.Row],
    total: int,
    depth: int,
    include_files: bool,
) -> list[TreemapNode]:
    nodes: list[TreemapNode] = []
    other = 0
    for row in rows:
        size = row["total_size"]
        if size * 200 < total:
            other += size
        else:
            nodes.append(
                TreemapNode(
                    name=row["name"],
                    value=size,
                    path=row["path"],
                    children=_treemap_children(con, row, depth - 1, include_files) if depth > 1 else [],
                )
            )
    if other:
        nodes.append(TreemapNode(name="Прочее", value=other, path=None, children=[]))
    return nodes


def treemap(
    path: str,
    depth: int = 2,
    *,
    include_files: bool = False,
    db_path: Path | str | None = None,
) -> list[TreemapNode]:
    if not 1 <= depth <= 2:
        raise ValueError("Глубина карты должна быть 1 или 2")
    with closing(_read_index(db_path)) as con, con:
        con.execute("BEGIN")
        if path:
            parent = find_directory(con, path)
            return _treemap_children(con, parent, depth, include_files) if parent is not None else []
        roots = con.execute(
            "SELECT * FROM dirs WHERE parent_id IS NULL AND total_size > 0 ORDER BY total_size DESC"
        )
        total = con.execute(
            "SELECT COALESCE(SUM(total_size), 0) FROM dirs WHERE parent_id IS NULL"
        ).fetchone()[0]
        return _treemap_nodes(con, roots, total, depth, include_files)


def _sql_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def sql_readonly(query: str, limit: int = 500, *, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    keyword = re.match(r"[A-Za-z]+", query.lstrip())
    if keyword is None or keyword[0].upper() not in {"SELECT", "WITH", "EXPLAIN"}:
        raise ValueError("Разрешены запросы SELECT, WITH и EXPLAIN")
    if not 1 <= limit <= 10_000:
        raise ValueError("Лимит SQL должен быть от 1 до 10 000 строк")
    with closing(db.connect(_db_path(db_path), readonly=True)) as con:
        return [
            {name: _sql_value(value) for name, value in dict(row).items()}
            for row in con.execute(query).fetchmany(limit)
        ]
