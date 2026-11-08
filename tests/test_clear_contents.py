"""Очистка содержимого папок и проверка изменений без дат папок (F3.1, F3.3)."""

from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from pcinspector import config, services
from pcinspector.actions import executor
from pcinspector.core import db, scanner

OLD = 946684800


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "QUARANTINE_DIR", tmp_path / "quarantine")
    trash = tmp_path / "trash"
    trash.mkdir()
    monkeypatch.setattr(executor, "send2trash", lambda path: Path(path).rename(trash / Path(path).name))


def _old_cache(root: Path) -> Path:
    cache = root / "Cache"
    (cache / "старая").mkdir(parents=True)
    for path, data in ((cache / "старая" / "a.bin", b"a" * 10), (cache / "b.bin", b"b" * 5)):
        path.write_bytes(data)
        os.utime(path, (OLD, OLD))
    return cache


def _index(root: Path, tmp_path: Path) -> Path:
    path = tmp_path / "cache.db"
    with closing(db.connect(path)) as con:
        scanner.scan(str(root), con)
    return path


def _plan(index: Path, path: Path, action: str = "clear_contents") -> dict[str, Any]:
    draft = services.create_plan("Очистка", db_path=index)
    result = services.add_plan_items(draft["id"], [services.PlanItemIn(str(path), action)], db_path=index)
    assert not result["rejected"], result
    return services.get_plan(draft["id"], db_path=index)


def _execute(index: Path, plan: dict[str, Any]) -> dict[str, Any]:
    token = services.plan_confirmation(plan["id"], plan["revision"], db_path=index)
    return services.execute_plan(plan["id"], token, db_path=index)


def test_first_scan_of_fresh_tree_is_executable(tmp_path: Path) -> None:
    """Регрессия: дата папки в листинге родителя меняется после первого чтения самой папки."""
    root = tmp_path / "свежее"
    (root / "nm" / "a").mkdir(parents=True)
    (root / "nm" / "a" / "x.js").write_bytes(b"1" * 8)
    index = _index(root, tmp_path)
    result = _execute(index, _plan(index, root / "nm", "quarantine"))
    assert result["done"] == 1, result["issues"]


def test_new_file_in_directory_still_blocks_quarantine(tmp_path: Path) -> None:
    root = tmp_path / "дерево"
    (root / "nm").mkdir(parents=True)
    (root / "nm" / "x.js").write_bytes(b"1")
    index = _index(root, tmp_path)
    plan = _plan(index, root / "nm", "quarantine")
    (root / "nm" / "новый.js").write_bytes(b"2")
    assert _execute(index, plan)["skipped"] == 1


def test_clear_contents_keeps_folder_and_restores(tmp_path: Path) -> None:
    cache = _old_cache(tmp_path / "дерево")
    index = _index(cache.parent, tmp_path)
    plan = _plan(index, cache)
    (cache / "новый.tmp").write_bytes(b"n")
    result = _execute(index, plan)
    assert result["done"] == 1 and result["processed_size"] == 15
    assert cache.is_dir() and [path.name for path in cache.iterdir()] == ["новый.tmp"]
    stored = services.list_quarantine(db_path=index)
    record = stored["items"][0]
    assert record["action"] == "clear_contents" and stored["size"] == 15
    assert (
        "пропущено свежих или занятых 1" in services.get_plan(plan["id"], db_path=index)["items"][0]["error"]
    )
    assert services.restore_action(record["id"], db_path=index)["restored"]
    assert (cache / "старая" / "a.bin").read_bytes() == b"a" * 10 and (cache / "b.bin").exists()


def test_restore_keeps_occupied_names(tmp_path: Path) -> None:
    cache = _old_cache(tmp_path / "дерево")
    index = _index(cache.parent, tmp_path)
    _execute(index, _plan(index, cache))
    record = services.list_quarantine(db_path=index)["items"][0]
    (cache / "b.bin").write_bytes(b"new")
    result = services.restore_action(record["id"], db_path=index)
    assert result["restored"] is False and "заняты" in result["message"]
    assert (cache / "b.bin").read_bytes() == b"new" and (cache / "старая" / "a.bin").exists()
    (cache / "b.bin").unlink()
    assert services.restore_action(record["id"], db_path=index)["restored"]
    assert (cache / "b.bin").read_bytes() == b"b" * 5


def test_restore_needs_source_folder(tmp_path: Path) -> None:
    cache = _old_cache(tmp_path / "дерево")
    index = _index(cache.parent, tmp_path)
    _execute(index, _plan(index, cache))
    record = services.list_quarantine(db_path=index)["items"][0]
    cache.rmdir()
    cache.write_bytes(b"file")
    with pytest.raises(ValueError, match="Исходная папка"):
        services.restore_action(record["id"], db_path=index)


def test_skips_when_everything_is_fresh(tmp_path: Path) -> None:
    root = tmp_path / "дерево"
    (root / "Cache").mkdir(parents=True)
    (root / "Cache" / "a.bin").write_bytes(b"a")
    index = _index(root, tmp_path)
    assert _execute(index, _plan(index, root / "Cache"))["skipped"] == 1
    assert (root / "Cache" / "a.bin").exists()
    assert services.list_quarantine(db_path=index)["items"] == []


def test_skips_busy_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _old_cache(tmp_path / "дерево")
    index = _index(cache.parent, tmp_path)
    plan = _plan(index, cache)
    rename = Path.rename

    def busy(self: Path, target: Path) -> Path:
        if self.name == "b.bin":
            raise PermissionError("занят")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", busy)
    assert _execute(index, plan)["processed_size"] == 10
    assert (cache / "b.bin").exists() and not (cache / "старая").exists()


def test_requires_directory_at_execution(tmp_path: Path) -> None:
    root = tmp_path / "дерево"
    (root / "Cache").mkdir(parents=True)
    index = _index(root, tmp_path)
    plan = _plan(index, root / "Cache")
    (root / "Cache").rmdir()
    (root / "Cache").write_bytes(b"file")
    assert _execute(index, plan)["skipped"] == 1


def test_rejects_file(index: Path, fake_tree: Path) -> None:
    draft = services.create_plan("Файл", db_path=index)
    item = services.PlanItemIn(str(fake_tree / "Документы" / "отчёт.txt"), "clear_contents")
    rejected = services.add_plan_items(draft["id"], [item], db_path=index)["rejected"]
    assert "только у папки" in rejected[0]["reason"]


def test_purge_moves_container_to_recycle(tmp_path: Path) -> None:
    cache = _old_cache(tmp_path / "дерево")
    index = _index(cache.parent, tmp_path)
    _execute(index, _plan(index, cache))
    with closing(db.connect(index)) as con, con:
        con.execute("UPDATE actions_log SET at=1")
    result = services.purge_quarantine(1, confirmed=True, db_path=index)
    assert result["count"] == 1 and result["processed_size"] == 15 and not result["errors"]
