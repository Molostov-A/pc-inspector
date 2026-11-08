"""Фоновые задачи, чтение SQL и обычная точка входа CLI (F1.7, F1.8, F4, §9.8)."""

from __future__ import annotations

import json
import runpy
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event

import pytest

from pcinspector import config, services
from pcinspector.core import jobs, scanner


def _wait(job_id: str) -> jobs.JobStatus:
    deadline = time.monotonic() + 5
    while (status := services.job_status(job_id)).state in ("queued", "running"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    return status


def test_jobs_report_progress_and_cancel(
    index: Path, fake_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, resume = Event(), Event()
    original_scan = scanner.scan

    def pause(
        root: str, con: sqlite3.Connection, progress: Callable[[scanner.ScanProgress], None], *, cancel: Event
    ) -> scanner.ScanResult:
        def notify(update: scanner.ScanProgress) -> None:
            progress(update)
            entered.set()
            assert resume.wait(5)

        return original_scan(root, con, notify, cancel=cancel)

    monkeypatch.setattr(scanner, "scan", pause)
    running = services.start_scan([str(fake_tree)], db_path=index)
    try:
        assert entered.wait(5)
        status = services.job_status(running)
        assert status.state == "running"
        assert status.progress is not None
        assert status.progress.path == str(fake_tree)
        assert status.progress.dirs == 1
        queued = services.start_scan([str(fake_tree)], db_path=index)
        assert services.job_status(queued).state == "queued"
        assert services.cancel_job(queued)
        assert _wait(queued).state == "cancelled"
        assert services.cancel_job(running)
    finally:
        resume.set()
    assert _wait(running).state == "cancelled"
    assert not services.cancel_job(running)
    assert services.list_roots(db_path=index).findings[0].meta["file_count"] == 6


def test_default_scan_discovers_drives(index: Path, fake_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scanner, "local_ntfs_roots", lambda: [str(fake_tree), str(fake_tree).upper()])
    status = _wait(services.start_scan([], db_path=index))
    assert status.state == "completed"
    assert len(status.results) == 1


def test_sql_readonly(index: Path) -> None:
    assert services.sql_readonly("SELECT name FROM files ORDER BY id", limit=2, db_path=index)[0]["name"]
    with pytest.raises(ValueError, match="Разрешены"):
        services.sql_readonly("DELETE FROM files", db_path=index)
    with pytest.raises(ValueError, match="Разрешены"):
        services.sql_readonly("ATTACH DATABASE 'another.db' AS another", db_path=index)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        services.sql_readonly("WITH x AS (SELECT 1) DELETE FROM files", db_path=index)
    with pytest.raises(sqlite3.ProgrammingError):
        services.sql_readonly("SELECT 1; DELETE FROM files", db_path=index)
    assert services.sql_readonly("SELECT count(*) AS n FROM files", db_path=index) == [{"n": 6}]


def test_cli_multiple_roots_and_json(
    fake_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pcinspector import cli

    second, third = tmp_path / "Диск 2", tmp_path / "Диск 3"
    for path in (second, third):
        path.mkdir()
    db_path = str(tmp_path / "cli.db")
    monkeypatch.setenv("PCI_DB", str(tmp_path / "fallback.db"))

    assert cli.main(["--db", db_path, "scan", str(fake_tree), str(second), str(third), "--json"]) == 0
    scan = json.loads(capsys.readouterr().out)
    assert scan["state"] == "completed"
    assert len(scan["results"]) == 3
    assert not (tmp_path / "fallback.db").exists()

    for args in (
        ["junk", "--min-size", "1", "--group", "false"],
        ["tree", str(fake_tree)],
        ["tree", "--path", str(fake_tree)],
        ["types", "--by", "ext"],
        ["forgotten_dirs", "--min-size", "1", "--days", "365"],
    ):
        assert cli.main(["--json", *args, "--db", db_path]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["analyzer"] == args[0]
        assert report["findings"]

    assert cli.main(["sql", "SELECT x'ff' AS blob, 1e999 AS large", "--json", "--db", db_path]) == 0
    assert json.loads(capsys.readouterr().out) == [{"blob": "ff", "large": "inf"}]
    assert cli.main(["--db", db_path, "tree", str(fake_tree)]) == 0
    output = capsys.readouterr().out
    assert "Размер" in output and "Итого" in output


def test_module_entrypoint(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(config, "configure_logging", lambda: None)
    monkeypatch.setattr(sys, "argv", ["pcinspector", "analyzers", "--json"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("pcinspector", run_name="__main__")
    assert exit_info.value.code == 0
    metadata = json.loads(capsys.readouterr().out)
    assert len(metadata) == 7
    types = next(info for info in metadata if info["id"] == "types")
    assert next(param for param in types["params"] if param["name"] == "by")["choices"] == ["category", "ext"]
