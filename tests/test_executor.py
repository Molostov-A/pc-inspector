"""Обратимые операции на временных файлах и защита подтверждения (F3, N8)."""

from __future__ import annotations

import json
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pcinspector import config, services
from pcinspector.actions import executor, snapshot
from pcinspector.core import db, scanner

_NATIVE_RECYCLE = executor.send2trash


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "QUARANTINE_DIR", tmp_path / "quarantine")
    trash = tmp_path / "trash"
    trash.mkdir()
    monkeypatch.setattr(executor, "send2trash", lambda path: Path(path).rename(trash / Path(path).name))


def _plan(index: Path, *items: services.PlanItemIn) -> dict[str, Any]:
    draft = services.create_plan("Проверка уборки", db_path=index)
    result = services.add_plan_items(draft["id"], list(items), db_path=index)
    assert not result["rejected"], result
    return services.get_plan(draft["id"], db_path=index)


def _execute(index: Path, plan: dict[str, Any]) -> dict[str, Any]:
    token = services.plan_confirmation(plan["id"], plan["revision"], db_path=index)
    return services.execute_plan(plan["id"], token, db_path=index)


def test_quarantine_and_restore_bytes(index: Path, fake_tree: Path, tmp_path: Path) -> None:
    source = fake_tree / "Документы" / "отчёт.txt"
    expected = source.read_bytes()
    plan = _plan(index, services.PlanItemIn(str(source), "quarantine"))
    result = _execute(index, plan)
    assert result["done"] == 1 and result["processed_size"] == len(expected) and result["freed_size"] == 0
    record = services.list_quarantine(db_path=index)["items"][0]
    assert not source.exists()
    destination = Path(record["dst"])
    assert destination.read_bytes() == expected
    manifest = next((tmp_path / "quarantine").glob("*/manifest.json"))
    assert json.loads(manifest.read_text(encoding="utf-8"))["entries"][0]["dst"] == str(destination)
    source.write_bytes(b"occupied")
    with pytest.raises(ValueError, match="уже занят"):
        services.restore_action(record["id"], db_path=index)
    assert source.read_bytes() == b"occupied"
    source.rename(source.with_suffix(".original"))
    assert services.restore_action(record["id"], db_path=index)["restored"]
    assert source.read_bytes() == expected
    assert services.list_quarantine(db_path=index)["size"] == 0
    with pytest.raises(ValueError, match="недоступен"):
        services.restore_action(record["id"], db_path=index)


def test_move_directory_and_restore(index: Path, fake_tree: Path) -> None:
    source = fake_tree / "Документы"
    target = fake_tree / "Новый каталог"
    data = (source / "копия.bin").read_bytes()
    plan = _plan(index, services.PlanItemIn(str(source), "move", target=str(target)))
    result = _execute(index, plan)
    assert result["done"] == 1, result["issues"][0]["error"]
    assert (target / "копия.bin").read_bytes() == data
    record = services.action_history(db_path=index)[0]
    assert services.restore_action(record["id"], db_path=index)["restored"]
    assert (source / "копия.bin").read_bytes() == data


def test_changed_subtree_and_missing_path_are_skipped(index: Path, fake_tree: Path) -> None:
    directory = fake_tree / "Документы"
    missing = fake_tree / "Архив" / "копия.bin"
    plan = _plan(index, services.PlanItemIn(str(directory)), services.PlanItemIn(str(missing)))
    (directory / "отчёт.txt").write_bytes(b"changed")
    missing.rename(missing.with_suffix(".old"))
    result = _execute(index, plan)
    assert result["status"] == "partially" and result["skipped"] == 2
    assert "изменил" in result["issues"][0]["error"]
    assert directory.exists()


