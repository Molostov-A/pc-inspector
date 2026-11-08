"""Запуск внешних программ Windows: Проводник и деинсталляторы."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .. import config
from ..actions import uninstaller
from ..analyzers.base import index_path, path_comparison
from ..analyzers.overview import find_directory
from ..core import db, safety
from .workspace import DbPath, read


def open_uninstaller(app_id: int, *, confirmed: bool, db_path: DbPath = None) -> None:
    if not confirmed:
        raise ValueError("Подтвердите запуск деинсталлятора")
    from ..collectors import apps

    with read(db_path) as con:
        if not db.has_table(con, "apps"):
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


def open_in_explorer(path: str, *, db_path: DbPath = None) -> None:
    """Открыть папку или выделить файл в Проводнике без запуска файла."""
    with read(db_path) as con:
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
