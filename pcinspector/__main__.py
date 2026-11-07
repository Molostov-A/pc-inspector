"""Точка входа в приложение при запуске через python -m pcinspector."""

from __future__ import annotations

from .cli import main
from .config import configure_logging

configure_logging()
raise SystemExit(main())
