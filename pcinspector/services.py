"""Общий фасад сканирования, анализа и чтения индекса для интерфейсов."""

from __future__ import annotations

import math
import os
import re
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any, Literal, NotRequired, TypedDict

from . import config
from .actions import executor, plans, quarantine, uninstaller
from .actions.plans import Action, Author, PlanItemIn
from .analyzers import REGISTRY, get
from .analyzers.base import index_path, path_comparison
from .analyzers.overview import directory_finding, find_directory
from .collectors import hashing
from .core import db, jobs, safety, scanner, settings
from .core.fmt import DAY, human_size, parse_size
from .core.models import Finding, Report


def _db_path(db_path: Path | str | None) -> Path:
    return Path(db_path if db_path is not None else os.environ.get("PCI_DB", config.DB_PATH)).absolute()


def start_scan(paths: list[str], *, db_path: Path | str | None = None) -> str:
    roots = {}
    for path in paths or get_settings(db_path=db_path)["scan_roots"] or scanner.local_ntfs_roots():
        root = scanner.normalize_root(path)
        roots.setdefault(root.casefold(), root)
    if not roots:
        raise ValueError("Нет доступных локальных дисков NTFS. Укажите папку сканирования.")
    return jobs.start_scan(list(roots.values()), _db_path(db_path))


def job_status(job_id: str) -> jobs.JobStatus:
    return jobs.job_status(job_id)


def start_hash(
    root: str = "",
    min_size: str | int = hashing.DEFAULT_MIN_SIZE,
    *,
    db_path: Path | str | None = None,
) -> str:
    minimum = parse_size(min_size)
    if not 0 <= minimum <= 2**63 - 1:
        raise ValueError("Укажите допустимый неотрицательный минимальный размер")
    scope = scanner.normalize_root(root) if root else ""
    path = _db_path(db_path)

    def work(notify: Callable[[jobs.HashProgress], None], cancel: Event) -> jobs.HashResult:
        with closing(db.connect(path)) as con:
            return hashing.collect(con, scope, minimum, notify, cancel)

    return jobs.start_hash(work)


def cancel_job(job_id: str) -> bool:
    return jobs.cancel_job(job_id)


def collect_apps(*, db_path: Path | str | None = None) -> str:
    from .collectors import usage

    path = _db_path(db_path)

    def work(cancel: Event) -> jobs.AppsResult:
        with closing(db.connect(path)) as con:
            count, notes = usage.collect(con, cancel)
            return {"count": count, "notes": notes}

    return jobs.start_apps(work)


def open_uninstaller(app_id: int, *, confirmed: bool, db_path: Path | str | None = None) -> None:
    if not confirmed:
        raise ValueError("Подтвердите запуск деинсталлятора")
    from .collectors import apps

    with closing(_read_index(db_path)) as con:
        if con.execute("SELECT 1 FROM sqlite_schema WHERE name='apps'").fetchone() is None:
            raise KeyError("Сведения о программе отсутствуют")
        row = con.execute(
            "SELECT source,reg_key,name,uninstall_cmd,quiet_uninstall_cmd FROM apps WHERE id=? AND is_system_component=0",
            (app_id,),
        ).fetchone()
    if row is None:
        raise KeyError("Сведения о программе отсутствуют")
    if not row["uninstall_cmd"] or row["uninstall_cmd"] == row["quiet_uninstall_cmd"]:
        raise ValueError("Интерактивная команда деинсталляции отсутствует")
    try:
        current = apps.current_app(row["source"], row["reg_key"])
    except OSError as ex:
        raise ValueError("Не удалось проверить запись программы в реестре") from ex
    if (
        current is None
        or current.name != row["name"]
        or current.uninstall_cmd != row["uninstall_cmd"]
        or current.uninstall_cmd == current.quiet_uninstall_cmd
    ):
        raise ValueError("Запись программы изменилась. Обновите список программ")
    uninstaller.launch(current.uninstall_cmd)


