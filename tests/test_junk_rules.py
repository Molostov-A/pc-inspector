"""Правила мусора на раскладках из реальных профилей Windows (F2)."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from pcinspector import services
from pcinspector.core import db, scanner

CLEAR = "clear_contents"
LAYOUTS = [
    ("AppData/Local/pip/cache/a", "AppData/Local/pip/cache", "pip_cache", "safe", CLEAR),
    ("AppData/Local/Unity/cache/a", "AppData/Local/Unity/cache", "unity_cache", "review", CLEAR),
    (
        "AppData/Local/Google/Chrome/User Data/Default/Cache/a",
        "AppData/Local/Google/Chrome/User Data/Default/Cache",
        "browser_cache",
        "safe",
        CLEAR,
    ),
    (
        "AppData/Local/Mozilla/Firefox/Profiles/x/cache2/a",
        "AppData/Local/Mozilla/Firefox/Profiles/x/cache2",
        "firefox_cache",
        "safe",
        CLEAR,
    ),
    ("AppData/Roaming/Figma/Code Cache/a", "AppData/Roaming/Figma/Code Cache", "engine_cache", "safe", CLEAR),
    (
        "AppData/Roaming/Telegram Desktop/tdata/cache/a",
        "AppData/Roaming/Telegram Desktop/tdata/cache",
        "app_cache",
        "review",
        CLEAR,
    ),
    ("AppData/Local/Temp/a", "AppData/Local/Temp", "user_temp", "safe", CLEAR),
    (
        "AppData/Local/D/Crashpad/reports/a.dmp",
        "AppData/Local/D/Crashpad/reports/a.dmp",
        "crashpad_dumps",
        "safe",
        "quarantine",
    ),
    ("AppData/Local/R/Core.dmp", "AppData/Local/R/Core.dmp", "dumps", "review", "quarantine"),
    ("Work/film.part", "Work/film.part", "partial", "review", "quarantine"),
]


@pytest.mark.parametrize(("file", "found", "rule", "safety", "action"), LAYOUTS)
def test_rule_for_layout(tmp_path: Path, file: str, found: str, rule: str, safety: str, action: str) -> None:
    root = tmp_path / "Users" / "U"
    path = root / file
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * 16)
    index = tmp_path / "rules.db"
    with closing(db.connect(index)) as con:
        scanner.scan(str(root), con)
    report = services.run_analyzer("junk", min_size=1, group=False, limit=100, db_path=index)
    assert [(item.path, item.category, item.safety, item.meta["action"]) for item in report.findings] == [
        (str(root / found), rule, safety, action)
    ]
