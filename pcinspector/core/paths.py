"""Нормализация фильтров путей для запросов к индексу."""

from __future__ import annotations

import os


def index_path(path: str) -> str:
    if os.name == "nt" and len(path) == 2 and path[1] == ":":
        path += "\\"
    return os.path.normpath(path)


def path_comparison(column: str, path: str) -> tuple[str, str]:
    if path.isascii():
        return column, path
    return f"pci_casefold({column})", path.casefold()


def root_filter(column: str, root: str) -> tuple[str, list[str]]:
    """Вернуть параметризованное условие для папки и её поддерева."""
    if not root:
        return "1=1", []
    column, root = path_comparison(column, index_path(root))
    prefix = root if root.endswith(os.sep) else root + os.sep
    return (
        f"({column} = ? COLLATE NOCASE OR {column} LIKE ? ESCAPE '|')",
        [root, prefix.replace("|", "||").replace("%", "|%").replace("_", "|_") + "%"],
    )
