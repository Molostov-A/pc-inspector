"""Снимки метаданных корня, сохраняемые вместе со сканированием."""

from __future__ import annotations

import sqlite3
from datetime import datetime

MAX_DEPTH = 4
MIN_SIZE = 50 * 1024**2


def record(con: sqlite3.Connection, root_id: int) -> None:
    root = con.execute(
        "SELECT path,scanned_at,total_size,file_count,error_count FROM roots WHERE id=?",
        (root_id,),
    ).fetchone()
    if root is None or root["scanned_at"] is None:
        return
    raw = root["scanned_at"]
    try:
        timestamp = float(raw)
    except (ValueError, TypeError):
        try:
            timestamp = datetime.fromisoformat(raw).timestamp()
        except (ValueError, TypeError, OSError, OverflowError):
            return
    added = con.execute(
        "INSERT INTO snapshots(root_path,root_key,taken_at,total_size,file_count,error_count) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(root_key,taken_at) DO NOTHING RETURNING id",
        (
            root["path"],
            root["path"].casefold(),
            timestamp,
            root["total_size"] or 0,
            root["file_count"] or 0,
            root["error_count"] or 0,
        ),
    ).fetchone()
    if added is not None:
        con.execute(
            "INSERT OR REPLACE INTO snapshot_dirs(snapshot_id,path,path_key,total_size,total_files) "
            "SELECT ?,path,pci_casefold(path),total_size,total_files FROM dirs "
            "WHERE root_id=? AND depth<=? AND total_size>=?",
            (added["id"], root_id, MAX_DEPTH, MIN_SIZE),
        )
