"""Поиск мусора по правилам из rules.py: кэши, временные файлы, зависимости, дампы."""

from __future__ import annotations

import sqlite3
from typing import ClassVar

from ..core.models import Finding, Report
from .base import P_LIMIT, P_ROOT, Analyzer, Param, register, root_filter
from .rules import DIR_NAME_RULES, DIR_PATH_RULES, FILE_RULES


def _drop_nested(findings: list[Finding]) -> list[Finding]:
    """Если папка уже найдена, вложенные в неё находки не считаем повторно."""
    out: list[Finding] = []
    kept: list[str] = []
    for f in sorted(findings, key=lambda f: (f.path or "").lower()):
        p = (f.path or "").lower()
        if any(p == k or p.startswith(k + "\\") for k in kept):
            continue
        out.append(f)
        if f.kind == "dir":
            kept.append(p.rstrip("\\"))
    return out


@register
class Junk(Analyzer):
    id = "junk"
    title = "Мусор: кэши, временные файлы, зависимости"
    description = (
        "Находит кэши, временные файлы, node_modules, .venv, артефакты сборки, дампы, "
        "установщики в Загрузках. У каждой находки safety: safe (можно удалять) или review."
    )
    params: ClassVar[list[Param]] = [
        P_ROOT,
        Param("min_size", "size", "1M", "Не показывать находки меньше"),
        P_LIMIT,
        Param("group", "bool", True, "Сгруппировать по правилам (false — список путей)"),
    ]

    def run(self, con: sqlite3.Connection, root: str, min_size: int, limit: int, group: bool) -> Report:
        found: list[Finding] = []
        rcond, rargs = root_filter("d.path", root)

        for rule in DIR_NAME_RULES:
            marks = ",".join("?" * len(rule["names"]))
            sql = f"SELECT d.* FROM dirs d WHERE d.name COLLATE NOCASE IN ({marks}) AND {rcond}"
            args: list = [*rule["names"], *rargs]
            if "path_like" in rule:
                sql += " AND d.path LIKE ?"
                args.append(rule["path_like"])
            if "require_file" in rule:
                sql += (
                    " AND EXISTS (SELECT 1 FROM files f WHERE f.dir_id = d.id AND f.name = ? COLLATE NOCASE)"
                )
                args.append(rule["require_file"])
            if "sibling_ext" in rule:
                m = ",".join("?" * len(rule["sibling_ext"]))
                sql += f" AND EXISTS (SELECT 1 FROM files f WHERE f.dir_id = d.parent_id AND f.ext IN ({m}))"
                args.extend(rule["sibling_ext"])
            for d in con.execute(sql, args):
                found.append(self._dir(d, rule))

        for rule in DIR_PATH_RULES:
            sql = f"SELECT d.* FROM dirs d WHERE d.path LIKE ? AND {rcond}"
            for d in con.execute(sql, (rule["like"], *rargs)):
                found.append(self._dir(d, rule))

        fcond, fargs = root_filter("path", root)
        for rule in FILE_RULES:
            m = ",".join("?" * len(rule["exts"]))
            sql = f"SELECT * FROM files_v WHERE ext IN ({m}) AND path LIKE ? AND size > 0 AND {fcond}"
            for f in con.execute(sql, (*rule["exts"], rule["dir_like"], *fargs)):
                found.append(
                    Finding(
                        "file",
                        f["name"],
                        f["size"],
                        f["path"],
                        safety=rule["safety"],
                        category=rule["id"],
                        reason=rule["title"],
                        meta={"mtime": f["mtime"], "hint": rule["hint"]},
                    )
                )

        found = [f for f in _drop_nested(found) if f.size >= min_size]
        rep = Report(self.id, self.title)
        rep.summary = {
            "safe_size": sum(f.size for f in found if f.safety == "safe"),
            "review_size": sum(f.size for f in found if f.safety == "review"),
            "items": len(found),
        }
        if not group:
            rep.findings = sorted(found, key=lambda f: f.size, reverse=True)[:limit]
            return rep

        rules = {r["id"]: r for r in (*DIR_NAME_RULES, *DIR_PATH_RULES, *FILE_RULES)}
        groups: dict[str, list[Finding]] = {}
        for f in found:
            groups.setdefault(f.category or "", []).append(f)
        for rid, items in groups.items():
            rule = rules[rid]
            items.sort(key=lambda f: f.size, reverse=True)
            rep.findings.append(
                Finding(
                    "group",
                    rule["title"],
                    sum(f.size for f in items),
                    safety=rule["safety"],
                    category=rid,
                    reason=f"{len(items)} шт. — {rule['hint']}",
                    meta={"count": len(items), "top": [{"path": f.path, "size": f.size} for f in items[:10]]},
                )
            )
        rep.findings.sort(key=lambda f: f.size, reverse=True)
        return rep

    @staticmethod
    def _dir(d: sqlite3.Row, rule: dict) -> Finding:
        return Finding(
            "dir",
            d["name"],
            d["total_size"],
            d["path"],
            safety=rule["safety"],
            category=rule["id"],
            reason=rule["title"],
            meta={"files": d["total_files"], "newest_mtime": d["newest_mtime"], "hint": rule["hint"]},
        )
