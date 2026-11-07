"""Сохранение старых индексов и версия схемы (N3)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from pcinspector.core import db, migrations


@pytest.mark.parametrize("version", [0, 1])
def test_upgrade_preserves_existing_data(tmp_path: Path, version: int) -> None:
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as con, con:
        con.executescript(migrations.SCHEMA_V1)
        con.execute(f"PRAGMA user_version={version}")
        con.execute("INSERT INTO roots(id, path) VALUES (1, ?)", (str(tmp_path),))
        con.execute(
            "INSERT INTO dirs(id, root_id, path, name, depth) VALUES (1, 1, ?, 'old', 0)",
            (str(tmp_path),),
        )
        con.execute("INSERT INTO files(root_id, dir_id, name, ext, size) VALUES (1, 1, 'a.bin', 'bin', 12)")
    for _ in range(2):
        with closing(db.connect(path)) as con:
            assert con.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
            assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            row = con.execute("SELECT path, size, attrs FROM files_v").fetchone()
            assert tuple(row) == (str(tmp_path / "a.bin"), 12, 0)


def test_newer_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    with closing(sqlite3.connect(path)) as con:
        con.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="новее"):
        db.connect(path)
    with closing(sqlite3.connect(path)) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 99
