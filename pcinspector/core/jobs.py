"""Фоновые задачи сканирования с наблюдаемым прогрессом и отменой."""

from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event, Lock
from typing import Literal
from uuid import uuid4

from . import db, scanner

JobState = Literal["queued", "running", "completed", "cancelled", "failed"]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class JobStatus:
    id: str
    state: JobState = "queued"
    progress: scanner.ScanProgress | None = None
    results: tuple[scanner.ScanResult, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(slots=True)
class _Job:
    status: JobStatus
    cancel: Event = field(default_factory=Event)


_lock = Lock()
_jobs: dict[str, _Job] = {}
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pci-scan")


def start_scan(paths: list[str], db_path: Path | str) -> str:
    job_id = uuid4().hex
    job = _Job(JobStatus(job_id))
    with _lock:
        _jobs[job_id] = job
        _pool.submit(_run_scan, job, tuple(paths), db_path)
    return job_id


def job_status(job_id: str) -> JobStatus:
    with _lock:
        status = _jobs[job_id].status
        return replace(status, results=tuple(result.copy() for result in status.results))


def cancel_job(job_id: str) -> bool:
    with _lock:
        job = _jobs[job_id]
        if job.status.state not in ("queued", "running"):
            return False
        job.cancel.set()
        if job.status.state == "queued":
            job.status = replace(job.status, state="cancelled")
        return True


def _run_scan(job: _Job, paths: tuple[str, ...], db_path: Path | str) -> None:
    def notify(progress: scanner.ScanProgress) -> None:
        with _lock:
            job.status = replace(job.status, progress=progress)

    def record_error(message: str) -> None:
        logger.warning("%s", message)
        with _lock:
            job.status = replace(job.status, errors=(*job.status.errors, message))

    with _lock:
        if job.cancel.is_set():
            return
        job.status = replace(job.status, state="running")

    state: JobState
    try:
        with closing(db.connect(db_path)) as con:
            for path in paths:
                if job.cancel.is_set():
                    raise CancelledError()
                try:
                    result = scanner.scan(path, con, notify, cancel=job.cancel)
                except (OSError, ValueError) as ex:
                    record_error(f"{path}: {ex}")
                    continue
                with _lock:
                    job.status = replace(job.status, results=(*job.status.results, result))
        state = "failed" if job.status.errors else "completed"
    except CancelledError:
        state = "cancelled"
    except sqlite3.Error as ex:
        record_error(f"Ошибка SQLite: {ex}")
        state = "failed"
    except Exception as ex:
        logger.exception("Ошибка фонового сканирования")
        record_error(str(ex))
        state = "failed"

    with _lock:
        job.status = replace(job.status, state=state)
