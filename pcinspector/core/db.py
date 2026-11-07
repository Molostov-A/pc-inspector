"""SQLite-индекс файловой системы."""

from __future__ import annotations

import sqlite3
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


def connect(db_path: Path | str = DEFAULT_DB, readonly: bool = False) -> sqlite3.Connection:
    db_path = Path(db_path).absolute()
    if readonly:
        con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        con.execute("PRAGMA query_only=ON")
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db_path)
        try:
            migrations.migrate(con)
            con.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            con.close()
            raise
    con.row_factory = sqlite3.Row
    con.create_function("pci_casefold", 1, str.casefold, deterministic=True)
    return con


def empty_index() -> sqlite3.Connection:
    """Открыть пустой индекс в памяти для отчётов до первого сканирования."""
    con = sqlite3.connect(":memory:")
    migrations.migrate(con)
    con.row_factory = sqlite3.Row
    con.create_function("pci_casefold", 1, str.casefold, deterministic=True)
    con.execute("PRAGMA query_only=ON")
    return con


def drop_indexes(con: sqlite3.Connection) -> None:
    for stmt in INDEXES:
        name = stmt.split()[5]
        con.execute(f"DROP INDEX IF EXISTS {name}")


def create_indexes(con: sqlite3.Connection) -> None:
    for stmt in INDEXES:
        con.execute(stmt)
    con.execute("ANALYZE")
