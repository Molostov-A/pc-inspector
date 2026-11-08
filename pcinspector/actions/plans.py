"""Черновики планов, проверка по индексу и одноразовое подтверждение."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from ..core import safety
from . import quarantine, snapshot

Action = Literal["recycle", "quarantine", "move", "delete_empty_dir", "clear_contents"]
Author = Literal["user", "ai"]
ACTIONS = ("recycle", "quarantine", "move", "delete_empty_dir", "clear_contents")
MAX_ITEMS = 500


@dataclass(frozen=True)
class PlanItemIn:
    path: str
    action: Action = "quarantine"
    reason: str = ""
    target: str | None = None
    safety: str = "review"
    source: str = "manual"


def require_plan(con: sqlite3.Connection, plan_id: int, *, draft: bool = False) -> sqlite3.Row:
    plan = con.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if plan is None:
        raise KeyError("План не найден")
    if draft and plan["status"] != "draft":
        raise ValueError("Изменить или выполнить можно только черновик")
    return plan


def _summary(plan: sqlite3.Row) -> dict[str, Any]:
    fields = ("id", "title", "author", "note", "created_at", "status", "revision")
    return {field: plan[field] for field in fields}


def public_item(item: sqlite3.Row) -> dict[str, Any]:
    result = dict(item)
    del result["fingerprint"]
    return result


def list_plans(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT plans.*, (SELECT count(*) FROM plan_items WHERE plan_id = plans.id) AS item_count "
        "FROM plans ORDER BY id DESC LIMIT 100"
    )
    return [{**_summary(row), "item_count": row["item_count"]} for row in rows]


def get_plan(con: sqlite3.Connection, plan_id: int) -> dict[str, Any]:
    plan = require_plan(con, plan_id)
    items = [
        public_item(row)
        for row in con.execute(
            "SELECT * FROM plan_items WHERE plan_id = ? ORDER BY id LIMIT ?",
            (plan_id, MAX_ITEMS),
        )
    ]
    selected = [item for item in items if item["selected"]]
    return {
        **_summary(plan),
        "items": items,
        "selected_count": len(selected),
        "selected_size": sum(item["size"] for item in selected),
    }


def _text(value: str, maximum: int, *, required: bool = False) -> str:
    value = value.strip()
    if len(value) > maximum or (required and not value):
        raise ValueError(f"Укажите текст длиной от {int(required)} до {maximum} символов")
    return value


def create_plan(con: sqlite3.Connection, title: str, author: Author, note: str) -> dict[str, Any]:
    with con:
        cursor = con.execute(
            "INSERT INTO plans(title, author, note, created_at, storage_key) VALUES (?,?,?,?,?)",
            (_text(title, 160, required=True), author, _text(note, 2000), time.time(), uuid4().hex),
        )
        return get_plan(con, cursor.lastrowid)


def _changed(con: sqlite3.Connection, plan_id: int) -> None:
    con.execute(
        "UPDATE plans SET revision=revision+1, confirm_hash=NULL, confirm_expires=NULL WHERE id=?",
        (plan_id,),
    )


def edit_plan(
    con: sqlite3.Connection,
    plan_id: int,
    title: str | None,
    note: str | None,
    cancel: bool,
) -> dict[str, Any]:
    with con:
        con.execute("BEGIN IMMEDIATE")
        plan = require_plan(con, plan_id, draft=True)
        con.execute(
            "UPDATE plans SET title=?, note=?, status=? WHERE id=?",
            (
                plan["title"] if title is None else _text(title, 160, required=True),
                plan["note"] if note is None else _text(note, 2000),
                "cancelled" if cancel else "draft",
                plan_id,
            ),
        )
        _changed(con, plan_id)
        return get_plan(con, plan_id)


def _indexed(con: sqlite3.Connection, path: str) -> tuple[snapshot.Kind, sqlite3.Row]:
    path_match = "path = ? COLLATE NOCASE" if path.isascii() else "pci_casefold(path) = ?"
    directory = con.execute(
        "SELECT id,path,total_size AS size,total_files,mtime,protected FROM dirs "
        f"WHERE {path_match} ORDER BY depth,id LIMIT 1",
        (path.casefold(),),
    ).fetchone()
    if directory is not None:
        return "dir", directory
    parent, name = os.path.split(path)
    parent_match = "d.path = ? COLLATE NOCASE" if parent.isascii() else "pci_casefold(d.path) = ?"
    file = con.execute(
        "SELECT f.size,f.mtime,d.protected FROM dirs d JOIN files f ON f.dir_id=d.id "
        f"WHERE {parent_match} AND pci_casefold(f.name)=? ORDER BY d.depth,f.id LIMIT 1",
        (parent.casefold(), name.casefold()),
    ).fetchone()
    if file is None:
        raise ValueError("Путь отсутствует в индексе")
    return "file", file


def validate_operation(
    path: Path,
    kind: str,
    action: Action,
    target: str | None,
    *,
    quarantine_paths: tuple[str, ...] = (),
) -> str | None:
    safety.reject_quarantine_overlap(str(path), quarantine_paths)
    if action not in ACTIONS:
        raise ValueError("Неизвестное действие")
    if action == "delete_empty_dir" and kind != "dir":
        raise ValueError("Для этого действия нужна пустая папка")
    if action == "clear_contents" and kind != "dir":
        raise ValueError("Очистить содержимое можно только у папки")
    if action != "move":
        if target is not None:
            raise ValueError("Назначение требуется только для перемещения")
        return None
    if not target:
        raise ValueError("Укажите полный путь назначения")
    destination = safety.checked_action_path(target, destination=True, create_parents=True)
    safety.reject_quarantine_overlap(str(destination), quarantine_paths)
    if safety.paths_overlap(str(path), str(destination)):
        raise ValueError("Источник и назначение пересекаются")
    if path.drive.lower() != destination.drive.lower():
        raise ValueError("Обратимое перемещение выполняется в пределах одного диска")
    if os.path.lexists(destination):
        raise ValueError("Путь назначения уже занят")
    return str(destination)


def _check_overlap(
    path: str,
    target: str | None,
    existing: list[sqlite3.Row],
) -> None:
    if any(
        safety.paths_overlap(candidate, previous)
        for candidate in (path, target)
        if candidate
        for item in existing
        for previous in (item["path"], item["target"])
        if previous
    ):
        raise ValueError("Путь или назначение пересекается с другим пунктом плана")


def add_items(
    con: sqlite3.Connection,
    plan_id: int,
    items: list[PlanItemIn],
    *,
    keep_path: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    if len(items) > MAX_ITEMS:
        raise ValueError(f"За раз можно передать до {MAX_ITEMS} пунктов")
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    with con:
        con.execute("BEGIN IMMEDIATE")
        require_plan(con, plan_id, draft=True)
        existing = list(con.execute("SELECT * FROM plan_items WHERE plan_id = ?", (plan_id,)))
        quarantine_paths = quarantine.protected_paths(con)
        if keep_path is not None:
            keep_path = str(safety.checked_action_path(keep_path))
            kind, _ = _indexed(con, keep_path)
            if kind != "file":
                raise ValueError("Сохраняемая копия должна быть файлом")
            if any(
                safety.paths_overlap(keep_path, path)
                for row in existing
                for path in (row["path"], row["target"])
                if path
            ):
                raise ValueError(
                    "Сохраняемая копия уже затронута этим планом. Выберите другой план или копию"
                )
        for item in items:
            try:
                if len(existing) >= MAX_ITEMS:
                    raise ValueError(f"В плане уже {MAX_ITEMS} пунктов")
                path = safety.checked_action_path(item.path)
                kind, indexed = _indexed(con, str(path))
                if indexed["protected"] or indexed["mtime"] is None:
                    raise ValueError("Защищённый путь или неполные данные в индексе")
                if keep_path is not None and (
                    safety.paths_overlap(keep_path, str(path))
                    or (item.target is not None and safety.paths_overlap(keep_path, item.target))
                ):
                    raise ValueError("Пункт затрагивает копию, выбранную для сохранения")
                target = validate_operation(
                    path, kind, item.action, item.target, quarantine_paths=quarantine_paths
                )
                _check_overlap(str(path), target, existing)
                indexed_path = indexed["path"] if kind == "dir" else str(path)
                entries = (
                    snapshot.directory_index(con, indexed["id"])
                    if kind == "dir"
                    else [
                        (indexed_path, kind, indexed["size"], indexed["mtime"]),
                    ]
                )
                digest = snapshot.fingerprint(entries, indexed_path)
                if item.action == "delete_empty_dir" and not digest.startswith("1:"):
                    raise ValueError("По данным индекса папка не пуста")
                # Метка интерфейса не даёт разрешения на выполнение.
                label = item.safety if item.safety in ("safe", "review") else "review"
                row = con.execute(
                    "INSERT INTO plan_items(plan_id,action,path,target,kind,size,expected_mtime,"
                    "fingerprint,safety,reason,source) VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING *",
                    (
                        plan_id,
                        item.action,
                        str(path),
                        target,
                        kind,
                        indexed["size"],
                        indexed["mtime"],
                        digest,
                        label,
                        _text(item.reason, 1000),
                        _text(item.source, 80, required=True),
                    ),
                ).fetchone()
                existing.append(row)
                accepted.append(public_item(row))
            except (ValueError, OSError) as ex:
                rejected.append({"path": item.path, "reason": str(ex)})
        if accepted:
            _changed(con, plan_id)
    return {"accepted": accepted, "rejected": rejected}


def edit_item(
    con: sqlite3.Connection,
    plan_id: int,
    item_id: int,
    *,
    action: Action | None = None,
    target: str | None = None,
    selected: bool | None = None,
    remove: bool = False,
) -> dict[str, Any]:
    with con:
        con.execute("BEGIN IMMEDIATE")
        require_plan(con, plan_id, draft=True)
        existing = list(con.execute("SELECT * FROM plan_items WHERE plan_id = ?", (plan_id,)))
        item = next((row for row in existing if row["id"] == item_id), None)
        if item is None:
            raise KeyError("Пункт плана не найден")
        if remove:
            con.execute("DELETE FROM plan_items WHERE id = ?", (item_id,))
        else:
            operation = action or item["action"]
            destination = target if target is not None else item["target"] if operation == "move" else None
            if action is not None or target is not None:
                source = safety.checked_action_path(item["path"])
                destination = validate_operation(
                    source,
                    item["kind"],
                    operation,
                    destination,
                    quarantine_paths=quarantine.protected_paths(con),
                )
                _check_overlap(item["path"], destination, [row for row in existing if row["id"] != item_id])
            con.execute(
                "UPDATE plan_items SET action=?, target=?, selected=? WHERE id=?",
                (operation, destination, item["selected"] if selected is None else int(selected), item_id),
            )
        _changed(con, plan_id)
        return get_plan(con, plan_id)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(con: sqlite3.Connection, plan_id: int, revision: int) -> str:
    with con:
        con.execute("BEGIN IMMEDIATE")
        plan = require_plan(con, plan_id, draft=True)
        if plan["revision"] != revision:
            raise ValueError("План изменился. Просмотрите его заново")
        if not get_plan(con, plan_id)["selected_count"]:
            raise ValueError("Выберите хотя бы один пункт плана")
        token = secrets.token_urlsafe(32)
        con.execute(
            "UPDATE plans SET confirm_hash=?, confirm_expires=? WHERE id=?",
            (token_hash(token), time.time() + 120, plan_id),
        )
        return token
