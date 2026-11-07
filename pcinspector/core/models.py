"""Единый формат результатов анализа.

Любой анализатор возвращает Report со списком Finding. CLI, веб-API и MCP
только отображают/сериализуют Report — поэтому новый анализатор сразу
доступен во всех интерфейсах.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# safe   — можно удалять без раздумий (кэш, временное; восстановится само)
# review — вероятно не нужно, но решает человек
# keep   — найдено, но трогать не стоит (системное, защищённое)
# info   — просто информация (размер папки, статистика)
Safety = Literal["safe", "review", "keep", "info"]
Kind = Literal["dir", "file", "group", "app", "stat"]


@dataclass
class Finding:
    kind: Kind
    title: str  # коротко: путь, имя приложения, расширение...
    size: int = 0  # байты, которые занимает (или освободятся)
    path: str | None = None  # абсолютный путь, если применимо
    safety: Safety = "info"
    category: str | None = None  # группа внутри отчёта (id правила, категория типа)
    reason: str | None = None  # почему найдено — человеку понятным языком
    meta: dict[str, Any] = field(default_factory=dict)  # всё остальное (даты, счётчики)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Report:
    analyzer: str
    title: str
    params: dict[str, Any] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)  # итоги для шапки отчёта
    notes: list[str] = field(default_factory=list)  # предупреждения/оговорки

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "analyzer": self.analyzer,
            "title": self.title,
            "params": self.params,
            "summary": self.summary,
            "total_size": self.total_size,
            "notes": self.notes,
            "findings": [f.to_dict() for f in self.findings],
        }
