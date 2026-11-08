"""Единая точка открытия баз для функций фасада."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from .. import config
from ..core import db

DbPath = Path | str | None


def resolve(db_path: DbPath) -> Path:
    return Path(db_path if db_path is not None else os.environ.get("PCI_DB", config.DB_PATH)).absolute()


def _open_read(db_path: DbPath) -> sqlite3.Connection:
    path = resolve(db_path)
    try:
        return db.connect(path, readonly=True)
    except sqlite3.OperationalError:
        if path.exists():
            raise
        return db.empty_index()


@contextmanager
def read(db_path: DbPath) -> Iterator[sqlite3.Connection]:
    """Согласованный снимок индекса и состояния только для чтения."""
    with closing(_open_read(db_path)) as con, con:
        con.execute("BEGIN")
        yield con


@contextmanager
def state(db_path: DbPath) -> Iterator[sqlite3.Connection]:
    """Запись планов, настроек и кэшей; скан ей не мешает."""
    with closing(db.connect_state(resolve(db_path))) as con:
        yield con


@contextmanager
def state_snapshot(db_path: DbPath) -> Iterator[sqlite3.Connection]:
    with closing(db.connect_state(resolve(db_path))) as con, con:
        con.execute("BEGIN")
        yield con
