"""Произвольные запросы только для чтения с ограничением времени."""

from __future__ import annotations

import math
import re
import sqlite3
import time
from contextlib import closing
from typing import Any

from ..core import db
from .workspace import DbPath, resolve

TIME_LIMIT = 10.0
SECRET_COLUMNS = frozenset({("plans", "confirm_hash")})


def _sql_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _authorize_sql(
    action: int,
    first: str | None,
    second: str | None,
    _database: str | None,
    _source: str | None,
) -> int:
    if action == sqlite3.SQLITE_READ and (first, second) in SECRET_COLUMNS:
        return sqlite3.SQLITE_IGNORE
    allowed = (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE)
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def sql_readonly(query: str, limit: int = 500, *, db_path: DbPath = None) -> list[dict[str, Any]]:
    keyword = re.match(r"[A-Za-z]+", query.lstrip())
    if keyword is None or keyword[0].upper() not in {"SELECT", "WITH", "EXPLAIN"}:
        raise ValueError("Разрешены запросы SELECT, WITH и EXPLAIN")
    if not 1 <= limit <= 10_000:
        raise ValueError("Лимит SQL должен быть от 1 до 10 000 строк")
    with closing(db.connect(resolve(db_path), readonly=True)) as con:
        con.set_authorizer(_authorize_sql)
        deadline = time.monotonic() + TIME_LIMIT
        con.set_progress_handler(lambda: time.monotonic() > deadline, 100_000)
        try:
            return [
                {name: _sql_value(value) for name, value in dict(row).items()}
                for row in con.execute(query).fetchmany(limit)
            ]
        except sqlite3.OperationalError as ex:
            if ex.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT:
                raise ValueError(f"Запрос выполнялся дольше {TIME_LIMIT:.0f} с и был остановлен") from ex
            raise
