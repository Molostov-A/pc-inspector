"""Итеративный обход диска и транзакционная замена данных корня."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import stat
import time
from collections.abc import Callable
from concurrent.futures import CancelledError
from dataclasses import dataclass
from threading import Event
from typing import TypedDict

from . import db
from .safety import is_protected

REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT
CLOUD = stat.FILE_ATTRIBUTE_OFFLINE | 0x400000
SYSTEM_FILES = frozenset({"pagefile.sys", "hiberfil.sys", "swapfile.sys"})
FILE_BATCH = 50_000
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScanProgress:
    path: str
    files: int
    dirs: int
    errors: int
    files_per_second: float


class ScanResult(TypedDict):
    root: str
    files: int
    dirs: int
    errors: int
    size: int
    system_size: int
    seconds: float


@dataclass(slots=True)
class _Directory:
    parent: int | None
    path: str
    depth: int
    mtime: float
    protected: bool
    own_size: int = 0
    own_files: int = 0
    total_size: int = 0
    total_files: int = 0
    newest: float | None = None


def normalize_root(path: str) -> str:
    """Привести путь сканирования к абсолютному нативному виду."""
    if os.name == "nt" and len(path) == 2 and path[1] == ":":
        path += "\\"
    return os.path.abspath(path)


def local_ntfs_roots() -> list[str]:
    """Перечислить доступные локальные тома NTFS для скана без аргументов."""
    if os.name != "nt":
        return []

    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    drives = kernel.GetLogicalDrives()
    if not drives:
        raise ctypes.WinError(ctypes.get_last_error())
    filesystem = ctypes.create_unicode_buffer(32)
    roots = []
    for index in range(26):
        path = chr(ord("A") + index) + ":\\"
        if (
            drives & (1 << index)
            and kernel.GetDriveTypeW(path) in (2, 3)
            and kernel.GetVolumeInformationW(path, None, 0, None, None, None, filesystem, len(filesystem))
            and filesystem.value == "NTFS"
        ):
            roots.append(path)
    return roots


def _exclude_paths(con: sqlite3.Connection, root: str) -> set[str]:
    setting = con.execute("SELECT value FROM settings WHERE key = ?", ("exclude_paths",)).fetchone()
    if setting is None:
        return set()
    paths = json.loads(setting[0])
    if not isinstance(paths, list) or not all(isinstance(path, str) and path.strip() for path in paths):
        raise ValueError("Настройка exclude_paths должна быть списком непустых путей")
    return {normalize_root(os.path.join(root, path)).casefold() for path in paths}


def _is_link(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & REPARSE) or stat.S_ISLNK(info.st_mode)


def _is_volume_root(path: str) -> bool:
    return os.path.dirname(path) == path


def _ext(name: str) -> str:
    dot = name.rfind(".")
    return name[dot + 1 :].lower()[:16] if dot > 0 else ""


def scan(
    root: str,
    con: sqlite3.Connection,
    progress: Callable[[ScanProgress], None] | None = None,
    *,
    cancel: Event | None = None,
) -> ScanResult:
    """Просканировать один корень, сохранив прежние данные при ошибке или отмене."""
    root = normalize_root(root)
    root_info = os.lstat(root)
    if _is_link(root_info) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"Корень сканирования должен быть обычной папкой: {root}")

    synchronous = con.execute("PRAGMA synchronous").fetchone()[0]
    con.execute("PRAGMA synchronous=OFF")
    try:
        con.execute("BEGIN IMMEDIATE")
        with con:
            if cancel is not None:
                con.set_progress_handler(cancel.is_set, 10_000)
            try:
                result = _scan(root, root_info, con, progress, cancel)
            except sqlite3.OperationalError as ex:
                if ex.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT and cancel is not None and cancel.is_set():
                    raise CancelledError() from ex
                raise
            finally:
                con.set_progress_handler(None, 0)
    finally:
        con.execute(f"PRAGMA synchronous={synchronous}")
    logger.info(
        "Скан %s: %s файлов, %s папок, %s ошибок за %.2f с",
        root,
        result["files"],
        result["dirs"],
        result["errors"],
        result["seconds"],
    )
    return result


def _scan(
    root: str,
    root_info: os.stat_result,
    con: sqlite3.Connection,
    progress: Callable[[ScanProgress], None] | None,
    cancel: Event | None,
) -> ScanResult:
    start = time.perf_counter()
    root_key = root.casefold()
    excluded = _exclude_paths(con, root)

    def check_cancelled() -> None:
        if cancel is not None and cancel.is_set():
            raise CancelledError()

    check_cancelled()
    if any(root_key == path or root_key.startswith(path.rstrip(os.sep) + os.sep) for path in excluded):
        raise ValueError(f"Корень указан в исключениях: {root}")

    # Пустой путь существует только внутри транзакции. Читатели видят старый корень до фиксации.
    previous_roots = [
        row["id"] for row in con.execute("SELECT id, path FROM roots") if row["path"].casefold() == root_key
    ]
    root_id = con.execute("INSERT INTO roots(path) VALUES ('')").lastrowid
    db.drop_indexes(con)
    root_dir_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM dirs").fetchone()[0]
    dirs = {root_dir_id: _Directory(None, root, 0, root_info.st_mtime, is_protected(root))}
    stack = [root_dir_id]
    files: list[tuple] = []
    errors: list[tuple] = []
    n_files = n_errors = system_size = system_files = 0
    last_report = 0.0

    def notify(path: str, *, force: bool = False) -> None:
        nonlocal last_report
        now = time.perf_counter()
        if progress is not None and (force or now - last_report >= 0.25):
            progress(ScanProgress(path, n_files, len(dirs), n_errors, n_files / max(now - start, 0.001)))
            last_report = now

    def flush_files() -> None:
        con.executemany(
            "INSERT INTO files(root_id, dir_id, name, ext, size, mtime, atime, ctime, attrs)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            files,
        )
        files.clear()

    def record_error(path: str, error: OSError) -> None:
        nonlocal n_errors
        n_errors += 1
        errors.append((root_id, path, str(error)))
        if len(errors) == FILE_BATCH:
            con.executemany("INSERT INTO errors(root_id, path, message) VALUES (?,?,?)", errors)
            errors.clear()

    notify(root, force=True)
    while stack:
        check_cancelled()
        did = stack.pop()
        directory = dirs[did]
        volume_root = _is_volume_root(directory.path)
        notify(directory.path)
        try:
            with os.scandir(directory.path) as entries:
                for entry in entries:
                    check_cancelled()
                    if excluded and entry.path.casefold() in excluded:
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError as ex:
                        record_error(entry.path, ex)
                        continue

                    attrs = getattr(info, "st_file_attributes", 0)
                    is_dir = stat.S_ISDIR(info.st_mode)
                    if _is_link(info) and (is_dir or not attrs & CLOUD or stat.S_ISLNK(info.st_mode)):
                        continue

                    if is_dir:
                        child_id = root_dir_id + len(dirs)
                        dirs[child_id] = _Directory(
                            did,
                            entry.path,
                            directory.depth + 1,
                            info.st_mtime,
                            directory.protected or is_protected(entry.path),
                        )
                        stack.append(child_id)
                        continue

                    if volume_root and entry.name.lower() in SYSTEM_FILES:
                        system_size += info.st_size
                        system_files += 1
                        continue

                    size = 0 if attrs & CLOUD else info.st_size
                    directory.own_size += size
                    directory.own_files += 1
                    if directory.newest is None or info.st_mtime > directory.newest:
                        directory.newest = info.st_mtime
                    n_files += 1
                    files.append(
                        (
                            root_id,
                            did,
                            entry.name,
                            _ext(entry.name),
                            size,
                            info.st_mtime,
                            info.st_atime,
                            info.st_ctime,
                            attrs,
                        )
                    )
                    if len(files) == FILE_BATCH:
                        flush_files()
                    if n_files % 256 == 0:
                        notify(directory.path)
        except OSError as ex:
            if did == root_dir_id:
                raise
            record_error(directory.path, ex)

    flush_files()
    con.executemany("INSERT INTO errors(root_id, path, message) VALUES (?,?,?)", errors)
    for directory in reversed(dirs.values()):
        check_cancelled()
        directory.total_size += directory.own_size
        directory.total_files += directory.own_files
        if directory.parent is not None:
            parent = dirs[directory.parent]
            parent.total_size += directory.total_size
            parent.total_files += directory.total_files
            if directory.newest is not None and (parent.newest is None or directory.newest > parent.newest):
                parent.newest = directory.newest

    con.executemany(
        "INSERT INTO dirs(id, root_id, parent_id, path, name, depth, own_size, own_files,"
        " total_size, total_files, mtime, newest_mtime, protected) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            (
                did,
                root_id,
                directory.parent,
                directory.path,
                os.path.basename(directory.path) or directory.path,
                directory.depth,
                directory.own_size,
                directory.own_files,
                directory.total_size,
                directory.total_files,
                directory.mtime,
                directory.newest,
                directory.protected,
            )
            for did, directory in dirs.items()
        ),
    )

    for old_id in previous_roots:
        for table in ("files", "dirs", "errors"):
            check_cancelled()
            con.execute(f"DELETE FROM {table} WHERE root_id = ?", (old_id,))
        con.execute("DELETE FROM roots WHERE id = ?", (old_id,))

    try:
        usage = shutil.disk_usage(root)
        disk_total, disk_free = usage.total, usage.free
    except OSError:
        disk_total = disk_free = None
    db.create_indexes(con)
    duration = time.perf_counter() - start
    size = dirs[root_dir_id].total_size
    con.execute(
        "UPDATE roots SET path=?, scanned_at=?, duration_s=?, total_size=?, file_count=?, dir_count=?,"
        " error_count=?, system_size=?, system_files=?, disk_total=?, disk_free=? WHERE id=?",
        (
            root,
            time.time(),
            duration,
            size,
            n_files,
            len(dirs),
            n_errors,
            system_size,
            system_files,
            disk_total,
            disk_free,
            root_id,
        ),
    )
    notify(root, force=True)
    check_cancelled()
    return ScanResult(
        root=root,
        files=n_files,
        dirs=len(dirs),
        errors=n_errors,
        size=size,
        system_size=system_size,
        seconds=round(duration, 3),
    )