def create_plan(
    title: str,
    author: Author = "user",
    note: str = "",
    *,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    with closing(db.connect(_db_path(db_path))) as con:
        return plans.create_plan(con, title, author, note)


def list_plans(*, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    with closing(db.connect(_db_path(db_path))) as con:
        return plans.list_plans(con)


def get_plan(plan_id: int, *, db_path: Path | str | None = None) -> dict[str, Any]:
    with closing(db.connect(_db_path(db_path))) as con, con:
        con.execute("BEGIN")
        return plans.get_plan(con, plan_id)


def edit_plan(
    plan_id: int,
    title: str | None = None,
    note: str | None = None,
    *,
    cancel: bool = False,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    with closing(db.connect(_db_path(db_path))) as con:
        return plans.edit_plan(con, plan_id, title, note, cancel)


def add_plan_items(
    plan_id: int,
    items: list[PlanItemIn],
    *,
    keep_path: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    with closing(db.connect(_db_path(db_path))) as con:
        return plans.add_items(con, plan_id, items, keep_path=keep_path)


def edit_plan_item(
    plan_id: int,
    item_id: int,
    *,
    action: Action | None = None,
    target: str | None = None,
    selected: bool | None = None,
    remove: bool = False,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    with closing(db.connect(_db_path(db_path))) as con:
        return plans.edit_item(
            con, plan_id, item_id, action=action, target=target, selected=selected, remove=remove
        )


def plan_confirmation(plan_id: int, revision: int, *, db_path: Path | str | None = None) -> str:
    with closing(db.connect(_db_path(db_path))) as con:
        return plans.issue_token(con, plan_id, revision)


def execute_plan(plan_id: int, confirm_token: str, *, db_path: Path | str | None = None) -> dict[str, Any]:
    with closing(db.connect(_db_path(db_path))) as con:
        return executor.execute_plan(con, plan_id, confirm_token)


def action_history(*, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    with closing(db.connect(_db_path(db_path))) as con:
        return executor.history(con)


def restore_action(action_id: int, *, db_path: Path | str | None = None) -> dict[str, Any]:
    with closing(db.connect(_db_path(db_path))) as con:
        return executor.restore_action(con, action_id)


def list_quarantine(*, db_path: Path | str | None = None) -> dict[str, Any]:
    with closing(db.connect(_db_path(db_path))) as con, con:
        con.execute("BEGIN")
        return {"items": quarantine.entries(con), "size": quarantine.usage(con)}


def purge_quarantine(
    older_than_days: int = 30,
    *,
    confirmed: bool = False,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    with closing(db.connect(_db_path(db_path))) as con:
        return executor.purge_quarantine(con, older_than_days, confirmed=confirmed)


def list_analyzers() -> list[dict[str, Any]]:
    return [
        {
            "id": analyzer.id,
            "title": analyzer.title,
            "description": analyzer.description,
            "params": [asdict(param) for param in analyzer.params],
        }
        for analyzer in REGISTRY.values()
    ]


def analyzer_catalog() -> list[dict[str, Any]]:
    """Добавить JSON Schema к метаданным каждого анализатора."""
    types = {"int": "integer", "str": "string", "bool": "boolean", "choice": "string"}
    entries = list_analyzers()
    for entry in entries:
        properties = {}
        for param in entry["params"]:
            kind = param["type"]
            schema = {"description": param["help"], "default": param["default"]}
            if kind == "size":
                schema["anyOf"] = [{"type": "integer", "minimum": 0}, {"type": "string"}]
            else:
                schema["type"] = types[kind]
            if kind == "choice":
                schema["enum"] = param["choices"]
            properties[param["name"]] = schema
        entry["schema"] = {"type": "object", "properties": properties, "additionalProperties": False}
    return entries


def _read_index(db_path: Path | str | None) -> sqlite3.Connection:
    path = _db_path(db_path)
    try:
        return db.connect(path, readonly=True)
    except sqlite3.OperationalError:
        if path.exists():
            raise
        return db.empty_index()


def run_analyzer(analyzer_id: str, *, db_path: Path | str | None = None, **params: Any) -> Report:
    analyzer = get(analyzer_id)
    with closing(_read_index(db_path)) as con, con:
        con.execute("BEGIN")
        if analyzer_id in {"stale_files", "forgotten_dirs"} and params.get("days") is None:
            params["days"] = settings.load(con)["stale_days"]
        return analyzer(con, **params)


def get_settings(*, db_path: Path | str | None = None) -> settings.Settings:
    with closing(_read_index(db_path)) as con:
        return settings.load(con)


def save_settings(value: settings.Settings, *, db_path: Path | str | None = None) -> settings.Settings:
    with closing(db.connect(_db_path(db_path))) as con:
        return settings.save(con, value)


def list_roots(*, db_path: Path | str | None = None) -> Report:
    return run_analyzer("roots", db_path=db_path)


def tree(path: str, limit: int = 100, *, db_path: Path | str | None = None) -> Report:
    return run_analyzer("tree", path=path, limit=limit, db_path=db_path)


def _folder_files(con: sqlite3.Connection, directory_id: int, limit: int) -> Iterable[sqlite3.Row]:
    return con.execute(
        "SELECT name, path, size, mtime, protected FROM files_v WHERE dir_id = ? "
        "ORDER BY size DESC, name LIMIT ?",
        (directory_id, limit),
    )


def folder_contents(path: str, limit: int, *, db_path: Path | str | None = None) -> Report:
    """Дочерние папки и крупнейшие файлы непосредственно в выбранной папке."""
    with closing(_read_index(db_path)) as con, con:
        con.execute("BEGIN")
        report = get("tree")(con, path=path, limit=limit)
        parent = find_directory(con, path) if path else None
        if parent is None:
            return report
        report.findings = [item for item in report.findings if item.kind == "dir"]
        for row in _folder_files(con, parent["id"], limit):
            report.findings.append(
                Finding(
                    "file",
                    row["name"],
                    row["size"],
                    row["path"],
                    safety="keep" if row["protected"] else "info",
                    meta={"files": 1, "newest_mtime": row["mtime"]},
                )
            )
        if parent["own_files"] > limit:
            report.notes.append(f"Показаны {limit} крупнейших файлов из {parent['own_files']}.")
        return report


def largest_folders(*, db_path: Path | str | None = None) -> Report:
    """Пять крупнейших дочерних папок просканированных корней."""
    with closing(_read_index(db_path)) as con:
        rows = con.execute(
            "SELECT * FROM dirs WHERE depth = 1 AND total_size > 0 ORDER BY total_size DESC, path LIMIT 5"
        )
        return Report("tree", "Крупные папки", findings=[directory_finding(row) for row in rows])


def open_in_explorer(path: str, *, db_path: Path | str | None = None) -> None:
    """Открыть папку или выделить файл в Проводнике без запуска файла."""
    with closing(_read_index(db_path)) as con:
        row = find_directory(con, path)
        directory = row is not None
        if row is None:
            column, normalized = path_comparison("path", index_path(path))
            row = con.execute(
                f"SELECT path FROM files_v WHERE {column} = ? COLLATE NOCASE LIMIT 1",
                (normalized,),
            ).fetchone()
    if row is None:
        raise FileNotFoundError(path)
    indexed_path = safety.local_path_without_links(row["path"])
    if directory and not Path(indexed_path).is_dir():
        raise FileNotFoundError(path)
    options = [] if directory else ["/select,"]
    subprocess.Popen([str(config.EXPLORER_EXE), *options, indexed_path])


def list_drives(*, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    indexed = {item.path.casefold() for item in list_roots(db_path=db_path).findings if item.path}
    return [{**drive, "scanned": drive["path"].casefold() in indexed} for drive in scanner.local_drives()]


class TreemapNode(TypedDict):
    name: str
    value: int
    path: str | None
    children: list[TreemapNode]
    kind: NotRequired[Literal["file"]]


def _treemap_children(
    con: sqlite3.Connection,
    parent: sqlite3.Row,
    depth: int,
    include_files: bool,
) -> list[TreemapNode]:
    rows = con.execute(
        "SELECT * FROM dirs WHERE parent_id = ? AND total_size > 0 ORDER BY total_size DESC",
        (parent["id"],),
    )
    nodes = _treemap_nodes(con, rows, parent["total_size"], depth, include_files)
    own = parent["own_size"]
    if include_files and own:
        for row in _folder_files(con, parent["id"], 30):
            if row["size"] <= 0 or row["size"] * 200 < parent["total_size"]:
                break
            own -= row["size"]
            nodes.append(
                TreemapNode(name=row["name"], value=row["size"], path=row["path"], children=[], kind="file")
            )
    if own:
        if own * 200 >= parent["total_size"]:
            nodes.append(
                TreemapNode(
                    name="Остальные файлы" if include_files else "Файлы в папке",
                    value=own,
                    path=None,
                    children=[],
                )
            )
        else:
            other = next((node for node in nodes if node["name"] == "Прочее" and node["path"] is None), None)
            if other is None:
                nodes.append(TreemapNode(name="Прочее", value=own, path=None, children=[]))
            else:
                other["value"] += own
    return nodes


def _treemap_nodes(
    con: sqlite3.Connection,
    rows: Iterable[sqlite3.Row],
    total: int,
    depth: int,
    include_files: bool,
) -> list[TreemapNode]:
    nodes: list[TreemapNode] = []
    other = 0
    for row in rows:
        size = row["total_size"]
        if size * 200 < total:
            other += size
        else:
            nodes.append(
                TreemapNode(
                    name=row["name"],
                    value=size,
                    path=row["path"],
                    children=_treemap_children(con, row, depth - 1, include_files) if depth > 1 else [],
                )
            )
    if other:
        nodes.append(TreemapNode(name="Прочее", value=other, path=None, children=[]))
    return nodes


def treemap(
    path: str,
    depth: int = 2,
    *,
    include_files: bool = False,
    db_path: Path | str | None = None,
) -> list[TreemapNode]:
    if not 1 <= depth <= 2:
        raise ValueError("Глубина карты должна быть 1 или 2")
    with closing(_read_index(db_path)) as con, con:
        con.execute("BEGIN")
        if path:
            parent = find_directory(con, path)
            return _treemap_children(con, parent, depth, include_files) if parent is not None else []
        roots = con.execute(
            "SELECT * FROM dirs WHERE parent_id IS NULL AND total_size > 0 ORDER BY total_size DESC"
        )
        total = con.execute(
            "SELECT COALESCE(SUM(total_size), 0) FROM dirs WHERE parent_id IS NULL"
        ).fetchone()[0]
        return _treemap_nodes(con, roots, total, depth, include_files)


def _sql_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _authorize_sql(
    action: int,
    _first: str | None,
    _second: str | None,
    _database: str | None,
    _source: str | None,
) -> int:
    allowed = (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE)
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def sql_readonly(query: str, limit: int = 500, *, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    keyword = re.match(r"[A-Za-z]+", query.lstrip())
    if keyword is None or keyword[0].upper() not in {"SELECT", "WITH", "EXPLAIN"}:
        raise ValueError("Разрешены запросы SELECT, WITH и EXPLAIN")
    if not 1 <= limit <= 10_000:
        raise ValueError("Лимит SQL должен быть от 1 до 10 000 строк")
    with closing(db.connect(_db_path(db_path), readonly=True)) as con:
        con.set_authorizer(_authorize_sql)
        return [
            {name: _sql_value(value) for name, value in dict(row).items()}
            for row in con.execute(query).fetchmany(limit)
        ]


def _markdown_cell(value: str) -> str:
    return (
        " ".join(value.splitlines())
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "&#124;")
        .replace(chr(96), "&#96;")
    )


def ai_brief(max_items: int = 15, *, db_path: Path | str | None = None) -> str:
    """Сформировать краткую Markdown-сводку из одного снимка SQLite."""
    if not 1 <= max_items <= 200:
        raise ValueError("Число строк сводки должно быть от 1 до 200")
    limit = min(max_items, 10)
    with closing(_read_index(db_path)) as con, con:
        con.execute("BEGIN")
        roots = get("roots")(con)
        indexed = [item for item in roots.findings if item.kind == "stat"]
        lines = ["# PC Inspector — сводка по индексу", ""]
        if not indexed:
            return "\n".join([*lines, "Индекс пуст. Запустите сканирование нужной папки или диска.", ""])
        lines.extend(
            [
                "## Просканированные диски и папки",
                "",
                "| Корень | В индексе | Свободно | Ёмкость | Последний скан |",
                "| --- | ---: | ---: | ---: | --- |",
            ]
        )
        stale = []
        now = time.time()
        for item in indexed:
            timestamp = item.meta.get("scanned_at")
            date = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M") if timestamp else "—"
            if not timestamp or now - timestamp > 7 * DAY:
                stale.append(_markdown_cell(item.title))
            lines.append(
                f"| {_markdown_cell(item.title)} | {human_size(item.size)} | "
                f"{human_size(item.meta.get('disk_free'))} | "
                f"{human_size(item.meta.get('disk_total'))} | {date} |"
            )
        if stale:
            lines.extend(["", "Данные старше 7 дней или дата неизвестна — " + ", ".join(stale) + "."])
        junk = get("junk")(con, limit=min(5, max_items))
        lines.extend(
            [
                "",
                "## Что можно освободить",
                "",
                f"- Можно освободить (safe) — {human_size(junk.summary['safe_size'])}.",
                f"- Требуется проверка (review) — {human_size(junk.summary['review_size'])}.",
            ]
        )
        lines.extend(
            f"- {_markdown_cell(item.title)} — {human_size(item.size)} ({item.safety})."
            for item in junk.findings
        )
        for depth in (1, 2):
            lines.extend(["", f"## Крупные папки уровня {depth}", ""])
            folders = con.execute(
                "SELECT path, total_size FROM dirs WHERE depth = ? AND total_size > 0 "
                "ORDER BY total_size DESC, path LIMIT ?",
                (depth, limit),
            ).fetchall()
            lines.extend(
                f"- {_markdown_cell(row['path'])} — {human_size(row['total_size'])}." for row in folders
            )
            if not folders:
                lines.append("Подходящих папок нет.")
        forgotten = get("forgotten_dirs")(con, limit=limit, days=settings.load(con)["stale_days"])
        lines.extend(["", "## Забытые папки", ""])
        lines.extend(
            f"- {_markdown_cell(item.path or item.title)} — {human_size(item.size)}. {item.reason}"
            for item in forgotten.findings
        )
        if not forgotten.findings:
            lines.append("Подходящих папок нет.")
        lines.extend(["", "## Дополнительные данные", ""])
        duplicates = get("duplicates")(con, limit=min(5, max_items))
        if duplicates.findings:
            lines.append("### Дубликаты")
            lines.extend(
                f"- {_markdown_cell(item.title)} — {human_size(item.size)} потенциально, {item.meta['count']} копий."
                for item in duplicates.findings
            )
        elif not duplicates.notes:
            lines.append("Групп дубликатов в выбранном размере не найдено.")
        lines.extend(duplicates.notes)
        applications = get("unused_apps")(con, limit=limit)
        lines.extend(["", "### Неиспользуемые программы"])
        lines.extend(
            f"- {_markdown_cell(item.title)} — {human_size(item.size)}. {item.reason}."
            for item in applications.findings
        )
        lines.extend(applications.notes)
        if not applications.findings and "collected_at" in applications.summary:
            lines.append("В выбранном диапазоне подходящих программ нет.")
        growth = get("growth")(con, limit=limit)
        lines.extend(["", "### Рост папок"])
        for item in growth.findings:
            sign = "+" if item.meta["delta"] > 0 else "−"
            lines.append(f"- {_markdown_cell(item.title)} — {sign}{human_size(item.size)}.")
        lines.extend(growth.notes)

    text = "\n".join(lines) + "\n"
    if len(text) > 12_000:
        note = "\n\nСводка сокращена до 12 000 символов.\n"
        return text[: 12_000 - len(note)].rsplit("\n", 1)[0] + note
    return text
