"""Форматирование для людей (размеры, даты, возраст)."""

from __future__ import annotations

import time
from datetime import datetime

DAY = 86_400


def human_size(n: float | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} ТБ"


def parse_size(s: str | int) -> int:
    """'500M', '2G', '100k', '1024' -> байты."""
    if isinstance(s, int):
        return s
    s = s.strip().upper().rstrip("B")
    mult = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    if s and s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(float(s))


def ts_date(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "—"


def age_days(ts: float | None) -> int | None:
    return int((time.time() - ts) // DAY) if ts else None
