"""Настройки, предпросмотр, подтверждение и откат перемещения (F6.6, F6.10)."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_api import ORIGIN, _job, _token

from pcinspector import config, services
from pcinspector.api.app import create_app
from pcinspector.core import db, scanner


def test_settings_and_downloads_move(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "QUARANTINE_DIR", None)
    root, index = tmp_path / "root", tmp_path / "index.db"
    downloads = root / "Downloads"
    source = downloads / "report.txt"
    downloads.mkdir(parents=True)
    source.write_text("example", encoding="utf-8")
    (root / "skip").mkdir()
    (root / "skip" / "ignored.txt").touch()
    with TestClient(create_app(db_path=index), base_url=ORIGIN) as client:
        settings = client.get("/api/settings").json()
        assert settings["stale_days"] == 365 and not index.exists()
        settings.update(
            scan_roots=[str(root), str(root).upper()],
            exclude_paths=["skip", "Skip", str(root / "unused")],
            stale_days=120,
            projects_root=str(root / "projects"),
            schedule_enabled=True,
            schedule_day="TUE",
            schedule_time="07:30",
            quarantine_path=str(tmp_path / "quarantine"),
        )
        assert client.put("/api/settings", json=settings).status_code == 403
        headers = {"Origin": ORIGIN, "X-PCI-Token": _token(client)}
        for invalid in (
            {"schedule_time": "25:00"},
            {"scan_roots": ["relative"]},
            {"exclude_paths": [".."]},
            {"stale_days": 0},
            {"projects_root": r"\\network\share"},
            {"exclude_paths": ["NUL"]},
            {"exclude_paths": [r"C:folder"]},
        ):
            assert client.put("/api/settings", json=settings | invalid, headers=headers).status_code == 422
        saved = client.put("/api/settings", json=settings, headers=headers)
        assert saved.status_code == 200
        assert client.get("/api/settings").json() == saved.json()
        assert saved.json()["scan_roots"] == [str(root)]
        assert saved.json()["exclude_paths"] == ["skip", str(root / "unused")]
        job = client.post("/api/scan", json={"paths": []}, headers=headers).json()
        assert _job(client, job["job_id"])["state"] == "completed"
        assert client.get("/api/analyzers/stale_files").json()["params"]["days"] == 120
        assert (
            not client.get("/api/analyzers/top_files").json()["findings"][0]["path"].endswith("ignored.txt")
        )
        report = client.get("/api/analyzers/downloads_sort", params={"path": str(downloads)}).json()
        target = Path(report["findings"][0]["meta"]["target"])
        draft = services.create_plan("Перемещения", db_path=index)
        plan_id = draft["id"]
        items = services.add_plan_items(
            plan_id,
            [
                services.PlanItemIn(str(source), "move", target=str(target)),
            ],
            db_path=index,
        )
        assert items["accepted"] and source.is_file() and not target.parent.exists()
        with pytest.raises(ValueError, match="Подтверждение"):
            services.execute_plan(plan_id, "invalid", db_path=index)
        assert source.is_file() and not target.parent.exists()
        draft = services.get_plan(plan_id, db_path=index)
        token = services.plan_confirmation(plan_id, draft["revision"], db_path=index)
        assert services.execute_plan(plan_id, token, db_path=index)["done"] == 1
        assert not source.exists() and target.read_text(encoding="utf-8") == "example"
        entry = services.action_history(db_path=index)[0]
        assert services.restore_action(entry["id"], db_path=index)["restored"]
        assert source.read_text(encoding="utf-8") == "example" and not target.exists()


def test_settings_quarantine_and_blocked_move_parent(
    index: Path,
    fake_tree: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "QUARANTINE_DIR", None)
    quarantine = tmp_path / "custom"
    saved = services.get_settings(db_path=index)
    saved["quarantine_path"] = str(quarantine)
    services.save_settings(saved, db_path=index)
    source = fake_tree / "Документы" / "отчёт.txt"
    plan = services.create_plan("Карантин", db_path=index)
    batch = services.add_plan_items(
        plan["id"],
        [
            services.PlanItemIn(str(source), "move", target=str(quarantine / "elsewhere" / source.name)),
            services.PlanItemIn(
                str(source), "move", target=str(source.with_name("копия.bin") / "child" / "file")
            ),
            services.PlanItemIn(str(source), "quarantine"),
        ],
        db_path=index,
    )
    assert "управляемым карантином" in batch["rejected"][0]["reason"]
    assert "должен быть папкой" in batch["rejected"][1]["reason"]
    assert len(batch["accepted"]) == 1
    current = services.get_plan(plan["id"], db_path=index)
    token = services.plan_confirmation(plan["id"], current["revision"], db_path=index)
    assert services.execute_plan(plan["id"], token, db_path=index)["done"] == 1
    entry = services.action_history(db_path=index)[0]
    moved = Path(entry["dst"])
    assert moved.is_relative_to(quarantine) and moved.is_file()
    saved["quarantine_path"] = str(tmp_path / "new-quarantine")
    services.save_settings(saved, db_path=index)
    with closing(db.connect(index)) as con:
        scanner.scan(str(quarantine), con)
    second = services.create_plan("Проверка старого карантина", db_path=index)
    batch = services.add_plan_items(second["id"], [services.PlanItemIn(str(moved))], db_path=index)
    assert "управляемым карантином" in batch["rejected"][0]["reason"]
    assert services.restore_action(entry["id"], db_path=index)["restored"]
    assert source.is_file()
