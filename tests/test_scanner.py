"""Сканирование временного дерева и атомарная отмена (F1.1–F1.10)."""

from __future__ import annotations

import json
import os
from concurrent.futures import CancelledError
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from pcinspector.core import db, scanner
from pcinspector.services import list_roots


def test_directory_totals(index: Path, fake_tree: Path) -> None:
    with closing(db.connect(index, readonly=True)) as con:
        root = con.execute("SELECT * FROM dirs WHERE depth = 0").fetchone()
        assert root["total_size"] == 270
        assert root["total_files"] == 6
        assert root["newest_mtime"] == 1704067200
        assert root["own_size"] == 0
        documents = con.execute(
            "SELECT * FROM dirs WHERE path = ?", (str(fake_tree / "Документы"),)
        ).fetchone()
        assert documents["own_size"] == documents["total_size"] == 136
        assert documents["own_files"] == 2


def test_junction_is_not_indexed(index: Path, fake_tree: Path) -> None:
    junction = fake_tree / "junction"
    if not junction.exists():
        pytest.skip("Junction недоступен")
    with closing(db.connect(index, readonly=True)) as con:
        assert con.execute("SELECT 1 FROM dirs WHERE path = ?", (str(junction),)).fetchone() is None


def test_access_error_is_recorded(tmp_path: Path, fake_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    denied = fake_tree / "Документы"
    scandir = os.scandir

    def fail_one(path: str) -> Any:
        if Path(path) == denied:
            raise PermissionError("Нет доступа")
        return scandir(path)

    monkeypatch.setattr(scanner.os, "scandir", fail_one)
    with closing(db.connect(tmp_path / "errors.db")) as con:
        result = scanner.scan(str(fake_tree), con)
        assert result["files"] == 4
        assert result["errors"] == 1
        row = con.execute("SELECT path, message FROM errors").fetchone()
        assert tuple(row) == (str(denied), "Нет доступа")


def test_exclusions_and_cloud_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "volume"
    root.mkdir()
    (root / "pagefile.sys").write_bytes(b"s" * 11)
    (root / "cloud.txt").write_bytes(b"c" * 7)
    (root / "regular.txt").write_bytes(b"r" * 5)
    junction = root / "junction"
    junction.mkdir()
    (junction / "alias.txt").write_bytes(b"alias")
    excluded = root / "Skip"
    excluded.mkdir()
    (excluded / "local.txt").write_bytes(b"hidden")
    scandir = os.scandir

    def reparse_entry(entry: os.DirEntry[str]) -> SimpleNamespace:
        def get_stat(*, follow_symlinks: bool) -> SimpleNamespace:
            info = entry.stat(follow_symlinks=follow_symlinks)
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_size=info.st_size,
                st_mtime=info.st_mtime,
                st_atime=info.st_atime,
                st_ctime=info.st_ctime,
                st_file_attributes=scanner.REPARSE | (0x400000 if entry.name == "cloud.txt" else 0),
            )

        return SimpleNamespace(name=entry.name, path=entry.path, stat=get_stat)

    @contextmanager
    def patched_scandir(path: str) -> Any:
        with scandir(path) as entries:
            yield (
                reparse_entry(entry) if entry.name in ("cloud.txt", "junction") else entry
                for entry in entries
            )

    monkeypatch.setattr(scanner.os, "scandir", patched_scandir)
    monkeypatch.setattr(scanner, "_is_volume_root", lambda path: Path(path) == root)
    db_path = tmp_path / "attrs.db"
    with closing(db.connect(db_path)) as con:
        with con:
            con.execute("INSERT INTO settings VALUES ('exclude_paths', ?)", (json.dumps(["skip"]),))
        result = scanner.scan(str(root), con)
        assert (result["files"], result["size"], result["system_size"], result["dirs"]) == (2, 5, 11, 1)
        file_rows = {row["name"]: row for row in con.execute("SELECT name, size, attrs FROM files_v")}
        assert file_rows["cloud.txt"]["size"] == 0
        assert file_rows["cloud.txt"]["attrs"] == scanner.REPARSE | 0x400000
        assert file_rows["regular.txt"]["size"] == 5
        with con:
            con.execute("UPDATE settings SET value=?", (json.dumps([str(excluded)]),))
        with pytest.raises(ValueError, match="исключениях"):
            scanner.scan(str(excluded), con)
    assert list_roots(db_path=db_path).findings[1].size == 11


def test_cancel_preserves_previous_data_and_indexes(index: Path, fake_tree: Path) -> None:
    cancel = Event()
    (fake_tree / "новый.txt").write_bytes(b"new")

    def cancel_at_end(update: scanner.ScanProgress) -> None:
        if update.files == 7:
            with closing(db.connect(index, readonly=True)) as reader:
                assert reader.execute("SELECT file_count FROM roots").fetchone()[0] == 6
                assert reader.execute("SELECT count(*) FROM files").fetchone()[0] == 6
            cancel.set()

    with closing(db.connect(index)) as con:
        sync = con.execute("PRAGMA synchronous").fetchone()[0]
        with pytest.raises(CancelledError):
            scanner.scan(str(fake_tree), con, cancel_at_end, cancel=cancel)
        assert con.execute("PRAGMA synchronous").fetchone()[0] == sync
        assert con.execute("SELECT file_count FROM roots").fetchone()[0] == 6
        assert con.execute("SELECT count(*) FROM files").fetchone()[0] == 6
        assert "ix_dirs_parent" in {row["name"] for row in con.execute("PRAGMA index_list(dirs)")}
        with con:
            con.execute("CREATE TABLE file_hashes (path TEXT)")
            con.execute("INSERT INTO file_hashes VALUES ('existing')")
        assert scanner.scan(str(fake_tree).upper(), con)["files"] == 7
        assert con.execute("SELECT count(*) FROM roots").fetchone()[0] == 1
        assert con.execute("SELECT path FROM file_hashes").fetchone()[0] == "existing"
