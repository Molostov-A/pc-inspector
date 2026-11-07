"""SQLite-индекс файловой системы."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..config import DB_PATH as DEFAULT_DB

SCHEMA_VERSION = 1  # увеличивать при изменении схемы (см. AGENTS.md → «Схема БД»)

SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (
    id          INTEGER PRIMARY KEY,
    path        TEXT UNIQUE NOT NULL,
    scanned_at  TEXT,
    duration_s  REAL,
    total_size  INTEGER,
    file_count  INTEGER,
    dir_count   INTEGER,
    error_count INTEGER
);

CREATE TABLE IF NOT EXISTS dirs (
    id           INTEGER PRIMARY KEY,
    root_id      INTEGER NOT NULL,
    parent_id    INTEGER,
    path         TEXT NOT NULL,
    name         TEXT NOT NULL,
    depth        INTEGER NOT NULL,
    own_size     INTEGER NOT NULL DEFAULT 0,
    own_files    INTEGER NOT NULL DEFAULT 0,
    total_size   INTEGER NOT NULL DEFAULT 0,
    total_files  INTEGER NOT NULL DEFAULT 0,
    mtime        REAL,
    newest_mtime REAL,   -- самая свежая дата изменения файла во всём поддереве
    protected    INTEGER NOT NULL DEFAULT 0  -- системная зона: не предлагать к удалению
);

CREATE TABLE IF NOT EXISTS files (
    id      INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL,
    dir_id  INTEGER NOT NULL,
    name    TEXT NOT NULL,
    ext     TEXT NOT NULL,
    size    INTEGER NOT NULL,
    mtime   REAL,
    atime   REAL,
    ctime   REAL   -- на Windows это дата создания
);

CREATE TABLE IF NOT EXISTS errors (
    root_id INTEGER NOT NULL,
    path    TEXT NOT NULL,
    message TEXT
);

CREATE VIEW IF NOT EXISTS files_v AS
    SELECT f.id, f.root_id, f.dir_id,
           CASE WHEN substr(d.path, -1) = '\\' THEN d.path || f.name
                ELSE d.path || '\\' || f.name END AS path,
           f.name, f.ext,
           f.size, f.mtime, f.atime, f.ctime, d.protected
    FROM files f JOIN dirs d ON d.id = f.dir_id;
"""

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
    db_path = Path(db_path)
    if readonly:
        con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db_path)
        con.executescript(SCHEMA)
        con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    return con


def drop_indexes(con: sqlite3.Connection) -> None:
    for stmt in INDEXES:
        name = stmt.split()[5]
        con.execute(f"DROP INDEX IF EXISTS {name}")


def create_indexes(con: sqlite3.Connection) -> None:
    for stmt in INDEXES:
        con.execute(stmt)
    con.execute("ANALYZE")
