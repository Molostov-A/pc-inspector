"""Приёмочные проверки временного индекса и конфигурации журнала."""

from __future__ import annotations

import logging
from contextlib import closing
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from pcinspector import config
from pcinspector.core import db


def test_temporary_index(index: Path, fake_tree: Path) -> None:
    with closing(db.connect(index, readonly=True)) as con:
        root = con.execute("SELECT path, file_count FROM roots").fetchone()
    assert Path(root["path"]) == fake_tree
    assert root["file_count"] == 6


def test_configure_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    logger = logging.getLogger("pcinspector")
    previous_level = logger.level
    original_handlers = logger.handlers.copy()
    try:
        config.configure_logging()
        config.configure_logging()
        [handler] = [handler for handler in logger.handlers if handler not in original_handlers]
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 5 * 1024 * 1024
        assert handler.backupCount == 5
        logging.getLogger("pcinspector.test").info("Проверка журнала")
        log_path = config.DATA_DIR / "logs" / "pci.log"
        assert "Проверка журнала" in log_path.read_text(encoding="utf-8")
        handler.doRollover()
        assert "Проверка журнала" in log_path.with_suffix(".log.1").read_text(encoding="utf-8")
    finally:
        for handler in logger.handlers.copy():
            if handler not in original_handlers:
                logger.removeHandler(handler)
                handler.close()
        logger.setLevel(previous_level)
