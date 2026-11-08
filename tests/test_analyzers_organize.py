"""История сканов и отчёты для наведения порядка (F1.11, F2.4–F2.9)."""

from __future__ import annotations

import os
import sqlite3
from concurrent.futures import CancelledError
from contextlib import closing
from pathlib import Path
from threading import Event

import pytest

from pcinspector import services
from pcinspector.core import db, scanner, snapshots
from pcinspector.core.models import Finding

MIB = 1024**2
OLD = 946684800


def _file(path: Path, size: int = 5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.truncate(size)
    os.utime(path, (OLD, OLD))
    return path


def test_growth_previous_snapshot_and_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, index = tmp_path / "root", tmp_path / "index.db"
    changing = _file(root / "Растёт" / "a.bin", 50 * MIB)
    shrinking = _file(root / "Меньше" / "b.bin", 65 * MIB)
    _file(root / "deep/a/b/c/d/data.bin", 50 * MIB)
    _file(root / "tiny/c.bin")
    with closing(db.connect(index)) as con:
        scanner.scan(str(root), con)
        assert con.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
        recorded = {row[0] for row in con.execute("SELECT path FROM snapshot_dirs")}
        assert str(root / "deep/a/b/c") in recorded
        assert str(root / "deep/a/b/c/d") not in recorded
        assert str(root / "tiny") not in recorded
        assert not services.run_analyzer("growth", db_path=index, min_delta=0).findings
        # Имитируем индекс v5 с датой скана в старом формате.
        with con:
            con.execute("DELETE FROM snapshots")
            con.execute("UPDATE roots SET scanned_at='2025-01-01T12:00:00'")
        _file(changing, 95 * MIB)
        _file(shrinking, 51 * MIB)
        scanner.scan(str(root), con)
        cancel = Event()
        record = snapshots.record

        def cancel_after_snapshot(connection: sqlite3.Connection, root_id: int) -> None:
            record(connection, root_id)
            if connection.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 3:
                cancel.set()

        monkeypatch.setattr(snapshots, "record", cancel_after_snapshot)
        _file(changing, 110 * MIB)
        with pytest.raises(CancelledError):
            scanner.scan(str(root), con, cancel=cancel)
        assert con.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 2
        assert con.execute("SELECT size FROM files WHERE name='a.bin'").fetchone()[0] == 95 * MIB

    report = services.run_analyzer("growth", db_path=index, min_delta="10M")
    changes = {item.path: item.meta for item in report.findings}
    assert changes[str(changing.parent)]["delta"] == 45 * MIB
    assert changes[str(shrinking.parent)]["delta"] == -14 * MIB
    filtered = services.run_analyzer("growth", db_path=index, root=str(shrinking.parent).upper(), min_delta=0)
    assert [item.path for item in filtered.findings] == [str(shrinking.parent)]


def test_organize_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, index = tmp_path / "root", tmp_path / "index.db"
    downloads = root / "Downloads"
    monkeypatch.setenv("USERPROFILE", str(root))
    installer = _file(downloads / "Widget-2.5-x64-setup.exe")
    _file(downloads / "Widget-3.5-setup.exe")
    _file(downloads / "Different-2.5-setup.exe")
    _file(downloads / "nested" / "report.txt")
    empty = root / "empty"
    (empty / "child").mkdir(parents=True)
    (root / "unreadable").mkdir()
    project = root / "Elsewhere" / "project"
    _file(project / "pyproject.toml")
    _file(project / "node_modules" / "dependency" / "package.json", 100 * MIB)
    _file(root / "Projects" / "excluded" / "package.json")
    video = _file(root / "Elsewhere" / "large.mp4", 10 * MIB)
    _file(root / "Видео" / "large.mp4", 10 * MIB)
    image = _file(root / "Elsewhere" / "disk.iso", 9 * MIB)
    _file(root / "Elsewhere" / "tiny.mkv")

    with closing(db.connect(index)) as con:
        scanned = scanner.scan(str(root), con)
        assert scanned["errors"] == 0
        with con:
            con.execute("INSERT INTO errors SELECT id,?,? FROM roots", (str(root / "unreadable"), "Ошибка"))
            con.execute(
                "INSERT INTO apps(source,reg_key,name,version) VALUES ('test','widget','Widget','2.5.0')"
            )
    settings = services.get_settings(db_path=index)
    settings["projects_root"] = str(root / "Projects")
    services.save_settings(settings, db_path=index)

    def analyze(name: str, **params: str) -> list[Finding]:
        return services.run_analyzer(name, db_path=index, **params).findings

    empties = {item.path: item for item in analyze("empty_dirs")}
    assert empties[str(empty)].meta["action"] == "quarantine"
    assert empties[str(empty / "child")].meta["action"] == "delete_empty_dir"
    assert str(root / "unreadable") not in empties
    assert str(downloads) not in empties
    preview = analyze("downloads_sort")
    assert len(preview) == 3
    target = Path(next(item.meta["target"] for item in preview if item.path == str(installer)))
    assert target.name == installer.name and target.is_relative_to(downloads)
    assert not target.parent.exists()
    assert [item.path for item in analyze("installed_installers")] == [str(installer)]
    projects = analyze("scattered_projects")
    assert [(item.path, item.safety) for item in projects] == [(str(project), "review")]
    assert projects[0].meta["dependency_size"] == 100 * MIB
    assert [item.path for item in analyze("big_media", min_size="2M")] == [str(video), str(image)]
