"""Базовый класс и реестр анализаторов.

Чтобы добавить анализатор:
  1. создать модуль в pcinspector/analyzers/
  2. унаследоваться от Analyzer, задать id/title/description/params, реализовать run()
  3. повесить декоратор @register
  4. добавить импорт модуля в analyzers/__init__.py
Всё. CLI-подкоманда, API-эндпоинт и MCP-инструмент появятся автоматически.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, ClassVar

from ..core.fmt import parse_size
from ..core.models import Report


@dataclass(frozen=True)
class Param:
    name: str
    type: str  # "int" | "str" | "size" | "bool"
    default: Any
    help: str

    def coerce(self, value: Any) -> Any:
        if value is None:
            return self.default
        if self.type == "int":
            return int(value)
        if self.type == "size":
            return parse_size(value)
        if self.type == "bool":
            return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes")
        return str(value)


class Analyzer:
    id: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str]  # 1–2 предложения: что ищет и зачем (видят и LLM через MCP)
    params: ClassVar[list[Param]] = []

    def run(self, con: sqlite3.Connection, **params: Any) -> Report:  # pragma: no cover
        raise NotImplementedError

    # Вызывать снаружи именно это: приводит типы и подставляет значения по умолчанию.
    def __call__(self, con: sqlite3.Connection, **raw: Any) -> Report:
        known = {p.name for p in self.params}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"{self.id}: неизвестные параметры {sorted(unknown)}")
        params = {p.name: p.coerce(raw.get(p.name)) for p in self.params}
        report = self.run(con, **params)
        report.params = params
        return report


REGISTRY: dict[str, Analyzer] = {}


def register(cls: type[Analyzer]) -> type[Analyzer]:
    if cls.id in REGISTRY:
        raise ValueError(f"Анализатор {cls.id!r} уже зарегистрирован")
    REGISTRY[cls.id] = cls()
    return cls


def get(analyzer_id: str) -> Analyzer:
    try:
        return REGISTRY[analyzer_id]
    except KeyError:
        raise KeyError(f"Нет анализатора {analyzer_id!r}. Есть: {', '.join(REGISTRY)}") from None


# Общие параметры, чтобы не повторять их в каждом анализаторе
P_LIMIT = Param("limit", "int", 30, "Сколько строк показать")
P_ROOT = Param("root", "str", "", "Ограничить путём (префикс), например C:\\Users")
P_MIN_SIZE = Param("min_size", "size", "100M", "Минимальный размер: 500K, 100M, 2G")


def root_filter(column: str, root: str) -> tuple[str, list[Any]]:
    """SQL-условие «путь внутри root» (без учёта регистра). Пустой root — без фильтра."""
    if not root:
        return "1=1", []
    root = root.rstrip("\\")
    return (
        f"({column} = ? COLLATE NOCASE OR {column} LIKE ? ESCAPE '|')",
        [root, root.replace("|", "||").replace("%", "|%").replace("_", "|_") + "\\%"],
    )
