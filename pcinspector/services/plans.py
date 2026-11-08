"""Планы уборки, выполнение, откат и карантин; после изменений обновляет индекс."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from typing import Any

from ..actions import executor, plans, quarantine
from ..actions.plans import Action, Author, PlanItemIn
from ..core import db, scanner
from .workspace import DbPath, resolve, state, state_snapshot

logger = logging.getLogger(__name__)
__all__ = ["Action", "Author", "PlanItemIn"]
REFRESH_TIMEOUT_MS = 1000


def _refresh_index(result: dict[str, Any], db_path: DbPath) -> dict[str, Any]:
    """Отразить изменения на диске в индексе. Если идёт скан, индекс обновит пересканирование."""
    paths = result.pop("changed_paths", [])
    if not paths:
        return result
    try:
        with closing(db.connect_index(resolve(db_path))) as con:
            con.execute(f"PRAGMA busy_timeout={REFRESH_TIMEOUT_MS}")
            scanner.refresh_paths(con, paths)
    except (sqlite3.OperationalError, OSError) as ex:
        logger.warning("Индекс не обновлён после уборки: %s", ex)
        result["index_updated"] = False
    else:
        result["index_updated"] = True
    return result


def create_plan(
    title: str,
    author: Author = "user",
    note: str = "",
    *,
    db_path: DbPath = None,
) -> dict[str, Any]:
    with state(db_path) as con:
        return plans.create_plan(con, title, author, note)


def list_plans(*, db_path: DbPath = None) -> list[dict[str, Any]]:
    with state(db_path) as con:
        return plans.list_plans(con)


def get_plan(plan_id: int, *, db_path: DbPath = None) -> dict[str, Any]:
    with state_snapshot(db_path) as con:
        return plans.get_plan(con, plan_id)


def edit_plan(
    plan_id: int,
    title: str | None = None,
    note: str | None = None,
    *,
    cancel: bool = False,
    db_path: DbPath = None,
) -> dict[str, Any]:
    with state(db_path) as con:
        return plans.edit_plan(con, plan_id, title, note, cancel)


def add_plan_items(
    plan_id: int,
    items: list[PlanItemIn],
    *,
    keep_path: str | None = None,
    db_path: DbPath = None,
) -> dict[str, list[dict[str, Any]]]:
    with state(db_path) as con:
        return plans.add_items(con, plan_id, items, keep_path=keep_path)


def edit_plan_item(
    plan_id: int,
    item_id: int,
    *,
    action: Action | None = None,
    target: str | None = None,
    selected: bool | None = None,
    remove: bool = False,
    db_path: DbPath = None,
) -> dict[str, Any]:
    with state(db_path) as con:
        return plans.edit_item(
            con, plan_id, item_id, action=action, target=target, selected=selected, remove=remove
        )


def plan_confirmation(plan_id: int, revision: int, *, db_path: DbPath = None) -> str:
    with state(db_path) as con:
        return plans.issue_token(con, plan_id, revision)


def execute_plan(plan_id: int, confirm_token: str, *, db_path: DbPath = None) -> dict[str, Any]:
    with state(db_path) as con:
        result = executor.execute_plan(con, plan_id, confirm_token)
    return _refresh_index(result, db_path)


def action_history(*, db_path: DbPath = None) -> list[dict[str, Any]]:
    with state(db_path) as con:
        return executor.history(con)


def restore_action(action_id: int, *, db_path: DbPath = None) -> dict[str, Any]:
    with state(db_path) as con:
        result = executor.restore_action(con, action_id)
    return _refresh_index(result, db_path)


def list_quarantine(*, db_path: DbPath = None) -> dict[str, Any]:
    with state_snapshot(db_path) as con:
        return {"items": quarantine.entries(con), "size": quarantine.usage(con)}


def purge_quarantine(
    older_than_days: int = 30,
    *,
    confirmed: bool = False,
    db_path: DbPath = None,
) -> dict[str, Any]:
    with state(db_path) as con:
        return executor.purge_quarantine(con, older_than_days, confirmed=confirmed)
