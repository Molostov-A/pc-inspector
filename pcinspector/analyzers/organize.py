"""Отчёты для наведения порядка в пользовательских файлах."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from datetime import datetime
from difflib import SequenceMatcher
from typing import ClassVar

from .. import config
from ..core import settings
from ..core.fmt import DAY
from ..core.models import Finding, Report
from .base import P_LIMIT, P_ROOT, Analyzer, Param, index_path, path_comparison, register, root_filter
from .rules import CATEGORIES, category

P_DOWNLOADS = Param("path", "str", "", "Папка загрузок (по умолчанию %USERPROFILE%\\Downloads)")
VERSION = re.compile(r"\d+(?:[._-]\d+){1,3}")
INSTALLER_WORDS = re.compile(r"\b(?:setup|installer|install|portable|win(?:dows)?|x86|x64|arm64|amd64|bit)\b")
MEDIA_FOLDERS = ("videos", "movies", "media", "video", "pictures", "видео", "фильмы", "медиа", "фото")
MEDIA_TYPES = sorted(CATEGORIES["Видео"] | {"iso", "img", "vhd", "vhdx", "vmdk", "wim", "dmg", "nrg", "mdf"})


@register
class EmptyDirectories(Analyzer):
    id = "empty_dirs"
    title = "Пустые папки"
    description = "Ищет папки без файлов во всём поддереве по данным скана."
    params: ClassVar[list[Param]] = [P_ROOT, P_LIMIT]

    def run(self, con: sqlite3.Connection, *, root: str, limit: int) -> Report:
        report = Report(self.id, self.title)
        scope, values = root_filter("d.path", root)
        for row in con.execute(
            f"""
            SELECT d.path, EXISTS (SELECT 1 FROM dirs c WHERE c.parent_id=d.id) AS has_children
            FROM dirs d WHERE d.depth>0 AND d.protected=0 AND d.total_files=0 AND {scope}
            AND NOT EXISTS (
                SELECT 1 FROM errors e WHERE e.root_id=d.root_id AND (
                    pci_casefold(e.path)=pci_casefold(d.path)
                    OR instr(pci_casefold(e.path),pci_casefold(d.path || ?))=1
                )
            )
            ORDER BY d.depth DESC, d.path LIMIT ?
            """,
            (*values, os.sep, limit),
        ):
            report.findings.append(
                Finding(
                    "dir",
                    row["path"],
                    path=row["path"],
                    safety="safe",
                    reason="В поддереве нет файлов по данным индекса",
                    meta={"action": "recycle" if row["has_children"] else "delete_empty_dir"},
                )
            )
        return report


@register
class DownloadsSort(Analyzer):
    id = "downloads_sort"
    title = "Разложить загрузки"
    description = "Предлагает перемещения файлов из папки загрузок по категориям и годам изменения."
    params: ClassVar[list[Param]] = [P_DOWNLOADS, P_LIMIT]

    def run(self, con: sqlite3.Connection, *, path: str, limit: int) -> Report:
        folder = index_path(path or config.downloads_dir())
        column, value = path_comparison("d.path", folder)
        report = Report(self.id, self.title)
        for row in con.execute(
            f"""SELECT f.* FROM dirs d JOIN files_v f ON f.dir_id=d.id
                WHERE {column}=? COLLATE NOCASE AND d.protected=0 ORDER BY f.size DESC, f.path LIMIT ?""",
            (value, limit),
        ):
            group = category(row["ext"])
            year = str(datetime.fromtimestamp(row["mtime"]).year) if row["mtime"] else "Без даты"
            destination = os.path.join(
                os.path.dirname(row["path"]), group.replace(" / ", " и "), year, row["name"]
            )
            report.findings.append(
                Finding(
                    "file",
                    row["name"],
                    row["size"],
                    row["path"],
                    category=group,
                    reason=f"{group} · {year}",
                    meta={"target": destination, "action": "move", "mtime": row["mtime"]},
                )
            )
        if not report.findings:
            report.notes.append(f"В индексе нет файлов непосредственно в папке {folder}.")
        return report


def _version(value: str) -> tuple[int, ...]:
    match = VERSION.search(value)
    if match is None:
        return ()
    parts = [int(part) for part in re.split(r"[._-]", match.group())]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _product_name(value: str) -> str:
    separated = re.sub(r"[\W_]+", " ", VERSION.sub(" ", value.casefold()))
    return "".join(INSTALLER_WORDS.sub(" ", separated).split())


@register
class InstalledInstallers(Analyzer):
    id = "installed_installers"
    title = "Установщики установленных программ"
    description = "Сопоставляет установщики в загрузках с именем и версией установленной программы."
    params: ClassVar[list[Param]] = [P_DOWNLOADS, P_LIMIT]

    def run(self, con: sqlite3.Connection, *, path: str, limit: int) -> Report:
        report = Report(self.id, self.title)
        versions: dict[tuple[int, ...], list[tuple[str, str, str]]] = {}
        if con.execute("SELECT 1 FROM sqlite_schema WHERE name='apps'").fetchone():
            for app in con.execute("SELECT name,version FROM apps WHERE version IS NOT NULL"):
                version = _version(app["version"])
                name = _product_name(app["name"])
                if version and name:
                    versions.setdefault(version, []).append((name, app["name"], app["version"]))
        if not versions:
            report.notes.append("Соберите список программ на вкладке «Забытое → Неиспользуемые программы».")
            return report
        scope, values = root_filter("path", path or config.downloads_dir())
        for row in con.execute(
            f"""SELECT * FROM files_v WHERE protected=0 AND {scope}
                AND ext IN ('exe','msi','msix','msixbundle','appx','appxbundle') ORDER BY size DESC, path""",
            values,
        ):
            stem = os.path.splitext(row["name"])[0]
            name = _product_name(stem)
            matches = versions.get(_version(stem), ())
            best = max(
                ((SequenceMatcher(None, name, app[0]).ratio(), app[1], app[2]) for app in matches),
                default=(0.0, "", ""),
            )
            if best[0] < 0.8:
                continue
            report.findings.append(
                Finding(
                    "file",
                    row["name"],
                    row["size"],
                    row["path"],
                    "review",
                    reason=f"Имя и версия похожи на установленную программу: {best[1]} {best[2]}",
                    meta={"app_name": best[1], "app_version": best[2]},
                )
            )
            if len(report.findings) == limit:
                break
        return report


@register
class ScatteredProjects(Analyzer):
    id = "scattered_projects"
    title = "Разрозненные проекты"
    description = "Ищет проекты вне основной папки и выделяет давние проекты с тяжёлыми зависимостями."
    params: ClassVar[list[Param]] = [
        P_ROOT,
        P_LIMIT,
        Param("projects_root", "str", "", "Основная папка проектов из настроек"),
    ]

    def run(self, con: sqlite3.Connection, *, root: str, limit: int, projects_root: str) -> Report:
        preferences = settings.load(con)
        projects_root = projects_root or preferences["projects_root"]
        scope, values = root_filter("d.path", root)
        primary, primary_values = root_filter("d.path", projects_root)
        cutoff = time.time() - preferences["stale_days"] * DAY
        report = Report(self.id, self.title)
        if not projects_root:
            primary = "0"
            report.notes.append("Укажите основную папку проектов в настройках для исключения её из отчёта.")
        for row in con.execute(
            f"""
            WITH markers AS (
                SELECT parent_id AS dir_id FROM dirs WHERE name='.git' COLLATE NOCASE
                UNION SELECT dir_id FROM files WHERE name COLLATE NOCASE
                IN ('.git','pyproject.toml','package.json','Cargo.toml','go.mod','pom.xml','Gemfile')
            ), projects AS (
                SELECT d.path, d.total_size,
                  coalesce((SELECT sum(total_size) FROM dirs WHERE parent_id=d.id
                    AND name COLLATE NOCASE IN ('.venv','node_modules')),0) AS deps,
                  coalesce((SELECT max(mtime) FROM (
                    SELECT max(mtime) AS mtime FROM files WHERE dir_id=d.id
                    UNION ALL SELECT max(newest_mtime) FROM dirs WHERE parent_id=d.id
                      AND name COLLATE NOCASE NOT IN ('.git','.venv','node_modules')
                  )),d.mtime) AS activity
                FROM markers m JOIN dirs d ON d.id=m.dir_id
                WHERE d.protected=0 AND {scope} AND NOT ({primary})
                AND instr(pci_casefold(d.path || ?),?)=0
                AND instr(pci_casefold(d.path || ?),?)=0
                AND instr(pci_casefold(d.path || ?),?)=0
            )
            SELECT *, coalesce(activity<=? AND deps>=?,0) AS stale
            FROM projects ORDER BY stale DESC, deps DESC, total_size DESC, path LIMIT ?
            """,
            (
                *values,
                *primary_values,
                os.sep,
                f"{os.sep}node_modules{os.sep}",
                os.sep,
                f"{os.sep}.venv{os.sep}",
                os.sep,
                f"{os.sep}.git{os.sep}",
                cutoff,
                100 * 1024**2,
                limit,
            ),
        ):
            stale = row["stale"]
            report.findings.append(
                Finding(
                    "dir",
                    row["path"],
                    row["total_size"],
                    row["path"],
                    "review" if stale else "info",
                    reason="Давний проект с тяжёлыми зависимостями" if stale else "Найден маркер проекта",
                    meta={"dependency_size": row["deps"], "newest_mtime": row["activity"]},
                )
            )
        return report


@register
class BigMedia(Analyzer):
    id = "big_media"
    title = "Крупные видео и образы дисков"
    description = "Ищет крупные видео и образы дисков вне обычных папок для медиа."
    params: ClassVar[list[Param]] = [
        P_ROOT,
        P_LIMIT,
        Param("min_size", "size", "1G", "Минимальный размер видео или образа"),
    ]

    def run(self, con: sqlite3.Connection, *, root: str, limit: int, min_size: int) -> Report:
        report = Report(self.id, self.title)
        scope, values = root_filter("path", root)
        extensions = ",".join("?" for _ in MEDIA_TYPES)
        folders = " AND ".join("instr(pci_casefold(path),?)=0" for _ in MEDIA_FOLDERS)
        for row in con.execute(
            f"""SELECT path,name,size,ext FROM files_v WHERE protected=0 AND size>=?
                AND ext IN ({extensions}) AND {folders} AND {scope} ORDER BY size DESC, path LIMIT ?""",
            (min_size, *MEDIA_TYPES, *(f"{os.sep}{name}{os.sep}" for name in MEDIA_FOLDERS), *values, limit),
        ):
            report.findings.append(
                Finding(
                    "file",
                    row["name"],
                    row["size"],
                    row["path"],
                    "review",
                    reason="Крупное видео или образ диска вне медиа-папок",
                )
            )
        return report
