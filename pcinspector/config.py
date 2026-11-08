"""Пути и настройки. Всё, что зависит от окружения, — только здесь.

Переопределяется переменными окружения:
  PCI_DB          — путь к SQLite-индексу
  PCI_QUARANTINE  — папка карантина (этап 4)
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

DB_PATH = Path(os.environ.get("PCI_DB", DATA_DIR / "index.db"))
QUARANTINE_DIR = Path(os.environ["PCI_QUARANTINE"]) if os.environ.get("PCI_QUARANTINE") else None
EXPLORER_EXE = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe"


def configure_logging() -> None:
    """Настроить файловый журнал приложения с пятью архивами по 5 МиБ."""
    logger = logging.getLogger("pcinspector")
    log_path = DATA_DIR / "logs" / "pci.log"
    if any(
        isinstance(handler, RotatingFileHandler) and handler.baseFilename == str(log_path)
        for handler in logger.handlers
    ):
        return

    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
