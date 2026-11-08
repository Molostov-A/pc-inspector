"""Фасад логики приложения: единственная точка входа для CLI, API и MCP."""

from __future__ import annotations

from . import system
from .brief import ai_brief
from .plans import (
    Action,
    Author,
    PlanItemIn,
    action_history,
    add_plan_items,
    create_plan,
    edit_plan,
    edit_plan_item,
    execute_plan,
    get_plan,
    list_plans,
    list_quarantine,
    plan_confirmation,
    purge_quarantine,
    restore_action,
)
from .reports import (
    analyzer_catalog,
    folder_contents,
    get_settings,
    largest_folders,
    list_analyzers,
    list_roots,
    run_analyzer,
    save_settings,
    tree,
)
from .scans import cancel_job, collect_apps, job_status, list_drives, start_hash, start_scan
from .sql import sql_readonly
from .system import open_in_explorer, open_uninstaller
from .treemap import TreemapNode
from .treemap import treemap as treemap

__all__ = [
    "Action",
    "Author",
    "PlanItemIn",
    "TreemapNode",
    "action_history",
    "add_plan_items",
    "ai_brief",
    "analyzer_catalog",
    "cancel_job",
    "collect_apps",
    "create_plan",
    "edit_plan",
    "edit_plan_item",
    "execute_plan",
    "folder_contents",
    "get_plan",
    "get_settings",
    "job_status",
    "largest_folders",
    "list_analyzers",
    "list_drives",
    "list_plans",
    "list_quarantine",
    "list_roots",
    "open_in_explorer",
    "open_uninstaller",
    "plan_confirmation",
    "purge_quarantine",
    "restore_action",
    "run_analyzer",
    "save_settings",
    "sql_readonly",
    "start_hash",
    "start_scan",
    "system",
    "tree",
    "treemap",
]
