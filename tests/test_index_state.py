"""Разделение баз, пересекающиеся корни, обновление индекса и лимит SQL (F1, F3, §9.8)."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pcinspector import config, services
from pcinspector.actions import executor
from pcinspector.api.app import create_app
from pcinspector.core import db, migrations, scanner
from pcinspector.services import sql

OLD = 946684800


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "QUARANTINE_DIR", tmp_path / "quarantine")
    trash = tmp_path / "trash"
    trash.mkdir()
    monkeypatch.setattr(executor, "send2trash", lambda path: Path(path).rename(trash / Path(path).name))


def _tree(root: Path) -> Path:
    for relative, data in (("a/b/one.bin", b"1" * 100), ("a/two.bin", b"2" * 50), ("c/three.bin", b"3" * 25)):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, (OLD, OLD))
    return root


def _scan(path: Path, index: Path) -> scanner.ScanResult:
    with closing(db.connect_index(index)) as con:
        return scanner.scan(str(path), con)


def _plan_and_run(index: Path, item: services.PlanItemIn) -> dict[str, Any]:
    plan = services.create_plan("План", db_path=index)
    assert not services.add_plan_items(plan["id"], [item], db_path=index)["rejected"]
    revision = services.get_plan(plan["id"], db_path=index)["revision"]
    token = services.plan_confirmation(plan["id"], revision, db_path=index)
    return services.execute_plan(plan["id"], token, db_path=index)


def _root_total(index: Path) -> tuple[int, int, int]:
    rows = services.sql_readonly("SELECT total_size, file_count, dir_count FROM roots", db_path=index)
    assert len(rows) == 1
    return rows[0]["total_size"], rows[0]["file_count"], rows[0]["dir_count"]


def _dir_total(index: Path, path: Path) -> int | None:
    rows = services.sql_readonly(
        f"SELECT total_size FROM dirs WHERE path = '{path}'",
        db_path=index,
    )
    return rows[0]["total_size"] if rows else None


def test_nested_scan_updates_parent_root_without_double_count(tmp_path: Path) -> None:
    root = _tree(tmp_path / "дерево")
    index = tmp_path / "i.db"
    _scan(root, index)
    (root / "a" / "b" / "new.bin").write_bytes(b"n" * 10)
    result = _scan(root / "a", index)
    assert result["size"] == 160 and result["files"] == 3
    assert _root_total(index) == (185, 4, 4)
    assert services.run_analyzer("types", db_path=index).total_size == 185


def test_scan_of_parent_replaces_nested_root(tmp_path: Path) -> None:
    root = _tree(tmp_path / "дерево")
    index = tmp_path / "i.db"
    _scan(root / "a", index)
    _scan(root, index)
    assert _root_total(index) == (175, 3, 4)


def test_excluded_nested_root_is_rejected(tmp_path: Path) -> None:
    root = _tree(tmp_path / "дерево")
    index = tmp_path / "i.db"
    _scan(root, index)
    current = services.get_settings(db_path=index)
    services.save_settings({**current, "exclude_paths": ["a"]}, db_path=index)
    with pytest.raises(ValueError, match="исключениях"):
        _scan(root / "a", index)


def test_execute_and_restore_update_index(tmp_path: Path) -> None:
    root = _tree(tmp_path / "дерево")
    index = tmp_path / "i.db"
    _scan(root, index)
    result = _plan_and_run(index, services.PlanItemIn(str(root / "a" / "b"), "quarantine"))
    assert result["index_updated"] is True and "changed_paths" not in result
    assert _root_total(index) == (75, 2, 3)
    assert _dir_total(index, root / "a") == 50
    record = services.list_quarantine(db_path=index)["items"][0]
    assert services.restore_action(record["id"], db_path=index)["index_updated"] is True
    assert _root_total(index) == (175, 3, 4)
    assert _dir_total(index, root / "a" / "b") == 100


def test_file_removal_updates_parent(tmp_path: Path) -> None:
    root = _tree(tmp_path / "дерево")
    index = tmp_path / "i.db"
    _scan(root, index)
    _plan_and_run(index, services.PlanItemIn(str(root / "a" / "two.bin"), "quarantine"))
    assert _root_total(index) == (125, 2, 4)
    rows = services.sql_readonly(
        f"SELECT own_size,own_files FROM dirs WHERE path='{root / 'a'}'", db_path=index
    )
    assert rows == [{"own_size": 0, "own_files": 0}]


def test_clear_and_move_update_index(tmp_path: Path) -> None:
    root = _tree(tmp_path / "дерево")
    index = tmp_path / "i.db"
    _scan(root, index)
    _plan_and_run(index, services.PlanItemIn(str(root / "c"), "clear_contents"))
    assert _root_total(index) == (150, 2, 4)
    target = root / "новая" / "глубже" / "b"
    _plan_and_run(index, services.PlanItemIn(str(root / "a" / "b"), "move", target=str(target)))
    assert _root_total(index) == (150, 2, 6)
    assert _dir_total(index, root / "новая") == 100


def test_refresh_outside_any_root_is_ignored(tmp_path: Path) -> None:
    root = _tree(tmp_path / "дерево")
    outside = _tree(tmp_path / "другое")
    index = tmp_path / "i.db"
    _scan(root, index)
    before = _root_total(index)
    with closing(db.connect_index(index)) as con:
        scanner.refresh_paths(con, [str(outside / "a"), str(outside / "a" / "two.bin"), str(root / "нет")])
    assert _root_total(index) == before


def test_refresh_new_and_changed_file(tmp_path: Path) -> None:
    root = _tree(tmp_path / "дерево")
    index = tmp_path / "i.db"
    _scan(root, index)
    (root / "c" / "new.bin").write_bytes(b"x" * 7)
    (root / "c" / "three.bin").write_bytes(b"x" * 5)
    with closing(db.connect_index(index)) as con:
        scanner.refresh_paths(con, [str(root / "c" / "new.bin"), str(root / "c" / "three.bin")])
    assert _root_total(index) == (162, 4, 4)
    with closing(db.connect_index(index)) as con:
        scanner.refresh_paths(con, [str(root)])
    assert _root_total(index) == (162, 4, 4)


def test_plans_work_while_index_is_locked(tmp_path: Path) -> None:
    root = _tree(tmp_path / "дерево")
    index = tmp_path / "i.db"
    _scan(root, index)
    with closing(db.connect_index(index)) as scan:
        scan.execute("BEGIN IMMEDIATE")
        scan.execute("UPDATE roots SET error_count=0")
        plan = services.create_plan("Во время скана", db_path=index)
        accepted = services.add_plan_items(
            plan["id"], [services.PlanItemIn(str(root / "a" / "two.bin"))], db_path=index
        )["accepted"]
        assert len(accepted) == 1
        current = services.get_settings(db_path=index)
        assert services.save_settings({**current, "stale_days": 30}, db_path=index)["stale_days"] == 30
        scan.rollback()


def test_execute_reports_index_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _tree(tmp_path / "дерево")
    index = tmp_path / "i.db"
    _scan(root, index)
    monkeypatch.setattr(services.plans, "REFRESH_TIMEOUT_MS", 1)
    with closing(db.connect_index(index)) as scan:
        scan.execute("BEGIN IMMEDIATE")
        scan.execute("UPDATE roots SET error_count=0")
        result = _plan_and_run(index, services.PlanItemIn(str(root / "a" / "two.bin"), "quarantine"))
        scan.rollback()
    assert result["done"] == 1 and result["index_updated"] is False


def test_small_rescan_keeps_indexes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _tree(tmp_path / "дерево")
    index = tmp_path / "i.db"
    calls: list[int] = []
    monkeypatch.setattr(db, "drop_indexes", lambda _con: calls.append(1))
    _scan(root, index)
    _scan(root, index)
    assert not calls


def test_large_scan_drops_indexes_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _tree(tmp_path / "дерево")
    index = tmp_path / "i.db"
    calls: list[int] = []
    original = db.drop_indexes

    def tracked(con: sqlite3.Connection) -> None:
        calls.append(1)
        original(con)

    monkeypatch.setattr(db, "drop_indexes", tracked)
    monkeypatch.setattr(scanner, "BULK_MIN", 1)
    _scan(root, index)
    assert calls == [1]
    assert services.run_analyzer("top_files", db_path=index).findings


def test_split_moves_state_and_keeps_data(tmp_path: Path) -> None:
    index = tmp_path / "old.db"
    con = sqlite3.connect(index)
    con.row_factory = sqlite3.Row
    original = migrations.SCHEMA_VERSION
    migrations.migrate(con)
    assert original == migrations.SCHEMA_VERSION
    con.execute("INSERT INTO settings(key,value) VALUES ('stale_days','77')")
    con.execute(
        "INSERT INTO plans(title,author,note,created_at,storage_key) VALUES ('Старый','user','',1.0,'k')"
    )
    con.commit()
    con.close()
    assert services.get_settings(db_path=index)["stale_days"] == 77
    assert services.list_plans(db_path=index)[0]["title"] == "Старый"
    assert db.state_path(index).exists()
    with closing(sqlite3.connect(index)) as raw:
        names = {row[0] for row in raw.execute("SELECT name FROM sqlite_schema")}
    assert not names & set(migrations.STATE_TABLES)
    assert services.get_settings(db_path=index)["stale_days"] == 77


def test_split_rerun_after_interrupted_drop(tmp_path: Path) -> None:
    index = tmp_path / "old.db"
    with closing(db.connect_state(index)) as con, con:
        con.execute("INSERT INTO settings(key,value) VALUES ('stale_days','11')")
    with closing(sqlite3.connect(index)) as raw, raw:
        raw.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        raw.execute("INSERT INTO settings VALUES ('stale_days','12')")
    assert services.get_settings(db_path=index)["stale_days"] == 12


def test_runaway_sql_is_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _tree(tmp_path / "дерево")
    index = tmp_path / "i.db"
    _scan(root, index)
    monkeypatch.setattr(sql, "TIME_LIMIT", 0.05)
    query = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c"
    with pytest.raises(ValueError, match="остановлен"):
        services.sql_readonly(query, db_path=index)


def test_sql_cannot_read_confirmation_hash(tmp_path: Path) -> None:
    root = _tree(tmp_path / "дерево")
    index = tmp_path / "i.db"
    _scan(root, index)
    plan = services.create_plan("Секрет", db_path=index)
    services.add_plan_items(plan["id"], [services.PlanItemIn(str(root / "c"))], db_path=index)
    fresh = services.get_plan(plan["id"], db_path=index)
    services.plan_confirmation(plan["id"], fresh["revision"], db_path=index)
    rows = services.sql_readonly("SELECT title, confirm_hash FROM plans", db_path=index)
    assert rows == [{"title": "Секрет", "confirm_hash": None}]
    with pytest.raises(sqlite3.Error):
        services.sql_readonly("SELECT * FROM (SELECT 1) WHERE load_extension('x')", db_path=index)


def test_api_reports_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _tree(tmp_path / "дерево")
    index = tmp_path / "i.db"
    _scan(root, index)

    def busy(**_kwargs: object) -> None:
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error

    monkeypatch.setattr(services, "list_plans", busy)
    client = TestClient(
        create_app(db_path=index), base_url="http://127.0.0.1:8765", raise_server_exceptions=False
    )
    response = client.get("/api/plans")
    assert response.status_code == 503 and response.json()["error"]["code"] == "busy"


def test_stale_days_default_comes_from_settings(tmp_path: Path) -> None:
    root = _tree(tmp_path / "дерево")
    index = tmp_path / "i.db"
    _scan(root, index)
    current = services.get_settings(db_path=index)
    services.save_settings({**current, "stale_days": 42}, db_path=index)
    assert services.run_analyzer("forgotten_dirs", db_path=index).params["days"] == 42
    assert services.run_analyzer("forgotten_dirs", days=5, db_path=index).params["days"] == 5
