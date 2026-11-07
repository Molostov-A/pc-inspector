"""Базовый класс и реестр анализаторов.

Чтобы добавить анализатор:
  1. создать модуль в pcinspector/analyzers/
  2. унаследоваться от Analyzer, задать id/title/description/params, реализовать run()
  3. повесить декоратор @register
  4. добавить импорт модуля в analyzers/__init__.py
Всё. CLI-подкоманда, API-эндпоинт и MCP-инструмент появятся автоматически.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from ..core.fmt import parse_size
from ..core.models import Report


@dataclass(frozen=True)
class Param:
    name: str
    type: Literal["int", "str", "size", "bool", "choice"]
    default: Any
    help: str
    choices: tuple[str, ...] = ()

    def coerce(self, value: Any) -> Any:
        if value is None:
            value = self.default
        if self.type == "int":
            number = int(value)
            if number < (1 if self.name == "limit" else 0):
                raise ValueError(f"{self.name}: недопустимое значение {number}")
            return number
        if self.type == "size":
            size = parse_size(value)
            if size < 0:
                raise ValueError(f"{self.name}: размер должен быть неотрицательным")
            return size
        if self.type == "bool":
            flag = str(value).lower()
            if flag in ("1", "true", "yes"):
                return True
            if flag in ("0", "false", "no"):
                return False
            raise ValueError(f"{self.name}: ожидается true или false")
        if self.type == "choice" and value not in self.choices:
            raise ValueError(f"{self.name}: выберите одно из значений: {', '.join(self.choices)}")
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


def index_path(path: str) -> str:
    if os.name == "nt" and len(path) == 2 and path[1] == ":":
        path += "\\"
    return os.path.normpath(path)


def path_comparison(column: str, path: str) -> tuple[str, str]:
    if path.isascii():
        return column, path
    return f"pci_casefold({column})", path.casefold()


def root_filter(column: str, root: str) -> tuple[str, list[Any]]:
    """SQL-условие «путь внутри root» (без учёта регистра). Пустой root — без фильтра."""
    if not root:
        return "1=1", []
    column, root = path_comparison(column, index_path(root))
    prefix = root if root.endswith(os.sep) else root + os.sep
    return (
        f"({column} = ? COLLATE NOCASE OR {column} LIKE ? ESCAPE '|')",
        [root, prefix.replace("|", "||").replace("%", "|%").replace("_", "|_") + "%"],
    )
