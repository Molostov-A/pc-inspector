"""Загрузка правил мусора и категорий файлов из rules.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

with Path(__file__).with_name("rules.toml").open("rb") as stream:
    _DATA: dict[str, Any] = tomllib.load(stream)

APP_LEFTOVER_SYSTEM_NAMES = frozenset(_DATA["app_leftover_system_names"])
CATEGORIES: dict[str, set[str]] = {name: set(values) for name, values in _DATA["categories"].items()}
EXT_TO_CAT = {ext: name for name, values in CATEGORIES.items() for ext in values}
DIR_PATH_RULES: list[dict[str, Any]] = _DATA["dir_path"]
DIR_NAME_RULES: list[dict[str, Any]] = _DATA["dir_name"]
FILE_RULES: list[dict[str, Any]] = _DATA["file"]


def category(ext: str) -> str:
    return EXT_TO_CAT.get(ext, "Прочее")
