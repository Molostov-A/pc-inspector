"""Подключения к двум базам: index.db (результаты скана) и .state.db (планы, настройки, кэши).

Скан держит блокировку записи только на индексе, поэтому планы и настройки остаются доступны.
Таблицы обеих баз видны в запросах без префикса.

  connect(readonly=True)  — отчёты, обе базы только для чтения
  connect_index()         — сканер, индекс для записи, состояние для чтения
  connect_state()         — планы и коллекторы, состояние для записи, индекс для чтения
  connect()               — обе базы для записи, только для служебных задач и тестов
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from ..config import DB_PATH as DEFAULT_DB
from . import migrations

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_dirs_parent ON dirs(parent_id)",
    "CREATE INDEX IF NOT EXISTS ix_dirs_path ON dirs(path COLLATE NOCASE)",
    "CREATE INDEX IF NOT EXISTS ix_dirs_size ON dirs(total_size)",
    "CREATE INDEX IF NOT EXISTS ix_dirs_name ON dirs(name COLLATE NOCASE)",
    "CREATE INDEX IF NOT EXISTS ix_dirs_root ON dirs(root_id)",
    "CREATE INDEX IF NOT EXISTS ix_files_dir ON files(dir_id)",
    "CREATE INDEX IF NOT EXISTS ix_files_size ON files(size)",
    "CREATE INDEX IF NOT EXISTS ix_files_ext ON files(ext)",
    "CREATE INDEX IF NOT EXISTS ix_files_root ON files(root_id)",
]
WRITE_TIMEOUT = 30.0


def state_path(index: Path | str) -> Path:
    path = Path(index).absolute()
    return path.with_name(f"{path.stem}.state.db")


def _uri(path: Path, *, readonly: bool) -> str:
    return f"{path.as_uri()}?mode={'ro' if readonly else 'rw'}"


def _finish(con: sqlite3.Connection) -> sqlite3.Connection:
    con.row_factory = sqlite3.Row
    con.create_function("pci_casefold", 1, str.casefold, deterministic=True)
    return con


def _prepare(index: Path) -> None:
    """Обновить схему и выделить базу состояния, если это ещё не сделано."""
    state = state_path(index)
    if index.exists() and state.exists():
        with closing(sqlite3.connect(_uri(index, readonly=True), uri=True)) as con:
            if migrations.is_current(con):
                return
    index.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(index, timeout=WRITE_TIMEOUT)
    try:
        migrations.migrate(con)
        con.execute("PRAGMA journal_mode=WAL")
        migrations.split_state(con, state)
    finally:
        con.close()


def _open(main: Path, other: Path, alias: str, *, main_ro: bool, other_ro: bool) -> sqlite3.Connection:
    con = sqlite3.connect(_uri(main, readonly=main_ro), uri=True, timeout=WRITE_TIMEOUT)
    try:
        con.execute(f"ATTACH DATABASE ? AS {alias}", (_uri(other, readonly=other_ro),))
        con.execute("PRAGMA foreign_keys=ON")
        if main_ro and other_ro:
            con.execute("PRAGMA query_only=ON")
    except BaseException:
        con.close()
        raise
    return _finish(con)


def connect(db_path: Path | str = DEFAULT_DB, readonly: bool = False) -> sqlite3.Connection:
    index = Path(db_path).absolute()
    if not readonly or index.exists():
        _prepare(index)
    return _open(index, state_path(index), "state", main_ro=readonly, other_ro=readonly)


def connect_index(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    index = Path(db_path).absolute()
    _prepare(index)
    return _open(index, state_path(index), "state", main_ro=False, other_ro=True)


def connect_state(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    index = Path(db_path).absolute()
    _prepare(index)
    return _open(state_path(index), index, "idx", main_ro=False, other_ro=True)


def empty_index() -> sqlite3.Connection:
    """Открыть пустой индекс в памяти для отчётов до первого сканирования."""
    con = sqlite3.connect(":memory:")
    migrations.migrate(con)
    _finish(con)
    con.execute("PRAGMA query_only=ON")
    return con


def has_table(con: sqlite3.Connection, name: str) -> bool:
    """Проверить таблицу во всех подключённых базах."""
    schemas = [row[1] for row in con.execute("PRAGMA database_list")]
    return any(
        con.execute(f'SELECT 1 FROM "{schema}".sqlite_schema WHERE name=?', (name,)).fetchone()
        for schema in schemas
    )


def drop_indexes(con: sqlite3.Connection) -> None:
    for stmt in INDEXES:
        name = stmt.split()[5]
        con.execute(f"DROP INDEX IF EXISTS {name}")


def create_indexes(con: sqlite3.Connection, *, analyze: bool = True) -> None:
    for stmt in INDEXES:
        con.execute(stmt)
    if analyze:
        con.execute("ANALYZE main")
