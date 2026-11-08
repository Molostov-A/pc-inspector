"""Единственная точка записи в файловую систему при уборке и откате."""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import time
from itertools import chain
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..core import safety, settings
from ..core.fmt import DAY
from . import plans, quarantine, recycle_bin, snapshot

logger = logging.getLogger(__name__)
# Файлы моложе суток могут использоваться запущенными программами.
CLEAR_MIN_AGE = DAY


def send2trash(path: str) -> None:
    """Отправить в Корзину только то, что она примет, не удалив объект безвозвратно."""
    from pywintypes import com_error
    from send2trash.win.modern import send2trash as recycle

    recycle_bin.ensure_recyclable(path)
    try:
        recycle(path)
    except com_error as ex:
        raise OSError(f"Корзина Windows отказалась принять объект: {ex}") from ex


def _log_result(
    con: sqlite3.Connection,
    item_id: int,
    status: str,
    error: str | None,
    restorable: bool = False,
) -> None:
    con.execute("UPDATE plan_items SET status=?, error=? WHERE id=?", (status, error, item_id))
    con.execute(
        "UPDATE actions_log SET status=?, error=?, restorable=? WHERE plan_item_id=?",
        (status, error, int(restorable), item_id),
    )
    logger.info("Пункт уборки %s: %s %s", item_id, status, error or "")


def _check_snapshot(item: sqlite3.Row, path: Path) -> None:
    entries = snapshot.live_entries(path)
    first = next(entries)
    if first[1] != item["kind"] or (first[1] == "file" and first[3] != item["expected_mtime"]):
        raise ValueError("Объект изменился после анализа")
    if item["action"] == "delete_empty_dir" and next(entries, None) is not None:
        raise ValueError("Папка больше не пуста")
    if snapshot.fingerprint(chain((first,), entries), str(path)) != item["fingerprint"]:
        raise ValueError("Размер, дата или содержимое изменились после анализа")


def _make_directories(directory: Path) -> None:
    for current in (*reversed(directory.parents), directory):
        try:
            current.lstat()
        except FileNotFoundError:
            current.mkdir()
        safety.local_path_without_links(str(current))
        if not current.is_dir():
            raise ValueError("Компонент пути назначения не является папкой")


def _write_manifest(con: sqlite3.Connection, base: Path, plan_id: int) -> None:
    rows = con.execute(
        "SELECT l.id,l.src,l.dst,l.at,l.size FROM actions_log l "
        "JOIN plan_items p ON p.id=l.plan_item_id "
        "WHERE p.plan_id=? AND l.action IN ('quarantine','clear_contents') AND l.dst IS NOT NULL",
        (plan_id,),
    )
    entries = [dict(row) for row in rows if Path(row["dst"]).is_relative_to(base)]
    temporary = base / f".manifest-{uuid4().hex}.tmp"
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump({"plan_id": plan_id, "entries": entries}, stream, ensure_ascii=False, indent=2)
    try:
        temporary.replace(base / "manifest.json")
    finally:
        temporary.unlink(missing_ok=True)


def _reserve(
    con: sqlite3.Connection,
    plan: sqlite3.Row,
    item: sqlite3.Row,
) -> tuple[Path | None, Path | None, bool]:
    base = destination = None
    error = None
    try:
        if item["action"] in quarantine.STORED_ACTIONS:
            base, destination = quarantine.location(plan, Path(item["path"]), settings.quarantine_path(con))
        elif item["action"] == "move":
            destination = Path(item["target"])
    except (OSError, ValueError) as ex:
        error = str(ex)
    with con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT INTO actions_log(plan_item_id,at,action,src,dst,size,status,restorable)"
            " VALUES (?,?,?,?,?,?,'running',?)",
            (
                item["id"],
                time.time(),
                item["action"],
                item["path"],
                str(destination) if destination else None,
                item["size"],
                int(destination is not None),
            ),
        )
        con.execute("UPDATE plan_items SET status='running' WHERE id=?", (item["id"],))
        if error is not None:
            _log_result(con, item["id"], "failed", error)
    return base, destination, error is None


