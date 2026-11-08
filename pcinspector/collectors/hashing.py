"""Фоновое двухпроходное хэширование кандидатов из SQLite-индекса."""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError
from threading import Event
from typing import BinaryIO, Literal

from ..core import safety
from ..core.jobs import HashProgress, HashResult
from ..core.paths import root_filter
from ..core.scanner import CLOUD, REPARSE

DEFAULT_MIN_SIZE = 1024**2
PART = 64 * 1024
BLOCK = 1024**2
BATCH = 128
logger = logging.getLogger(__name__)

_UPSERT_PARTIAL = """
INSERT INTO file_hashes(path,directory,name,size,mtime,ctime,identity,partial_hash)
VALUES (?,?,?,?,?,?,?,?)
ON CONFLICT(path) DO UPDATE SET
    directory=excluded.directory, name=excluded.name, size=excluded.size,
    mtime=excluded.mtime, ctime=excluded.ctime, identity=excluded.identity,
    partial_hash=excluded.partial_hash,
    full_hash=CASE WHEN file_hashes.size=excluded.size AND file_hashes.mtime=excluded.mtime
        AND file_hashes.ctime=excluded.ctime AND file_hashes.identity=excluded.identity
        AND file_hashes.partial_hash=excluded.partial_hash THEN file_hashes.full_hash END
"""


class _HardLink(ValueError):
    """Объект с несколькими именами нельзя считать освобождаемой копией."""


def _check_cancel(cancel: Event) -> None:
    if cancel.is_set():
        raise CancelledError()


def _digest(stream: BinaryIO, size: int, full: bool, cancel: Event) -> str:
    digest = hashlib.blake2b(digest_size=32)
    if full:
        while chunk := stream.read(BLOCK):
            _check_cancel(cancel)
            digest.update(chunk)
    else:
        digest.update(stream.read(PART))
        if size > PART:
            stream.seek(-PART, os.SEEK_END)
            digest.update(stream.read(PART))
    return digest.hexdigest()


def _identity(info: os.stat_result, row: sqlite3.Row) -> str:
    if getattr(info, "st_file_attributes", 0) & (CLOUD | REPARSE) or not stat.S_ISREG(info.st_mode):
        raise ValueError("Ссылка, облачная заглушка или необычный тип файла")
    if info.st_nlink > 1:
        raise _HardLink("У файла несколько жёстких ссылок")
    if not info.st_ino:
        raise ValueError("Не удалось проверить идентификатор файла")
    if (info.st_size, info.st_mtime, info.st_ctime) != (row["size"], row["mtime"], row["ctime"]):
        raise ValueError("Файл изменился после сканирования")
    return f"{info.st_dev}:{info.st_ino}"


def _hash_file(
    con: sqlite3.Connection,
    row: sqlite3.Row,
    phase: Literal["partial", "full"],
    cancel: Event,
) -> tuple[str, str, bool]:
    path = row["path"]
    if os.name == "nt":
        safety.local_path_without_links(path)
    before = _identity(os.lstat(path), row)
    with open(path, "rb") as stream:
        identity = _identity(os.fstat(stream.fileno()), row)
        if before != identity:
            raise ValueError("Файл был заменён перед чтением")
        if phase == "full" and row["identity"] != identity:
            raise ValueError("Файл был заменён после расчёта частичного хэша")
        cache = con.execute(
            "SELECT partial_hash,full_hash FROM file_hashes WHERE path=? COLLATE NOCASE "
            "AND size=? AND mtime=? AND ctime=? AND identity=?",
            (path, row["size"], row["mtime"], row["ctime"], identity),
        ).fetchone()
        value = cache[f"{phase}_hash"] if cache is not None else None
        if phase == "full" and cache is not None and cache["partial_hash"] != row["partial_hash"]:
            value = None
        if value is None:
            value = _digest(stream, row["size"], phase == "full", cancel)
            if _identity(os.fstat(stream.fileno()), row) != identity:
                raise ValueError("Файл изменился при чтении")
            current = _identity(os.lstat(path), row)
            if current != identity:
                raise ValueError("Файл был заменён при чтении")
            return identity, value, False
        return identity, value, True


def _materialize(con: sqlite3.Connection, sql: str, args: list[int | str], cancel: Event) -> None:
    con.set_progress_handler(cancel.is_set, 10_000)
    try:
        con.execute(sql, args)
    except sqlite3.OperationalError as ex:
        if cancel.is_set() and ex.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT:
            raise CancelledError() from ex
        raise
    finally:
        con.set_progress_handler(None, 0)


def _batches(con: sqlite3.Connection, phase: Literal["partial", "full"]) -> Iterator[list[sqlite3.Row]]:
    table = "hash_candidates" if phase == "partial" else "hash_matches"
    cursor = 0
    while rows := con.execute(
        f"SELECT rowid AS sequence,* FROM {table} WHERE rowid>? ORDER BY rowid LIMIT ?",
        (cursor, BATCH),
    ).fetchall():
        cursor = rows[-1]["sequence"]
        yield rows


