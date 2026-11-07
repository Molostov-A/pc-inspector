"""Обход диска и запись в индекс.

Используется os.scandir: на Windows атрибуты файла приходят вместе с листингом
каталога (FindNextFileW), поэтому лишних системных вызовов stat нет.
Точки повторной обработки (симлинки, junction) не обходятся — иначе возможны
циклы и двойной подсчёт.
"""

from __future__ import annotations

import os
import sqlite3
import stat as stat_mod
import sys
import time
from datetime import datetime

from . import db

REPARSE = getattr(stat_mod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

# Системные зоны: показываем в отчётах, но никогда не предлагаем удалять руками.
PROTECTED_SUFFIXES = [
    r"\windows",
    r"\program files",
    r"\program files (x86)",
    r"\programdata",
    r"\system volume information",
    r"\$recycle.bin",
    r"\recovery",
    r"\boot",
    r"\$windows.~bt",
    r"\$windows.~ws",
]

FILE_BATCH = 50_000


def _is_protected(path: str) -> bool:
    p = path.lower().rstrip("\\")
    # защищаем только папки верхнего уровня диска: C:\Windows, C:\Program Files...
    _, rest = os.path.splitdrive(p)
    return rest in PROTECTED_SUFFIXES


def _ext(name: str) -> str:
    dot = name.rfind(".")
    if dot <= 0 or dot == len(name) - 1:
        return ""
    return name[dot + 1 :].lower()[:16]


def scan(root: str, con: sqlite3.Connection, progress: bool = True) -> dict:
    root = os.path.abspath(root)
    if not root.endswith("\\") and len(root) == 2:  # "C:" -> "C:\"
        root += "\\"
    root_norm = root.rstrip("\\") if len(root) > 3 else root

    t0 = time.time()
    cur = con.cursor()

    # удаляем прошлый скан этого корня
    row = cur.execute("SELECT id FROM roots WHERE path = ?", (root_norm,)).fetchone()
    if row:
        rid = row[0]
        for table in ("files", "dirs", "errors"):
            cur.execute(f"DELETE FROM {table} WHERE root_id = ?", (rid,))
        cur.execute("DELETE FROM roots WHERE id = ?", (rid,))
    cur.execute("INSERT INTO roots(path) VALUES (?)", (root_norm,))
    root_id = cur.lastrowid

    db.drop_indexes(con)

    next_dir_id = (cur.execute("SELECT COALESCE(MAX(id), 0) FROM dirs").fetchone()[0]) + 1

    # dirs: id -> [parent_id, path, name, depth, own_size, own_files, mtime, newest, protected]
    dirs: dict[int, list] = {}
    file_rows: list[tuple] = []
    errors: list[tuple] = []
    n_files = 0
    last_report = 0.0

    root_id_dir = next_dir_id
    next_dir_id += 1
    try:
        root_mtime = os.stat(root).st_mtime
    except OSError:
        root_mtime = None
    dirs[root_id_dir] = [None, root_norm, root_norm, 0, 0, 0, root_mtime, None, 0]

    stack = [root_id_dir]
    while stack:
        did = stack.pop()
        d = dirs[did]
        dpath, depth, prot = d[1], d[3], d[8]
        own_size = own_files = 0
        newest = None
        try:
            with os.scandir(dpath if not dpath.endswith(":") else dpath + "\\") as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError as ex:
                        errors.append((root_id, e.path, str(ex)))
                        continue
                    attrs = getattr(st, "st_file_attributes", 0)
                    if attrs & REPARSE:
                        continue  # junction / symlink
                    if stat_mod.S_ISDIR(st.st_mode):
                        child_path = e.path
                        child_prot = 1 if (prot or _is_protected(child_path)) else 0
                        cid = next_dir_id
                        next_dir_id += 1
                        dirs[cid] = [did, child_path, e.name, depth + 1, 0, 0, st.st_mtime, None, child_prot]
                        stack.append(cid)
                    else:
                        size = st.st_size
                        own_size += size
                        own_files += 1
                        if newest is None or st.st_mtime > newest:
                            newest = st.st_mtime
                        file_rows.append(
                            (root_id, did, e.name, _ext(e.name), size, st.st_mtime, st.st_atime, st.st_ctime)
                        )
        except OSError as ex:
            errors.append((root_id, dpath, str(ex)))

        d[4], d[5], d[7] = own_size, own_files, newest
        n_files += own_files

        if len(file_rows) >= FILE_BATCH:
            cur.executemany(
                "INSERT INTO files(root_id, dir_id, name, ext, size, mtime, atime, ctime)"
                " VALUES (?,?,?,?,?,?,?,?)",
                file_rows,
            )
            file_rows.clear()

        if progress and time.time() - last_report > 1.0:
            last_report = time.time()
            print(
                f"\r  {root_norm}: файлов {n_files:,}, папок {len(dirs):,}, ошибок {len(errors):,}   ",
                end="",
                file=sys.stderr,
                flush=True,
            )

    if file_rows:
        cur.executemany(
            "INSERT INTO files(root_id, dir_id, name, ext, size, mtime, atime, ctime)"
            " VALUES (?,?,?,?,?,?,?,?)",
            file_rows,
        )

    # Агрегация снизу вверх: id ребёнка всегда больше id родителя.
    totals: dict[int, list] = {did: [d[4], d[5], d[7]] for did, d in dirs.items()}
    for did in sorted(dirs, reverse=True):
        parent = dirs[did][0]
        if parent is None:
            continue
        t, pt = totals[did], totals[parent]
        pt[0] += t[0]
        pt[1] += t[1]
        if t[2] is not None and (pt[2] is None or t[2] > pt[2]):
            pt[2] = t[2]

    cur.executemany(
        "INSERT INTO dirs(id, root_id, parent_id, path, name, depth, own_size, own_files,"
        " total_size, total_files, mtime, newest_mtime, protected)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            (
                did,
                root_id,
                d[0],
                d[1],
                d[2],
                d[3],
                d[4],
                d[5],
                totals[did][0],
                totals[did][1],
                d[6],
                totals[did][2],
                d[8],
            )
            for did, d in dirs.items()
        ),
    )
    cur.executemany("INSERT INTO errors(root_id, path, message) VALUES (?,?,?)", errors)

    duration = time.time() - t0
    total_size = totals[root_id_dir][0]
    cur.execute(
        "UPDATE roots SET scanned_at=?, duration_s=?, total_size=?, file_count=?,"
        " dir_count=?, error_count=? WHERE id=?",
        (
            datetime.now().isoformat(timespec="seconds"),
            duration,
            total_size,
            n_files,
            len(dirs),
            len(errors),
            root_id,
        ),
    )
    con.commit()
    if progress:
        print(
            f"\r  {root_norm}: файлов {n_files:,}, папок {len(dirs):,}, "
            f"ошибок {len(errors):,} — готово за {duration:.0f} с, индексирую...",
            file=sys.stderr,
            flush=True,
        )
    db.create_indexes(con)
    con.commit()
    return {
        "root": root_norm,
        "files": n_files,
        "dirs": len(dirs),
        "errors": len(errors),
        "size": total_size,
        "seconds": round(duration, 1),
    }
