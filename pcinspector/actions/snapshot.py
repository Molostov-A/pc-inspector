"""Сравнение индексированного снимка с состоянием файла или поддерева."""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import sqlite3
import stat
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Literal

Kind = Literal["file", "dir"]
Entry = tuple[str, Kind, int, float]


def fingerprint(entries: Iterable[Entry], root: str) -> str:
    # Сумма хэшей позволяет сравнить два потока без хранения или сортировки всего дерева.
    total = count = 0
    for path, kind, size, mtime in entries:
        relative = ntpath.relpath(path, root).casefold()
        data = json.dumps((relative, kind, size, mtime), ensure_ascii=False).encode("utf-8")
        total += int.from_bytes(hashlib.sha256(data).digest(), "big")
        count += 1
    return f"{count}:{total % (1 << 256):064x}"


def directory_index(con: sqlite3.Connection, directory_id: int) -> Iterator[Entry]:
    rows = con.execute(
        """
        WITH RECURSIVE subtree(id) AS (
            SELECT ? UNION ALL SELECT d.id FROM dirs d JOIN subtree p ON d.parent_id = p.id
        )
        SELECT path, 'dir', 0, mtime FROM dirs JOIN subtree USING(id)
        UNION ALL
        SELECT path, 'file', size, mtime FROM files_v JOIN subtree ON subtree.id = files_v.dir_id
        """,
        (directory_id,),
    )
    for path, kind, size, mtime in rows:
        if mtime is None:
            raise ValueError("Индекс не содержит даты изменения одного из элементов папки")
        yield path, kind, size, mtime


def _entry(path: Path, info: os.stat_result) -> Entry:
    if getattr(info, "st_file_attributes", 0) & (0x400000 | 0x1000 | stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError("В дереве есть ссылка или облачная заглушка")
    if stat.S_ISDIR(info.st_mode):
        return str(path), "dir", 0, info.st_mtime
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("В дереве есть объект, отличный от обычного файла или папки")
    return str(path), "file", info.st_size, info.st_mtime


def live_entries(path: Path) -> Iterator[Entry]:
    root = _entry(path, path.lstat())
    yield root
    stack = [path] if root[1] == "dir" else []
    while stack:
        with os.scandir(stack.pop()) as entries:
            for item in entries:
                entry = _entry(Path(item.path), item.stat(follow_symlinks=False))
                yield entry
                if entry[1] == "dir":
                    stack.append(Path(item.path))