def _phase(
    con: sqlite3.Connection,
    phase: Literal["partial", "full"],
    total: int,
    result: HashResult,
    notify: Callable[[HashProgress], None],
    cancel: Event,
) -> None:
    processed = 0
    notify(HashProgress(phase, 0, total, result["cached"], result["errors"]))
    last = time.monotonic()
    for rows in _batches(con, phase):
        writes = []
        invalid = []
        for row in rows:
            _check_cancel(cancel)
            try:
                identity, value, cached = _hash_file(con, row, phase, cancel)
            except (ValueError, OSError) as ex:
                invalid.append((row["path"], row["size"], row["mtime"], row["ctime"]))
                if isinstance(ex, _HardLink):
                    result["hardlinks"] += 1
                else:
                    result["errors"] += 1
                    logger.warning("Хэширование %s: %s", row["path"], ex)
            else:
                if cached:
                    result["cached"] += 1
                elif phase == "partial":
                    result["partial_read"] += 1
                else:
                    result["full_read"] += 1
                writes.append((row, identity, value))
            processed += 1
            now = time.monotonic()
            if now - last >= 0.25:
                notify(HashProgress(phase, processed, total, result["cached"], result["errors"]))
                last = now
        with con:
            con.executemany(
                "DELETE FROM file_hashes WHERE path=? COLLATE NOCASE AND size=? AND mtime=? AND ctime=?",
                invalid,
            )
            if phase == "partial":
                con.executemany(
                    _UPSERT_PARTIAL,
                    (
                        (r["path"], r["directory"], r["name"], r["size"], r["mtime"], r["ctime"], i, h)
                        for r, i, h in writes
                    ),
                )
                con.executemany(
                    "UPDATE hash_candidates SET identity=?,partial_hash=? WHERE rowid=?",
                    ((i, h, r["sequence"]) for r, i, h in writes),
                )
            else:
                con.executemany(
                    "UPDATE file_hashes SET full_hash=? WHERE path=? COLLATE NOCASE "
                    "AND size=? AND mtime=? AND ctime=? AND identity=? AND partial_hash=?",
                    (
                        (h, r["path"], r["size"], r["mtime"], r["ctime"], i, r["partial_hash"])
                        for r, i, h in writes
                    ),
                )
    notify(HashProgress(phase, processed, total, result["cached"], result["errors"]))


def collect(
    con: sqlite3.Connection,
    root: str,
    min_size: int,
    notify: Callable[[HashProgress], None],
    cancel: Event,
) -> HashResult:
    notify(HashProgress("candidates"))
    _check_cancel(cancel)
    con.execute("DROP TABLE IF EXISTS temp.hash_matches")
    con.execute("DROP TABLE IF EXISTS temp.hash_candidates")
    started = time.time()
    condition, args = root_filter("v.path", root)
    _materialize(
        con,
        f"""
        CREATE TEMP TABLE hash_candidates AS
        WITH eligible AS MATERIALIZED (
            SELECT v.id,v.path,d.path AS directory,v.name,v.size,v.mtime,v.ctime
            FROM files_v v JOIN dirs d ON d.id=v.dir_id
            WHERE v.size>=? AND v.protected=0 AND (v.attrs & ?)=0
                AND v.mtime IS NOT NULL AND v.ctime IS NOT NULL AND {condition}
        ), unique_paths AS (
            SELECT max(id) AS id FROM eligible GROUP BY pci_casefold(path)
        ), candidates AS (
            SELECT e.* FROM eligible e JOIN unique_paths u USING(id)
        ), shared AS (
            SELECT size FROM candidates GROUP BY size HAVING count(*)>1
        )
        SELECT e.path,e.directory,e.name,e.size,e.mtime,e.ctime,
               CAST(NULL AS TEXT) AS identity,CAST(NULL AS TEXT) AS partial_hash
        FROM candidates e JOIN shared USING(size)
        """,
        [min_size, CLOUD | REPARSE, *args],
        cancel,
    )
    count = con.execute("SELECT count(*) FROM hash_candidates").fetchone()[0]
    result = HashResult(
        root=root, candidates=count, partial_read=0, full_read=0, cached=0, hardlinks=0, errors=0
    )
    _phase(con, "partial", count, result, notify, cancel)
    _check_cancel(cancel)
    _materialize(
        con,
        """
        CREATE TEMP TABLE hash_matches AS
        WITH groups AS (
            SELECT size,partial_hash FROM hash_candidates WHERE partial_hash IS NOT NULL
            GROUP BY size,partial_hash HAVING count(DISTINCT identity)>1
        )
        SELECT c.* FROM hash_candidates c JOIN groups USING(size,partial_hash)
        """,
        [],
        cancel,
    )
    matches = con.execute("SELECT count(*) FROM hash_matches").fetchone()[0]
    _phase(con, "full", matches, result, notify, cancel)
    _check_cancel(cancel)
    with con:
        con.execute(
            "INSERT INTO hash_runs(root,min_size,started_at,errors) VALUES (?,?,?,?) "
            "ON CONFLICT(root,min_size) DO UPDATE SET started_at=excluded.started_at,errors=excluded.errors",
            (root, min_size, started, result["errors"]),
        )
    return result
