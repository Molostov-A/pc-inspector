"""Общий фасад сканирования, анализа и чтения индекса для интерфейсов."""

from __future__ import annotations

import math
import os
import re
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import config
from .analyzers import REGISTRY, get
from .core import db, jobs, scanner
from .core.models import Report


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


def run_analyzer(analyzer_id: str, *, db_path: Path | str | None = None, **params: Any) -> Report:
    analyzer = get(analyzer_id)
    with closing(db.connect(_db_path(db_path), readonly=True)) as con:
        return analyzer(con, **params)


def list_roots(*, db_path: Path | str | None = None) -> Report:
    return run_analyzer("roots", db_path=db_path)


def tree(path: str, limit: int = 100, *, db_path: Path | str | None = None) -> Report:
    return run_analyzer("tree", path=path, limit=limit, db_path=db_path)


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
