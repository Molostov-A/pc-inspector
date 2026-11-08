"""Адреса карантина и выборка элементов для просмотра и ручной очистки."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..core import safety


def location(plan: sqlite3.Row, source: Path) -> tuple[Path, Path]:
    base = safety.quarantine_root(source) / f"{plan['id']}-{plan['storage_key']}"
    return base, base / "payload" / source.relative_to(source.anchor)


def entries(
    con: sqlite3.Connection,
    *,
    cutoff: float | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT * FROM actions_log WHERE action='quarantine' AND status='done' "
        "AND restored_at IS NULL AND purged_at IS NULL AND (? IS NULL OR at <= ?) "
        "ORDER BY at, id LIMIT ?",
        (cutoff, cutoff, limit),
    )
    return [dict(row) for row in rows]


def usage(con: sqlite3.Connection) -> int:
    return con.execute(
        "SELECT COALESCE(sum(size),0) FROM actions_log WHERE action='quarantine' "
        "AND status='done' AND restored_at IS NULL AND purged_at IS NULL"
    ).fetchone()[0]
