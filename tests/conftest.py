"""Фикстуры с временным деревом файлов и SQLite-индексом (ТЗ §10)."""

from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path

import pytest

from pcinspector.core import db, scanner


@pytest.fixture
def fake_tree(tmp_path: Path) -> Path:
    root = tmp_path / "Дерево"
    old, recent = 946684800, 1704067200
    files = {
        "Документы/отчёт.txt": (b"x" * 64, recent),
        "Документы/копия.bin": (b"duplicate" * 8, recent),
        "Архив/копия.bin": (b"duplicate" * 8, old),
        "node_modules/пакет/index.js": (b"x" * 32, old),
        ".venv/pyvenv.cfg": (b"home = Python\n", old),
        ".venv/Lib/cache.bin": (b"x" * 16, old),
    }
    (root / "Пустая").mkdir(parents=True)
    for relative, (content, mtime) in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.utime(path, (mtime, mtime))

    if os.name == "nt":
        from _winapi import CreateJunction

        try:
            CreateJunction(str(root / "junction"), str(root / "Документы"))
        except OSError:
            # На некоторых файловых системах junction недоступны.
            pass
    return root


@pytest.fixture
def index(fake_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "index.db"
    monkeypatch.setenv("PCI_DB", str(db_path))
    with closing(db.connect(db_path)) as con:
        scanner.scan(str(fake_tree), con)
    return db_path
