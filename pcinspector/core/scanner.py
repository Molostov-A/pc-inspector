"""Итеративный обход диска, транзакционная замена корня и точечное обновление индекса."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import stat
import time
from collections.abc import Callable, Iterable
from concurrent.futures import CancelledError
from dataclasses import dataclass, field
from threading import Event
from typing import TypedDict

from . import db, settings, snapshots
from .safety import is_protected, local_path_without_links

REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT
CLOUD = stat.FILE_ATTRIBUTE_OFFLINE | 0x400000
SYSTEM_FILES = frozenset({"pagefile.sys", "hiberfil.sys", "swapfile.sys"})
FILE_BATCH = 50_000
# Индексы удаляются только при большой записи: иначе их перестройка дороже самого скана.
BULK_MIN = 50_000
BULK_SHARE = 4
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


class Drive(TypedDict):
    path: str
    label: str
    filesystem: str
    total: int | None
    free: int | None


Progress = Callable[[ScanProgress], None] | None


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


@dataclass(slots=True)
class _Walk:
    first_id: int
    dirs: dict[int, _Directory] = field(default_factory=dict)
    files: int = 0
    errors: int = 0
    system_size: int = 0
    system_files: int = 0

    @property
    def top(self) -> _Directory:
        return self.dirs[self.first_id]


def normalize_root(path: str) -> str:
    """Привести путь сканирования к абсолютному нативному виду."""
    if os.name == "nt" and len(path) == 2 and path[1] == ":":
        path += "\\"
    return os.path.abspath(path)


def local_ntfs_roots() -> list[str]:
    """Перечислить доступные локальные тома NTFS для скана без аргументов."""
    return [drive["path"] for drive in local_drives() if drive["filesystem"] == "NTFS"]


def local_drives() -> list[Drive]:
    """Собрать метки и ёмкость доступных локальных томов Windows."""
    if os.name != "nt":
        return []

    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    drives = kernel.GetLogicalDrives()
    if not drives:
        raise ctypes.WinError(ctypes.get_last_error())
    filesystem = ctypes.create_unicode_buffer(32)
    label = ctypes.create_unicode_buffer(261)
    result: list[Drive] = []
    for index in range(26):
        path = chr(ord("A") + index) + ":\\"
        if (
            drives & (1 << index)
            and kernel.GetDriveTypeW(path) in (2, 3)
            and kernel.GetVolumeInformationW(
                path, label, len(label), None, None, None, filesystem, len(filesystem)
            )
        ):
            try:
                usage = shutil.disk_usage(path)
                total, free = usage.total, usage.free
            except OSError:
                total = free = None
            result.append(
                Drive(path=path, label=label.value, filesystem=filesystem.value, total=total, free=free)
            )
    return result


def _exclude_paths(con: sqlite3.Connection, root: str) -> set[str]:
    paths = settings.load(con)["exclude_paths"]
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


def _reject_excluded(root: str, excluded: set[str]) -> None:
    key = root.casefold()
    if any(key == path or _inside(key, path) for path in excluded):
        raise ValueError(f"Корень указан в исключениях: {root}")


def _inside(path: str, root: str) -> bool:
    """Проверить вложенность по строкам в casefold; равенство вложенностью не считается."""
    return path != root and path.startswith(root.rstrip(os.sep) + os.sep)


def _file_row(root_id: int, did: int, name: str, info: os.stat_result) -> tuple:
    attrs = getattr(info, "st_file_attributes", 0)
    size = 0 if attrs & CLOUD else info.st_size
    return (root_id, did, name, _ext(name), size, info.st_mtime, info.st_atime, info.st_ctime, attrs)


def scan(
    root: str, con: sqlite3.Connection, progress: Progress = None, *, cancel: Event | None = None
) -> ScanResult:
    """Просканировать один корень, сохранив прежние данные при ошибке или отмене.

    Папка внутри уже просканированного корня обновляется как его часть и не становится новым корнем.
    """
    root = normalize_root(root)
    if os.name == "nt":
        local_path_without_links(root)
    root_info = os.lstat(root)
    if _is_link(root_info) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"Корень сканирования должен быть обычной папкой: {root}")

    synchronous = con.execute("PRAGMA main.synchronous").fetchone()[0]
    con.execute("PRAGMA main.synchronous=OFF")
    try:
        con.execute("BEGIN IMMEDIATE")
        with con:
            if cancel is not None:
                con.set_progress_handler(cancel.is_set, 10_000)
            try:
                container = _container(con, root)
                result = (
                    _refresh_nested(root, container, con, progress, cancel)
                    if container is not None
                    else _scan(root, root_info, con, progress, cancel)
                )
            except sqlite3.OperationalError as ex:
                if ex.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT and cancel is not None and cancel.is_set():
                    raise CancelledError() from ex
                raise
            finally:
                con.set_progress_handler(None, 0)
    finally:
        con.execute(f"PRAGMA main.synchronous={synchronous}")
    logger.info(
        "Скан %s: %s файлов, %s папок, %s ошибок за %.2f с",
        root,
        result["files"],
        result["dirs"],
        result["errors"],
        result["seconds"],
    )
    return result


def _container(con: sqlite3.Connection, root: str) -> sqlite3.Row | None:
    key = root.casefold()
    return next(
        (row for row in con.execute("SELECT id, path FROM roots") if _inside(key, row["path"].casefold())),
        None,
    )


def _walk(
    con: sqlite3.Connection,
    root_id: int,
    top: _Directory,
    first_id: int,
    excluded: set[str],
    progress: Progress,
    cancel: Event | None,
    *,
    on_files: Callable[[int], None] = lambda _count: None,
) -> _Walk:
    """Обойти дерево от top, записать файлы и ошибки и посчитать итоги папок в памяти."""
    start = time.perf_counter()
    walk = _Walk(first_id, {first_id: top})
    dirs = walk.dirs
    stack = [first_id]
    files: list[tuple] = []
    errors: list[tuple] = []
    last_report = 0.0

    def check_cancelled() -> None:
        if cancel is not None and cancel.is_set():
            raise CancelledError()

    def notify(path: str, *, force: bool = False) -> None:
        nonlocal last_report
        now = time.perf_counter()
        if progress is not None and (force or now - last_report >= 0.25):
            progress(
                ScanProgress(path, walk.files, len(dirs), walk.errors, walk.files / max(now - start, 0.001))
            )
            last_report = now

    def flush_files() -> None:
        on_files(walk.files)
        con.executemany(
            "INSERT INTO files(root_id, dir_id, name, ext, size, mtime, atime, ctime, attrs)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            files,
        )
        files.clear()

    def record_error(path: str, error: OSError) -> None:
        walk.errors += 1
        errors.append((root_id, path, str(error)))
        if len(errors) == FILE_BATCH:
            con.executemany("INSERT INTO errors(root_id, path, message) VALUES (?,?,?)", errors)
            errors.clear()

    notify(top.path, force=True)
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
                        child_id = first_id + len(dirs)
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
                        walk.system_size += info.st_size
                        walk.system_files += 1
                        continue

                    row = _file_row(root_id, did, entry.name, info)
                    directory.own_size += row[4]
                    directory.own_files += 1
                    if directory.newest is None or info.st_mtime > directory.newest:
                        directory.newest = info.st_mtime
                    walk.files += 1
                    files.append(row)
                    if len(files) == FILE_BATCH:
                        flush_files()
                    if walk.files % 256 == 0:
                        notify(directory.path)
        except OSError as ex:
            if did == first_id and directory.parent is None:
                raise
            record_error(directory.path, ex)

    flush_files()
    con.executemany("INSERT INTO errors(root_id, path, message) VALUES (?,?,?)", errors)
    for directory in reversed(dirs.values()):
        check_cancelled()
        directory.total_size += directory.own_size
        directory.total_files += directory.own_files
        parent = dirs.get(directory.parent) if directory.parent is not None else None
        if parent is not None:
            parent.total_size += directory.total_size
            parent.total_files += directory.total_files
            if directory.newest is not None and (parent.newest is None or directory.newest > parent.newest):
                parent.newest = directory.newest
    notify(top.path, force=True)
    return walk


def _insert_dirs(con: sqlite3.Connection, root_id: int, walk: _Walk) -> None:
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
            for did, directory in walk.dirs.items()
        ),
    )


def _next_dir_id(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM dirs").fetchone()[0]


def _scan(
    root: str,
    root_info: os.stat_result,
    con: sqlite3.Connection,
    progress: Progress,
    cancel: Event | None,
) -> ScanResult:
    start = time.perf_counter()
    root_key = root.casefold()
    excluded = _exclude_paths(con, root)

    def check_cancelled() -> None:
        if cancel is not None and cancel.is_set():
            raise CancelledError()

    check_cancelled()
    _reject_excluded(root, excluded)

    # Прежние корни заменяются: этот же путь и все корни, оказавшиеся внутри нового.
    existing = list(con.execute("SELECT id, path, file_count FROM roots"))
    previous = [
        row
        for row in existing
        if row["path"].casefold() == root_key or _inside(row["path"].casefold(), root_key)
    ]
    replaced = {row["id"] for row in previous}
    others = sum(row["file_count"] or 0 for row in existing if row["id"] not in replaced)
    # Пустой путь существует только внутри транзакции. Читатели видят старый корень до фиксации.
    root_id = con.execute("INSERT INTO roots(path) VALUES ('')").lastrowid
    root_dir_id = _next_dir_id(con)
    bulk_limit = max(BULK_MIN, others // BULK_SHARE)
    dropped = False

    def on_files(count: int) -> None:
        nonlocal dropped
        if not dropped and count > bulk_limit:
            db.drop_indexes(con)
            dropped = True

    walk = _walk(
        con,
        root_id,
        _Directory(None, root, 0, root_info.st_mtime, is_protected(root)),
        root_dir_id,
        excluded,
        progress,
        cancel,
        on_files=on_files,
    )
    on_files(max(walk.files, len(walk.dirs)))
    _insert_dirs(con, root_id, walk)

    for row in previous:
        snapshots.record(con, row["id"])
        for table in ("files", "dirs", "errors"):
            check_cancelled()
            con.execute(f"DELETE FROM {table} WHERE root_id = ?", (row["id"],))
        con.execute("DELETE FROM roots WHERE id = ?", (row["id"],))

    try:
        usage = shutil.disk_usage(root)
        disk_total, disk_free = usage.total, usage.free
    except OSError:
        disk_total = disk_free = None
    first_stats = con.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_stat1'").fetchone() is None
    db.create_indexes(con, analyze=dropped or first_stats)
    duration = time.perf_counter() - start
    size = walk.top.total_size
    con.execute(
        "UPDATE roots SET path=?, scanned_at=?, duration_s=?, total_size=?, file_count=?, dir_count=?,"
        " error_count=?, system_size=?, system_files=?, disk_total=?, disk_free=? WHERE id=?",
        (
            root,
            time.time(),
            duration,
            size,
            walk.files,
            len(walk.dirs),
            walk.errors,
            walk.system_size,
            walk.system_files,
            disk_total,
            disk_free,
            root_id,
        ),
    )
    check_cancelled()
    snapshots.record(con, root_id)
    check_cancelled()
    return ScanResult(
        root=root,
        files=walk.files,
        dirs=len(walk.dirs),
        errors=walk.errors,
        size=size,
        system_size=walk.system_size,
        seconds=round(duration, 3),
    )


def _dir_row(con: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM dirs WHERE path = ? COLLATE NOCASE LIMIT 1", (path,)).fetchone()


def _propagate(
    con: sqlite3.Connection, dir_id: int | None, root_id: int, size: int, files: int, dirs: int
) -> None:
    """Применить разницу к предкам и заново вычислить самую свежую дату вверх по дереву."""
    current = dir_id
    while current is not None:
        newest = con.execute(
            "SELECT MAX(value) FROM (SELECT MAX(mtime) AS value FROM files WHERE dir_id = ?"
            " UNION ALL SELECT MAX(newest_mtime) FROM dirs WHERE parent_id = ?)",
            (current, current),
        ).fetchone()[0]
        con.execute(
            "UPDATE dirs SET total_size = total_size + ?, total_files = total_files + ?, newest_mtime = ?"
            " WHERE id = ?",
            (size, files, newest, current),
        )
        current = con.execute("SELECT parent_id FROM dirs WHERE id = ?", (current,)).fetchone()[0]
    con.execute(
        "UPDATE roots SET total_size = total_size + ?, file_count = file_count + ?, dir_count = dir_count + ?"
        " WHERE id = ?",
        (size, files, dirs, root_id),
    )


def _delete_subtree(con: sqlite3.Connection, row: sqlite3.Row) -> int:
    """Удалить строки поддерева из индекса и вернуть число удалённых папок."""
    subtree = "WITH RECURSIVE subtree(id) AS (SELECT ? UNION ALL SELECT d.id FROM dirs d JOIN subtree s ON d.parent_id = s.id)"
    con.execute(f"{subtree} DELETE FROM files WHERE dir_id IN (SELECT id FROM subtree)", (row["id"],))
    con.execute(f"{subtree} DELETE FROM dirs WHERE id IN (SELECT id FROM subtree)", (row["id"],))
    removed = con.execute("SELECT changes()").fetchone()[0]
    key = row["path"].casefold()
    con.execute(
        "DELETE FROM errors WHERE root_id = ? AND (pci_casefold(path) = ? OR instr(pci_casefold(path), ?) = 1)",
        (row["root_id"], key, key.rstrip(os.sep) + os.sep),
    )
    return removed


def _replace_directory(
    con: sqlite3.Connection,
    path: str,
    info: os.stat_result,
    old: sqlite3.Row | None,
    parent: sqlite3.Row | None,
    root_id: int,
    excluded: set[str],
    progress: Progress = None,
    cancel: Event | None = None,
) -> _Walk:
    removed = _delete_subtree(con, old) if old is not None else 0
    protected = bool(parent["protected"]) if parent is not None else False
    top = _Directory(
        parent["id"] if parent is not None else None,
        path,
        parent["depth"] + 1 if parent is not None else 0,
        info.st_mtime,
        protected or is_protected(path),
    )
    walk = _walk(con, root_id, top, _next_dir_id(con), excluded, progress, cancel)
    _insert_dirs(con, root_id, walk)
    _propagate(
        con,
        top.parent,
        root_id,
        walk.top.total_size - (old["total_size"] if old is not None else 0),
        walk.top.total_files - (old["total_files"] if old is not None else 0),
        len(walk.dirs) - removed,
    )
    return walk


def _refresh_nested(
    root: str,
    container: sqlite3.Row,
    con: sqlite3.Connection,
    progress: Progress,
    cancel: Event | None,
) -> ScanResult:
    start = time.perf_counter()
    excluded = _exclude_paths(con, container["path"])
    _reject_excluded(root, excluded)
    target, info = _indexable(con, root)
    if target is None or info is None:
        raise ValueError(f"Папка не связана с деревом корня {container['path']}: {root}")
    old = _dir_row(con, target)
    parent = _dir_row(con, os.path.dirname(target))
    walk = _replace_directory(
        con,
        target,
        info,
        old,
        parent,
        container["id"],
        excluded,
        progress,
        cancel,
    )
    return ScanResult(
        root=target,
        files=walk.files,
        dirs=len(walk.dirs),
        errors=walk.errors,
        size=walk.top.total_size,
        system_size=0,
        seconds=round(time.perf_counter() - start, 3),
    )


def _indexable(con: sqlite3.Connection, path: str) -> tuple[str | None, os.stat_result | None]:
    """Найти верхнюю отсутствующую в индексе папку на пути, родитель которой уже есть в индексе."""
    current = path
    while _dir_row(con, os.path.dirname(current)) is None:
        parent = os.path.dirname(current)
        if parent == current:
            return None, None
        current = parent
    try:
        info = os.lstat(current)
    except OSError:
        return None, None
    return (current, info) if stat.S_ISDIR(info.st_mode) and not _is_link(info) else (None, None)


def _refresh_file(con: sqlite3.Connection, path: str, info: os.stat_result | None) -> None:
    parent = _dir_row(con, os.path.dirname(path))
    if parent is None:
        return
    name = os.path.basename(path)
    old = con.execute(
        "SELECT id, size FROM files WHERE dir_id = ? AND name = ? COLLATE NOCASE LIMIT 1",
        (parent["id"], name),
    ).fetchone()
    if old is not None:
        con.execute("DELETE FROM files WHERE id = ?", (old["id"],))
    size = files = 0
    if info is not None:
        row = _file_row(parent["root_id"], parent["id"], name, info)
        con.execute(
            "INSERT INTO files(root_id, dir_id, name, ext, size, mtime, atime, ctime, attrs)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            row,
        )
        size, files = row[4], 1
    if old is None and info is None:
        return
    delta_size = size - (old["size"] if old is not None else 0)
    delta_files = files - (old is not None)
    con.execute(
        "UPDATE dirs SET own_size = own_size + ?, own_files = own_files + ? WHERE id = ?",
        (delta_size, delta_files, parent["id"]),
    )
    _propagate(con, parent["id"], parent["root_id"], delta_size, delta_files, 0)


def _refresh_path(con: sqlite3.Connection, path: str) -> None:
    try:
        info: os.stat_result | None = os.lstat(path)
    except FileNotFoundError:
        info = None
    if info is not None and _is_link(info):
        info = None
    row = _dir_row(con, path)
    if info is None or not stat.S_ISDIR(info.st_mode):
        if row is not None:
            removed = _delete_subtree(con, row)
            _propagate(
                con, row["parent_id"], row["root_id"], -row["total_size"], -row["total_files"], -removed
            )
        _refresh_file(con, path, info)
        return
    if row is not None:
        root_path = con.execute("SELECT path FROM roots WHERE id = ?", (row["root_id"],)).fetchone()["path"]
        parent = _dir_row(con, os.path.dirname(path)) if row["parent_id"] is not None else None
        _replace_directory(
            con, row["path"], info, row, parent, row["root_id"], _exclude_paths(con, root_path)
        )
        return
    target, target_info = _indexable(con, path)
    if target is None or target_info is None:
        return
    parent = _dir_row(con, os.path.dirname(target))
    root_path = con.execute("SELECT path FROM roots WHERE id = ?", (parent["root_id"],)).fetchone()["path"]
    _replace_directory(
        con, target, target_info, None, parent, parent["root_id"], _exclude_paths(con, root_path)
    )


def refresh_paths(con: sqlite3.Connection, paths: Iterable[str]) -> None:
    """Привести индекс в соответствие с диском для путей, изменённых уборкой или откатом."""
    con.execute("BEGIN IMMEDIATE")
    with con:
        for path in paths:
            _refresh_path(con, normalize_root(path))
