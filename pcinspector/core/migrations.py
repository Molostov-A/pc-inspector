"""Последовательные транзакционные миграции SQLite-индекса."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 7
STATE_TABLES = (
    "settings",
    "plans",
    "plan_items",
    "actions_log",
    "file_hashes",
    "hash_runs",
    "apps",
    "app_usage",
)

SCHEMA_V1 = """
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

SCHEMA_V2 = r"""
ALTER TABLE files ADD COLUMN attrs INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roots ADD COLUMN system_size INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roots ADD COLUMN system_files INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roots ADD COLUMN disk_total INTEGER;
ALTER TABLE roots ADD COLUMN disk_free INTEGER;
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
DROP VIEW files_v;
CREATE VIEW files_v AS
    SELECT f.id, f.root_id, f.dir_id,
           CASE WHEN substr(d.path, -1) = '\' THEN d.path || f.name
                ELSE d.path || '\' || f.name END AS path,
           f.name, f.ext, f.size, f.mtime, f.atime, f.ctime, d.protected, f.attrs
    FROM files f JOIN dirs d ON d.id = f.dir_id;
"""


SCHEMA_V3 = """
CREATE TABLE plans (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT NOT NULL CHECK (author IN ('user', 'ai')),
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    revision INTEGER NOT NULL DEFAULT 0,
    storage_key TEXT NOT NULL UNIQUE,
    confirm_hash TEXT,
    confirm_expires REAL
);
CREATE TABLE plan_items (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES plans(id),
    action TEXT NOT NULL,
    path TEXT NOT NULL,
    target TEXT,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    expected_mtime REAL NOT NULL,
    fingerprint TEXT NOT NULL,
    safety TEXT NOT NULL,
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    selected INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT
);
CREATE INDEX ix_plan_items_plan ON plan_items(plan_id);
CREATE TABLE actions_log (
    id INTEGER PRIMARY KEY,
    plan_item_id INTEGER NOT NULL UNIQUE REFERENCES plan_items(id),
    at REAL NOT NULL,
    action TEXT NOT NULL,
    src TEXT NOT NULL,
    dst TEXT,
    size INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    restorable INTEGER NOT NULL DEFAULT 0,
    restored_at REAL,
    purged_at REAL
);
"""


SCHEMA_V4 = """
CREATE TABLE file_hashes (
    path TEXT PRIMARY KEY COLLATE NOCASE,
    directory TEXT NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    ctime REAL NOT NULL,
    identity TEXT NOT NULL,
    partial_hash TEXT NOT NULL,
    full_hash TEXT
);
CREATE INDEX ix_file_hashes_full ON file_hashes(size, full_hash) WHERE full_hash IS NOT NULL;
CREATE TABLE hash_runs (
    root TEXT NOT NULL COLLATE NOCASE,
    min_size INTEGER NOT NULL,
    started_at REAL NOT NULL,
    errors INTEGER NOT NULL,
    PRIMARY KEY(root, min_size)
);
"""


SCHEMA_V5 = """
CREATE TABLE apps (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    reg_key TEXT NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL,
    publisher TEXT,
    version TEXT,
    install_date REAL,
    install_location TEXT,
    display_icon TEXT,
    estimated_size INTEGER,
    measured_size INTEGER,
    uninstall_cmd TEXT,
    quiet_uninstall_cmd TEXT,
    is_system_component INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source, reg_key)
);
CREATE TABLE app_usage (
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    exe_name TEXT NOT NULL,
    exe_path TEXT,
    last_run REAL NOT NULL,
    run_count INTEGER,
    source TEXT NOT NULL,
    PRIMARY KEY(app_id, source)
);
"""


SCHEMA_V6 = """
CREATE TABLE snapshots (
    id INTEGER PRIMARY KEY,
    root_path TEXT NOT NULL,
    root_key TEXT NOT NULL,
    taken_at REAL NOT NULL,
    total_size INTEGER NOT NULL,
    file_count INTEGER NOT NULL,
    error_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(root_key,taken_at)
);
CREATE TABLE snapshot_dirs (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    path_key TEXT NOT NULL,
    total_size INTEGER NOT NULL,
    total_files INTEGER NOT NULL,
    PRIMARY KEY(snapshot_id,path_key)
);
"""


def migrate(con: sqlite3.Connection) -> None:
    """Довести схему до текущей версии без потери ранее записанных строк."""
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return

    con.execute("BEGIN IMMEDIATE")
    with con:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError(f"Версия индекса {version} новее поддерживаемой ({SCHEMA_VERSION})")
        for next_version, schema in enumerate(
            (SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5, SCHEMA_V6, ""), start=1
        ):
            if version < next_version:
                for statement in schema.split(";"):
                    if statement.strip():
                        con.execute(statement)
                con.execute(f"PRAGMA user_version={next_version}")


def _state_objects(con: sqlite3.Connection) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(STATE_TABLES))
    return con.execute(
        f"SELECT type, name, tbl_name, sql FROM main.sqlite_schema "
        f"WHERE tbl_name IN ({marks}) AND sql IS NOT NULL ORDER BY type DESC",
        STATE_TABLES,
    ).fetchall()


def is_current(con: sqlite3.Connection) -> bool:
    """Проверить, что схема актуальна и состояние уже вынесено из индекса."""
    return con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION and not _state_objects(con)


def split_state(con: sqlite3.Connection, state: Path) -> None:
    """Перенести таблицы состояния в отдельную базу. Повторный запуск безопасен."""
    objects = _state_objects(con)
    con.execute("ATTACH DATABASE ? AS state", (str(state),))
    try:
        con.execute("PRAGMA state.journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=OFF")
        tables = sorted({row[2] for row in objects})
        con.execute("BEGIN IMMEDIATE")
        with con:
            for row in objects:
                # SQLite хранит текст CREATE уже без IF NOT EXISTS и без имени схемы.
                statement = re.sub(
                    r"^CREATE (TABLE|UNIQUE INDEX|INDEX) ", r"CREATE \1 IF NOT EXISTS state.", row[3], count=1
                )
                con.execute(statement)
            for table in tables:
                con.execute(f"INSERT OR REPLACE INTO state.{table} SELECT * FROM main.{table}")
            for table in tables:
                con.execute(f"DROP TABLE main.{table}")
        con.execute("PRAGMA state.user_version=1")
    finally:
        con.execute("DETACH DATABASE state")
