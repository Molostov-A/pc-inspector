"""Последовательные транзакционные миграции SQLite-индекса."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 2

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
        for next_version, schema in enumerate((SCHEMA_V1, SCHEMA_V2), start=1):
            if version < next_version:
                for statement in schema.split(";"):
                    if statement.strip():
                        con.execute(statement)
                con.execute(f"PRAGMA user_version={next_version}")