def test_recycle_failure_does_not_stop_later_items(
    index: Path,
    fake_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = fake_tree / "Документы" / "отчёт.txt"
    empty = fake_tree / "Пустая"
    original = executor.send2trash

    def recycle(path: str) -> None:
        if path == str(source):
            raise PermissionError("Файл занят")
        original(path)

    monkeypatch.setattr(executor, "send2trash", recycle)
    plan = _plan(
        index,
        services.PlanItemIn(str(source), "recycle"),
        services.PlanItemIn(str(empty), "delete_empty_dir"),
    )
    result = _execute(index, plan)
    assert result["done"] == 1 and result["failed"] == 1
    assert source.exists() and not empty.exists()
    history = services.action_history(db_path=index)
    assert "вручную" in services.restore_action(history[0]["id"], db_path=index)["message"]
    with pytest.raises(ValueError, match="недоступен"):
        services.restore_action(history[1]["id"], db_path=index)
    with pytest.raises(KeyError, match="не найдена"):
        services.restore_action(100, db_path=index)


def test_purge_is_explicit_and_age_limited(index: Path, fake_tree: Path, tmp_path: Path) -> None:
    source = fake_tree / "Документы" / "отчёт.txt"
    plan = _plan(index, services.PlanItemIn(str(source), "quarantine"))
    assert _execute(index, plan)["done"] == 1
    with pytest.raises(ValueError, match="Подтвердите"):
        services.purge_quarantine(db_path=index)
    with pytest.raises(ValueError, match="возраст"):
        services.purge_quarantine(0, confirmed=True, db_path=index)
    assert services.purge_quarantine(confirmed=True, db_path=index)["count"] == 0
    with closing(db.connect(index)) as con, con:
        con.execute("UPDATE actions_log SET at=0")
    assert services.purge_quarantine(confirmed=True, db_path=index)["count"] == 1
    assert (tmp_path / "trash" / source.name).read_bytes() == b"x" * 64
    assert services.list_quarantine(db_path=index)["size"] == 0
    action_id = services.action_history(db_path=index)[0]["id"]
    with pytest.raises(ValueError, match="недоступен"):
        services.restore_action(action_id, db_path=index)


def test_token_is_revision_bound_one_use_and_expires(index: Path, fake_tree: Path) -> None:
    path = fake_tree / "Документы" / "отчёт.txt"
    plan = _plan(index, services.PlanItemIn(str(path)))
    plan_id = plan["id"]
    token = services.plan_confirmation(plan_id, plan["revision"], db_path=index)
    updated = services.edit_plan(plan_id, title="Новый заголовок", db_path=index)
    with pytest.raises(ValueError, match="План изменился"):
        services.plan_confirmation(plan_id, plan["revision"], db_path=index)
    with pytest.raises(ValueError, match="Подтверждение"):
        services.execute_plan(plan_id, token, db_path=index)
    token = services.plan_confirmation(plan_id, updated["revision"], db_path=index)
    with closing(db.connect(index)) as con, con:
        con.execute("UPDATE plans SET confirm_expires=0 WHERE id=?", (plan_id,))
    with pytest.raises(ValueError, match="истекло"):
        services.execute_plan(plan_id, token, db_path=index)
    token = services.plan_confirmation(plan_id, updated["revision"], db_path=index)
    with pytest.raises(ValueError, match="недействительно"):
        services.execute_plan(plan_id, "wrong", db_path=index)
    assert services.execute_plan(plan_id, token, db_path=index)["done"] == 1
    with pytest.raises(ValueError, match="черновик"):
        services.execute_plan(plan_id, token, db_path=index)


def test_modern_recycle_wrapper_only_calls_mock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pywintypes import com_error
    from send2trash.win import modern

    calls: list[str] = []
    path = str(tmp_path / "file")
    monkeypatch.setattr(modern, "send2trash", calls.append)
    monkeypatch.setattr(executor.recycle_bin, "ensure_recyclable", lambda _path: None)
    _NATIVE_RECYCLE(path)
    assert calls == [path]

    def denied(_path: str) -> None:
        raise com_error("Недоступно")

    monkeypatch.setattr(modern, "send2trash", denied)
    with pytest.raises(OSError, match="отказалась"):
        _NATIVE_RECYCLE(path)


def test_snapshot_rejects_unsupported_entry_types(tmp_path: Path) -> None:
    path = tmp_path / "object"
    with pytest.raises(ValueError, match="заглушка"):
        snapshot._entry(path, SimpleNamespace(st_file_attributes=0x1000))
    with pytest.raises(ValueError, match="обычного файла"):
        snapshot._entry(path, SimpleNamespace(st_mode=stat.S_IFIFO))


def test_snapshot_visits_nested_directory(fake_tree: Path) -> None:
    root = fake_tree / "node_modules"
    assert {Path(path).name for path, *_ in snapshot.live_entries(root)} == {
        "node_modules",
        "пакет",
        "index.js",
    }


def test_empty_directory_is_rechecked_at_execution(index: Path, fake_tree: Path) -> None:
    source = fake_tree / "Пустая"
    plan = _plan(index, services.PlanItemIn(str(source), "delete_empty_dir"))
    (source / "new.bin").write_bytes(b"new")
    with closing(db.connect(index)) as con, con:
        # Имитируем файловую систему, где дата папки осталась прежней.
        con.execute("UPDATE plan_items SET expected_mtime=?", (source.stat().st_mtime,))
    assert "не пуста" in _execute(index, plan)["issues"][0]["error"]
    assert (source / "new.bin").exists()


def test_digest_detects_file_edit_with_matching_root_metadata(index: Path, fake_tree: Path) -> None:
    source = fake_tree / "Документы"
    plan = _plan(index, services.PlanItemIn(str(source)))
    (source / "отчёт.txt").write_bytes(b"edited")
    with closing(db.connect(index)) as con, con:
        con.execute("UPDATE plan_items SET expected_mtime=?", (source.stat().st_mtime,))
    assert "содержимое изменились" in _execute(index, plan)["issues"][0]["error"]


def test_quarantine_failure_cases(
    index: Path,
    fake_tree: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = fake_tree / "Документы" / "отчёт.txt"
    for blocked in (tmp_path / "missing" / "quarantine", tmp_path / "occupied"):
        if blocked.name == "occupied":
            blocked.write_bytes(b"unchanged")
        monkeypatch.setattr(config, "QUARANTINE_DIR", blocked)
        plan = _plan(index, services.PlanItemIn(str(source), "quarantine"))
        assert _execute(index, plan)["failed"] == 1
        assert source.exists()
    assert blocked.read_bytes() == b"unchanged"


def test_unreadable_source_is_logged_and_failed(
    index: Path,
    fake_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = fake_tree / "Документы"
    plan = _plan(index, services.PlanItemIn(str(source)))

    def unavailable(_path: Path) -> None:
        raise PermissionError("Нет доступа")

    monkeypatch.setattr(snapshot, "live_entries", unavailable)
    result = _execute(index, plan)
    assert result["failed"] == 1 and "Не удалось проверить" in result["issues"][0]["error"]
    assert source.exists()


def test_purge_keeps_modified_quarantine_item(index: Path, fake_tree: Path) -> None:
    source = fake_tree / "Документы" / "отчёт.txt"
    plan = _plan(index, services.PlanItemIn(str(source), "quarantine"))
    assert _execute(index, plan)["done"] == 1
    record = services.action_history(db_path=index)[0]
    destination = Path(record["dst"])
    destination.write_bytes(b"changed")
    with closing(db.connect(index)) as con, con:
        con.execute("UPDATE actions_log SET at=0")
    result = services.purge_quarantine(confirmed=True, db_path=index)
    assert result["count"] == 0 and result["errors"][0]["id"] == record["id"]
    assert destination.read_bytes() == b"changed"


def test_rescan_preserves_plan_and_original_expectation(index: Path, fake_tree: Path) -> None:
    source = fake_tree / "Документы" / "отчёт.txt"
    plan = _plan(index, services.PlanItemIn(str(source), "quarantine"))
    source.write_bytes(b"new content")
    with closing(db.connect(index)) as con:
        scanner.scan(str(fake_tree), con)
    assert services.get_plan(plan["id"], db_path=index)["items"] == plan["items"]
    assert _execute(index, plan)["skipped"] == 1
    assert source.read_bytes() == b"new content"


def test_one_token_cannot_execute_twice_concurrently(index: Path, fake_tree: Path) -> None:
    source = fake_tree / "Документы" / "отчёт.txt"
    plan = _plan(index, services.PlanItemIn(str(source), "quarantine"))
    token = services.plan_confirmation(plan["id"], plan["revision"], db_path=index)
    with ThreadPoolExecutor(2) as pool:
        outcomes = [pool.submit(services.execute_plan, plan["id"], token, db_path=index) for _ in range(2)]
    assert sum(future.exception() is None for future in outcomes) == 1
    assert sum(isinstance(future.exception(), ValueError) for future in outcomes) == 1
    assert len(services.action_history(db_path=index)) == 1


def test_journal_recovers_move_if_final_record_failed(
    index: Path,
    fake_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = fake_tree / "Документы" / "отчёт.txt"
    data = source.read_bytes()
    plan = _plan(index, services.PlanItemIn(str(source), "quarantine"))

    def lost_commit(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("Не удалось записать итог")

    monkeypatch.setattr(executor, "_log_result", lost_commit)
    with pytest.raises(sqlite3.OperationalError, match="итог"):
        _execute(index, plan)
    record = services.action_history(db_path=index)[0]
    assert record["status"] == "running" and not source.exists()
    assert services.restore_action(record["id"], db_path=index)["restored"]
    assert source.read_bytes() == data
