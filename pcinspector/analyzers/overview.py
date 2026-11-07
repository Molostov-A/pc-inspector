"""Обзор: что просканировано, дерево папок, крупные файлы, типы файлов."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import ClassVar

from ..core.models import Finding, Report
from .base import P_LIMIT, P_ROOT, Analyzer, Param, index_path, path_comparison, register, root_filter
from .rules import category


@register
class Roots(Analyzer):
    id = "roots"
    title = "Просканированные корни и диски"
    description = (
        "Список просканированных корней: размер, число файлов, дата скана, свободное место на диске."
    )
    params: ClassVar[list[Param]] = []

    def run(self, con: sqlite3.Connection) -> Report:
        rep = Report(self.id, self.title)
        for r in con.execute("SELECT * FROM roots ORDER BY path"):
            meta = dict(r)
            try:
                meta["scanned_at"] = float(r["scanned_at"])
            except (ValueError, TypeError):
                if r["scanned_at"]:
                    meta["scanned_at"] = datetime.fromisoformat(r["scanned_at"]).timestamp()
            rep.findings.append(
                Finding(
                    "stat",
                    r["path"],
                    r["total_size"] or 0,
                    r["path"],
                    reason=f"{r['file_count'] or 0:,} файлов",
                    meta=meta,
                )
            )
            if meta.get("system_files"):
                rep.findings.append(
                    Finding(
                        "group",
                        "Системные файлы",
                        meta["system_size"],
                        safety="keep",
                        reason=r["path"],
                        meta={"root": r["path"], "count": meta["system_files"]},
                    )
                )
        if not rep.findings:
            rep.notes.append("Индекс пуст. Запустите: python -m pcinspector scan C:\\")
        return rep


@register
class Tree(Analyzer):
    id = "tree"
    title = "Содержимое папки по размеру"
    description = (
        "Дочерние папки указанной папки, отсортированные по размеру (как WinDirStat). "
        "Без path — корни сканирования."
    )
    params: ClassVar[list[Param]] = [Param("path", "str", "", "Папка, например C:\\Users"), P_LIMIT]

    def run(self, con: sqlite3.Connection, path: str, limit: int) -> Report:
        rep = Report(self.id, self.title)
        if not path:
            rows = con.execute(
                "SELECT * FROM dirs WHERE parent_id IS NULL ORDER BY total_size DESC LIMIT ?", (limit,)
            ).fetchall()
            parent = None
        else:
            column, norm = path_comparison("path", index_path(path))
            parent = con.execute(
                f"SELECT * FROM dirs WHERE {column} = ? COLLATE NOCASE ORDER BY depth, id LIMIT 1",
                (norm,),
            ).fetchone()
            if parent is None:
                rep.notes.append(f"Папка {path} не найдена в индексе (не просканирована?)")
                return rep
            rows = con.execute(
                "SELECT * FROM dirs WHERE parent_id = ? ORDER BY total_size DESC LIMIT ?",
                (parent["id"], limit),
            ).fetchall()
            rep.summary = {
                "path": parent["path"],
                "total_size": parent["total_size"],
                "total_files": parent["total_files"],
            }
        for d in rows:
            rep.findings.append(
                Finding(
                    "dir",
                    d["name"],
                    d["total_size"],
                    d["path"],
                    safety="keep" if d["protected"] else "info",
                    reason=f"{d['total_files']:,} файлов",
                    meta={
                        "files": d["total_files"],
                        "newest_mtime": d["newest_mtime"],
                        "protected": d["protected"],
                    },
                )
            )
        if parent is not None and parent["own_files"]:
            rep.findings.append(
                Finding(
                    "file",
                    "[файлы в самой папке]",
                    parent["own_size"],
                    parent["path"],
                    safety="keep" if parent["protected"] else "info",
                    reason=f"{parent['own_files']:,} файлов",
                )
            )
            rep.findings.sort(key=lambda f: f.size, reverse=True)
        return rep


@register
class TopFiles(Analyzer):
    id = "top_files"
    title = "Самые большие файлы"
    description = "Крупнейшие файлы в индексе (или внутри root)."
    params: ClassVar[list[Param]] = [
        P_ROOT,
        P_LIMIT,
        Param("ext", "str", "", "Только это расширение, например mkv"),
    ]

    def run(self, con: sqlite3.Connection, root: str, limit: int, ext: str) -> Report:
        rep = Report(self.id, self.title)
        cond, args = root_filter("path", root)
        if ext:
            cond += " AND ext = ?"
            args.append(ext.lower().lstrip("."))
        for f in con.execute(
            f"SELECT * FROM files_v WHERE {cond} ORDER BY size DESC LIMIT ?", (*args, limit)
        ):
            rep.findings.append(
                Finding(
                    "file",
                    f["name"],
                    f["size"],
                    f["path"],
                    safety="keep" if f["protected"] else "info",
                    category=category(f["ext"]),
                    meta={"mtime": f["mtime"], "atime": f["atime"], "ctime": f["ctime"], "ext": f["ext"]},
                )
            )
        return rep


@register
class FileTypes(Analyzer):
    id = "types"
    title = "Занятое место по типам файлов"
    description = "Сумма размеров по категориям (видео, архивы, модели ИИ...) и по расширениям."
    params: ClassVar[list[Param]] = [
        P_ROOT,
        Param("by", "choice", "category", "Группировка", ("category", "ext")),
        P_LIMIT,
    ]

    def run(self, con: sqlite3.Connection, root: str, by: str, limit: int) -> Report:
        rep = Report(self.id, self.title)
        if root:
            cond, args = root_filter("path", root)
            sql = f"SELECT ext, SUM(size) s, COUNT(*) n FROM files_v WHERE {cond} GROUP BY ext"
        else:
            sql, args = "SELECT ext, SUM(size) s, COUNT(*) n FROM files GROUP BY ext", []
        rows = con.execute(sql, args).fetchall()
        if by == "ext":
            for r in sorted(rows, key=lambda r: r["s"] or 0, reverse=True)[:limit]:
                rep.findings.append(
                    Finding(
                        "group",
                        r["ext"] or "(без расширения)",
                        r["s"] or 0,
                        category=category(r["ext"]),
                        reason=f"{r['n']:,} файлов",
                        meta={"count": r["n"]},
                    )
                )
            return rep
        cats: dict[str, list] = defaultdict(lambda: [0, 0, []])
        for r in rows:
            c = cats[category(r["ext"])]
            c[0] += r["s"] or 0
            c[1] += r["n"]
            c[2].append((r["s"] or 0, r["ext"]))
        for name, (size, n, exts) in sorted(cats.items(), key=lambda kv: kv[1][0], reverse=True)[:limit]:
            top = ", ".join(e or "—" for _, e in sorted(exts, reverse=True)[:5])
            rep.findings.append(
                Finding("group", name, size, category=name, reason=f"{n:,} файлов; {top}", meta={"count": n})
            )
        return rep
