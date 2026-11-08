"""Фоновые задачи: скан, поиск дубликатов и сбор сведений о программах."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from threading import Event
from typing import Any

from ..collectors import hashing
from ..core import db, jobs, scanner
from ..core.fmt import parse_size
from .reports import get_settings, list_roots
from .workspace import DbPath, resolve


def start_scan(paths: list[str], *, db_path: DbPath = None) -> str:
    roots = {}
    for path in paths or get_settings(db_path=db_path)["scan_roots"] or scanner.local_ntfs_roots():
        root = scanner.normalize_root(path)
        roots.setdefault(root.casefold(), root)
    if not roots:
        raise ValueError("Нет доступных локальных дисков NTFS. Укажите папку сканирования.")
    return jobs.start_scan(list(roots.values()), resolve(db_path))


def job_status(job_id: str) -> jobs.JobStatus:
    return jobs.job_status(job_id)


def start_hash(
    root: str = "",
    min_size: str | int = hashing.DEFAULT_MIN_SIZE,
    *,
    db_path: DbPath = None,
) -> str:
    minimum = parse_size(min_size)
    if not 0 <= minimum <= 2**63 - 1:
        raise ValueError("Укажите допустимый неотрицательный минимальный размер")
    scope = scanner.normalize_root(root) if root else ""
    path = resolve(db_path)

    def work(notify: Callable[[jobs.HashProgress], None], cancel: Event) -> jobs.HashResult:
        with closing(db.connect_state(path)) as con:
            return hashing.collect(con, scope, minimum, notify, cancel)

    return jobs.start_hash(work)


def cancel_job(job_id: str) -> bool:
    return jobs.cancel_job(job_id)


def collect_apps(*, db_path: DbPath = None) -> str:
    from ..collectors import usage

    path = resolve(db_path)

    def work(cancel: Event) -> jobs.AppsResult:
        with closing(db.connect_state(path)) as con:
            count, notes = usage.collect(con, cancel)
            return {"count": count, "notes": notes}

    return jobs.start_apps(work)


def list_drives(*, db_path: DbPath = None) -> list[dict[str, Any]]:
    indexed = {item.path.casefold() for item in list_roots(db_path=db_path).findings if item.path}
    return [{**drive, "scanned": drive["path"].casefold() in indexed} for drive in scanner.local_drives()]
