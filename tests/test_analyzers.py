"""Проверки всех базовых анализаторов на временном индексе (F2)."""

from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path

import pytest

from pcinspector import services
from pcinspector.analyzers import stale
from pcinspector.core import db, scanner


def test_roots_and_tree(index: Path, fake_tree: Path) -> None:
    roots = services.list_roots(db_path=index)
    assert roots.findings[0].path == str(fake_tree)
    assert roots.findings[0].size == 270
    assert isinstance(roots.findings[0].meta["scanned_at"], float)
    assert roots.findings[0].meta["disk_free"] > 0
    report = services.tree(str(fake_tree).upper().replace("\\", "/"), limit=2, db_path=index)
    assert [item.title for item in report.findings] == ["Документы", "Архив"]
    assert report.summary["total_size"] == 270
    own = services.tree(str(fake_tree / "Документы"), db_path=index)
    assert own.findings[0].title == "[файлы в самой папке]"
    assert own.findings[0].size == 136


def test_top_files(index: Path, fake_tree: Path) -> None:
    report = services.run_analyzer(
        "top_files", root=str(fake_tree / "дОКУМЕНТЫ"), ext=".BIN", limit=1, db_path=index
    )
    assert [(item.title, item.size) for item in report.findings] == [("копия.bin", 72)]


def test_types_and_choice(index: Path) -> None:
    report = services.run_analyzer("types", by="ext", db_path=index)
    assert report.findings[0].title == "bin"
    assert report.findings[0].size == 160
    assert report.total_size == 270
    assert len(services.run_analyzer("types", limit=1, db_path=index).findings) == 1
    with pytest.raises(ValueError, match="выберите"):
        services.run_analyzer("types", by="unknown", db_path=index)


def test_stale_files(index: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stale.time, "time", lambda: 1710000000)
    report = services.run_analyzer("stale_files", min_size="50", db_path=index)
    assert [(item.title, item.size) for item in report.findings] == [("копия.bin", 72)]
    assert report.findings[0].safety == "review"


def test_forgotten_dirs(index: Path, fake_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stale.time, "time", lambda: 1710000000)
    report = services.run_analyzer("forgotten_dirs", min_size=1, db_path=index)
    assert {item.title for item in report.findings} == {"Архив", "node_modules", ".venv"}
    filtered = services.run_analyzer(
        "forgotten_dirs", root=str(fake_tree / "node_modules" / "пакет"), min_size=1, db_path=index
    )
    assert [item.title for item in filtered.findings] == ["пакет"]


def test_junk_nested_is_counted_once(index: Path, fake_tree: Path) -> None:
    (fake_tree / "node_modules" / "large.dmp").write_bytes(b"x" * 80)
    sibling = fake_tree / "node_modules-sibling.dmp"
    sibling.write_bytes(b"x" * 21)
    with closing(db.connect(index)) as con:
        scanner.scan(str(fake_tree), con)
    report = services.run_analyzer("junk", min_size=1, db_path=index)
    assert report.summary["items"] == 3
    assert report.total_size == 163
    flat = services.run_analyzer("junk", min_size=1, group=False, limit=2, db_path=index)
    assert [item.title for item in flat.findings] == ["node_modules", ".venv"]
    assert flat.summary == report.summary


def test_junk_never_suggests_protected_items(index: Path, fake_tree: Path) -> None:
    path = fake_tree / "node_modules"
    (path / "protected.dmp").write_bytes(b"sample")
    with closing(db.connect(index)) as con:
        scanner.scan(str(fake_tree), con)
        with con:
            con.execute("UPDATE dirs SET protected=1 WHERE path=?", (str(path),))
    report = services.run_analyzer("junk", min_size=1, group=False, db_path=index)
    assert [item.title for item in report.findings] == [".venv"]
    assert all(item.path != os.fspath(path) for item in report.findings)