def _clear_contents(plan: sqlite3.Row, source: Path, destination: Path) -> tuple[int, int, int]:
    """Перенести в карантин дочерние объекты папки, пропустив свежие и занятые."""
    cutoff = min(time.time() - CLEAR_MIN_AGE, plan["created_at"])
    moved = size = skipped = 0
    with os.scandir(source) as entries:
        children = [entry.path for entry in entries]
    for path in children:
        try:
            child = safety.checked_action_path(path)
            items = list(snapshot.live_entries(child))
            if any(kind == "file" and mtime >= cutoff for _, kind, _, mtime in items):
                skipped += 1
                continue
            _make_directories(destination)
            child.rename(destination / child.name)
        except (OSError, ValueError) as ex:
            logger.info("Очистка папки, пропущен %s: %s", path, ex)
            skipped += 1
            continue
        moved += 1
        size += sum(entry[2] for entry in items)
    return moved, size, skipped


def _finish_clear(con: sqlite3.Connection, item: sqlite3.Row, moved: int, size: int, skipped: int) -> None:
    if not moved:
        _log_result(con, item["id"], "skipped", "Все объекты свежие или заняты другими программами")
        return
    con.execute("UPDATE plan_items SET size=? WHERE id=?", (size, item["id"]))
    con.execute("UPDATE actions_log SET size=? WHERE plan_item_id=?", (size, item["id"]))
    note = f"Перенесено {moved}, пропущено свежих или занятых {skipped}" if skipped else None
    _log_result(con, item["id"], "done", note, True)


def _apply(
    con: sqlite3.Connection,
    plan: sqlite3.Row,
    item: sqlite3.Row,
    base: Path | None,
    destination: Path | None,
) -> None:
    with con:
        con.execute("BEGIN IMMEDIATE")
        try:
            source = safety.checked_action_path(item["path"])
            plans.validate_operation(
                source,
                item["kind"],
                item["action"],
                item["target"],
                quarantine_paths=quarantine.protected_paths(con),
            )
            if item["action"] != "clear_contents":
                _check_snapshot(item, source)
            elif not source.is_dir():
                raise ValueError("Объект больше не является папкой")
        except (FileNotFoundError, ValueError) as ex:
            _log_result(con, item["id"], "skipped", str(ex))
            return
        except OSError as ex:
            _log_result(con, item["id"], "failed", f"Не удалось проверить объект: {ex}")
            return
        try:
            if destination is not None:
                _make_directories(destination.parent)
            if base is not None:
                _write_manifest(con, base, plan["id"])
            if item["action"] == "clear_contents":
                _finish_clear(con, item, *_clear_contents(plan, source, destination))
                return
            if destination is None:
                send2trash(str(source))
            else:
                # На Windows rename атомарно отказывает при занятом назначении.
                source.rename(destination)
        except (OSError, ValueError, ImportError) as ex:
            _log_result(con, item["id"], "failed", f"Действие не выполнено: {ex}")
            return
        _log_result(con, item["id"], "done", None, destination is not None)


def execute_plan(con: sqlite3.Connection, plan_id: int, token: str) -> dict[str, Any]:
    with con:
        con.execute("BEGIN IMMEDIATE")
        plan = plans.require_plan(con, plan_id, draft=True)
        if (
            not plan["confirm_hash"]
            or plan["confirm_expires"] <= time.time()
            or not secrets.compare_digest(plan["confirm_hash"], plans.token_hash(token))
        ):
            raise ValueError("Подтверждение отсутствует, недействительно или истекло")
        con.execute(
            "UPDATE plans SET status='executing', confirm_hash=NULL, confirm_expires=NULL WHERE id=?",
            (plan_id,),
        )
        items = list(
            con.execute(
                "SELECT * FROM plan_items WHERE plan_id=? AND selected=1 AND status='pending' ORDER BY id",
                (plan_id,),
            )
        )
    for item in items:
        base, destination, reserved = _reserve(con, plan, item)
        if reserved:
            _apply(con, plan, item, base, destination)
    with con:
        con.execute("BEGIN IMMEDIATE")
        outcome = plans.get_plan(con, plan_id)
        done = [item for item in outcome["items"] if item["status"] == "done"]
        failures = [item for item in outcome["items"] if item["status"] in ("failed", "skipped")]
        state = "partially" if failures else "executed"
        con.execute("UPDATE plans SET status=? WHERE id=?", (state, plan_id))
    return {
        "plan_id": plan_id,
        "status": state,
        "done": len(done),
        "processed_size": sum(item["size"] for item in done),
        "freed_size": 0,
        "skipped": sum(item["status"] == "skipped" for item in failures),
        "failed": sum(item["status"] == "failed" for item in failures),
        "issues": failures,
        "changed_paths": [path for item in done for path in (item["path"], item["target"]) if path],
    }


