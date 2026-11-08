"""Вложенные данные для карты размеров."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Literal, NotRequired, TypedDict

from ..analyzers.overview import find_directory
from .reports import _folder_files
from .workspace import DbPath, read


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
    db_path: DbPath = None,
) -> list[TreemapNode]:
    if not 1 <= depth <= 2:
        raise ValueError("Глубина карты должна быть 1 или 2")
    with read(db_path) as con:
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
