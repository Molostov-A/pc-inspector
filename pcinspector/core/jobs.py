"""Фоновые задачи сканирования с наблюдаемым прогрессом и отменой."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event, Lock
from typing import Literal, TypedDict
from uuid import uuid4

from . import db, scanner

JobState = Literal["queued", "running", "completed", "cancelled", "failed"]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HashProgress:
    phase: Literal["candidates", "partial", "full"]
    processed: int = 0
    total: int = 0
    cached: int = 0
    errors: int = 0


class HashResult(TypedDict):
    root: str
    candidates: int
    partial_read: int
    full_read: int
    cached: int
    hardlinks: int
    errors: int


HashWork = Callable[[Callable[[HashProgress], None], Event], HashResult]


@dataclass(frozen=True, slots=True)
class JobStatus:
    id: str
    state: JobState = "queued"
    progress: scanner.ScanProgress | None = None
    results: tuple[scanner.ScanResult, ...] = ()
    errors: tuple[str, ...] = ()
    kind: Literal["scan", "hash"] = "scan"
    hash_progress: HashProgress | None = None
    hash_result: HashResult | None = None


@dataclass(slots=True)
class _Job:
    status: JobStatus
    cancel: Event = field(default_factory=Event)


_lock = Lock()
_jobs: dict[str, _Job] = {}
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pci-scan")


def start_scan(paths: list[str], db_path: Path | str) -> str:
    roots = tuple(paths)
    return _start("scan", lambda job: _scan_work(job, roots, db_path))


def start_hash(work: HashWork) -> str:
    def run(job: _Job) -> None:
        def notify(progress: HashProgress) -> None:
            with _lock:
                job.status = replace(job.status, hash_progress=progress)

        result = work(notify, job.cancel)
        with _lock:
            job.status = replace(job.status, hash_result=result)

    return _start("hash", run)


def _start(kind: Literal["scan", "hash"], work: Callable[[_Job], None]) -> str:
    job_id = uuid4().hex
    job = _Job(JobStatus(job_id, kind=kind))
    with _lock:
        _jobs[job_id] = job
        _pool.submit(_run, job, work)
    return job_id


def job_status(job_id: str) -> JobStatus:
    with _lock:
        status = _jobs[job_id].status
        return replace(
            status,
            results=tuple(result.copy() for result in status.results),
            hash_result=status.hash_result.copy() if status.hash_result is not None else None,
        )


def cancel_job(job_id: str) -> bool:
    with _lock:
        job = _jobs[job_id]
        if job.status.state not in ("queued", "running"):
            return False
        job.cancel.set()
        if job.status.state == "queued":
            job.status = replace(job.status, state="cancelled")
        return True


def _record_error(job: _Job, message: str) -> None:
    logger.warning("%s", message)
    with _lock:
        job.status = replace(job.status, errors=(*job.status.errors, message))


def _scan_work(job: _Job, paths: tuple[str, ...], db_path: Path | str) -> None:
    def notify(progress: scanner.ScanProgress) -> None:
        with _lock:
            job.status = replace(job.status, progress=progress)

    with closing(db.connect(db_path)) as con:
        for path in paths:
            if job.cancel.is_set():
                raise CancelledError()
            try:
                result = scanner.scan(path, con, notify, cancel=job.cancel)
            except (OSError, ValueError) as ex:
                _record_error(job, f"{path}: {ex}")
                continue
            with _lock:
                job.status = replace(job.status, results=(*job.status.results, result))


def _run(job: _Job, work: Callable[[_Job], None]) -> None:
    with _lock:
        if job.cancel.is_set():
            return
        job.status = replace(job.status, state="running")

    state: JobState
    try:
        work(job)
        state = "failed" if job.status.errors else "completed"
    except CancelledError:
        state = "cancelled"
    except sqlite3.Error as ex:
        _record_error(job, f"Ошибка SQLite: {ex}")
        state = "failed"
    except Exception as ex:
        logger.exception("Ошибка фоновой задачи")
        _record_error(job, str(ex))
        state = "failed"

    with _lock:
        job.status = replace(job.status, state=state)
