"""Анализаторы. Импорт модуля регистрирует его анализаторы в REGISTRY.

Новый модуль-анализатор нужно добавить в список импортов ниже.
"""

from . import apps, duplicates, growth, junk, organize, overview, stale  # noqa: F401
from .base import REGISTRY, Analyzer, Param, get, register

__all__ = ["REGISTRY", "Analyzer", "Param", "get", "register"]