def history(con: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in con.execute("SELECT * FROM actions_log ORDER BY id DESC LIMIT 500")]


def _restore_contents(con: sqlite3.Connection, record: sqlite3.Row) -> dict[str, Any]:
    source = safety.checked_action_path(record["src"], destination=True)
    if not source.is_dir():
        raise ValueError("Исходная папка отсутствует. Создайте её и повторите откат")
    moved = safety.checked_action_path(record["dst"], managed=True)
    with os.scandir(moved) as entries:
        children = [Path(entry.path) for entry in entries]
    occupied = 0
    for child in children:
        if os.path.lexists(source / child.name):
            occupied += 1
        else:
            child.rename(source / child.name)
    if occupied:
        return {
            "restored": False,
            "message": f"Не возвращено объектов: {occupied}. Такие имена уже заняты, они остались в карантине",
            "changed_paths": [str(source)],
        }
    now = time.time()
    con.execute("UPDATE actions_log SET restored_at=? WHERE id=?", (now, record["id"]))
    logger.info("Откат очистки %s: %s", record["id"], source)
    return {"restored": True, "path": str(source), "restored_at": now, "changed_paths": [str(source)]}


def restore_action(con: sqlite3.Connection, action_id: int) -> dict[str, Any]:
    with con:
        con.execute("BEGIN IMMEDIATE")
        record = con.execute("SELECT * FROM actions_log WHERE id=?", (action_id,)).fetchone()
        if record is None:
            raise KeyError("Запись истории не найдена")
        if record["status"] == "done" and record["action"] in ("recycle", "delete_empty_dir"):
            return {"restored": False, "message": "Откройте Корзину Windows и восстановите элемент вручную"}
        if not record["restorable"] or record["restored_at"] or record["purged_at"]:
            raise ValueError("Элемент недоступен для автоматического восстановления")
        if record["action"] == "clear_contents":
            return _restore_contents(con, record)
        source = safety.checked_action_path(record["src"], destination=True)
        protected = quarantine.protected_paths(con)
        safety.reject_quarantine_overlap(str(source), protected)
        if os.path.lexists(source):
            raise ValueError("Исходный путь уже занят. Существующий объект сохранён")
        moved = safety.checked_action_path(record["dst"], managed=record["action"] == "quarantine")
        if record["action"] == "move":
            safety.reject_quarantine_overlap(str(moved), protected)
        item = con.execute("SELECT * FROM plan_items WHERE id=?", (record["plan_item_id"],)).fetchone()
        _check_snapshot(item, moved)
        moved.rename(source)
        now = time.time()
        con.execute("UPDATE actions_log SET restored_at=? WHERE id=?", (now, action_id))
        logger.info("Откат %s: %s", action_id, source)
        return {
            "restored": True,
            "path": str(source),
            "restored_at": now,
            "changed_paths": [str(source), str(moved)],
        }


def purge_quarantine(con: sqlite3.Connection, older_than_days: int, *, confirmed: bool) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("Подтвердите очистку карантина")
    if not 1 <= older_than_days <= 3650:
        raise ValueError("Укажите возраст от 1 до 3650 дней")
    cutoff = time.time() - older_than_days * DAY
    errors = []
    count = size = 0
    with con:
        con.execute("BEGIN IMMEDIATE")
        for record in quarantine.entries(con, cutoff=cutoff):
            try:
                path = safety.checked_action_path(record["dst"], managed=True)
                item = con.execute(
                    "SELECT * FROM plan_items WHERE id=?", (record["plan_item_id"],)
                ).fetchone()
                if item["action"] != "clear_contents":
                    _check_snapshot(item, path)
                send2trash(str(path))
            except (OSError, ValueError, ImportError) as ex:
                errors.append({"id": record["id"], "reason": str(ex)})
                logger.warning("Очистка карантина %s: %s", record["id"], ex)
                continue
            con.execute("UPDATE actions_log SET purged_at=? WHERE id=?", (time.time(), record["id"]))
            logger.info("Карантин %s перемещён в Корзину", record["id"])
            count += 1
            size += record["size"]
    return {"count": count, "processed_size": size, "freed_size": 0, "errors": errors}
