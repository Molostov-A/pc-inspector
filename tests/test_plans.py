"""Планы и подтверждение через API и CLI на временном индексе (F3.2, F3.6)."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from test_api import ORIGIN, _token

from pcinspector import cli, config, services
from pcinspector.actions import executor, plans
from pcinspector.api.app import create_app
from pcinspector.core import db


def test_plan_validates_index_and_overlap(index: Path, fake_tree: Path, tmp_path: Path) -> None:
    external = tmp_path / "unindexed.bin"
    external.touch()
    parent = fake_tree / "Документы"
    child = parent / "отчёт.txt"
    plan_id = services.create_plan("Проверка", db_path=index)["id"]
    batch = services.add_plan_items(
        plan_id,
        [
            services.PlanItemIn(str(parent)),
            services.PlanItemIn(str(child)),
            services.PlanItemIn(str(external)),
            services.PlanItemIn(str(fake_tree.anchor)),
            services.PlanItemIn(str(fake_tree / "Архив"), "move", target=str(child)),
        ],
        db_path=index,
    )
    assert len(batch["accepted"]) == 1 and len(batch["rejected"]) == 4
    assert "пересекается" in batch["rejected"][0]["reason"]
    assert "индексе" in batch["rejected"][1]["reason"]
    assert "занят" in batch["rejected"][-1]["reason"]
    entry = batch["accepted"][0]["id"]
    updated = services.edit_plan_item(plan_id, entry, selected=False, db_path=index)
    assert updated["selected_count"] == 0
    with pytest.raises(ValueError, match="Выберите"):
        services.plan_confirmation(plan_id, updated["revision"], db_path=index)
    assert services.edit_plan_item(plan_id, entry, remove=True, db_path=index)["items"] == []
    assert services.edit_plan(plan_id, cancel=True, db_path=index)["status"] == "cancelled"


def test_web_confirm_quarantine_restore(
    index: Path,
    fake_tree: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "QUARANTINE_DIR", tmp_path / "quarantine")
    source = fake_tree / "Документы" / "отчёт.txt"
    data = source.read_bytes()
    with TestClient(create_app(db_path=index), base_url=ORIGIN) as client:
        headers = {"Origin": ORIGIN, "X-PCI-Token": _token(client)}
        draft = client.post("/api/plans", json={"title": "Черновик"}, headers=headers).json()
        endpoint = f"/api/plans/{draft['id']}"
        response = client.post(
            endpoint + "/items",
            headers=headers,
            json={
                "items": [
                    {"path": str(source), "action": "quarantine"},
                ]
            },
        )
        assert response.json()["accepted"]
        draft = client.get(endpoint).json()
        confirm = endpoint + "/confirm-token"
        assert client.get(confirm, params={"revision": draft["revision"]}).status_code == 403
        assert client.post(endpoint + "/execute", json={"confirm_token": "fake"}).status_code == 403
        assert (
            client.post(endpoint + "/execute", json={"confirm_token": "fake"}, headers=headers).status_code
            == 422
        )
        assert source.exists()
        issued = client.get(confirm, headers=headers, params={"revision": draft["revision"]}).json()
        executed = client.post(endpoint + "/execute", json=issued, headers=headers).json()
        assert executed["done"] == 1 and executed["processed_size"] == len(data)
        assert not source.exists()
        row = client.get("/api/history").json()[0]
        assert client.get("/api/quarantine").json()["size"] == len(data)
        assert client.post(f"/api/history/{row['id']}/restore", headers=headers).json()["restored"]
        assert source.read_bytes() == data


def test_cli_declines_and_accepts_yes(index: Path, fake_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = fake_tree / "Документы" / "отчёт.txt"
    calls: list[str] = []
    monkeypatch.setattr(executor, "send2trash", calls.append)
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert cli.main(["plan", "create", "Проверка", "--db", str(index)]) == 0
    assert cli.main(["plan", "add", "1", str(source), "--action", "recycle", "--db", str(index)]) == 0
    arguments = ["plan", "execute", "1", "--db", str(index)]
    assert cli.main(arguments) == 1
    assert not calls
    assert cli.main([*arguments, "--yes"]) == 0
    assert calls == [str(source)]


def test_invalid_operations_and_index_metadata(
    index: Path,
    fake_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = fake_tree / "Документы" / "отчёт.txt"
    directory = fake_tree / "Документы"
    with pytest.raises(ValueError, match="Укажите текст"):
        services.create_plan("   ", db_path=index)
    with pytest.raises(KeyError, match="План не найден"):
        services.get_plan(999, db_path=index)
    plan = services.create_plan("Проверка", db_path=index)
    plan_id = plan["id"]
    with pytest.raises(KeyError, match="Пункт плана"):
        services.edit_plan_item(plan_id, 999, selected=False, db_path=index)
    candidates = [
        services.PlanItemIn(str(file), cast(plans.Action, "unknown")),
        services.PlanItemIn(str(file), "delete_empty_dir"),
        services.PlanItemIn(str(file), "recycle", target=str(file)),
        services.PlanItemIn(str(file), "move"),
        services.PlanItemIn(str(directory), "move", target=str(directory / "nested")),
        services.PlanItemIn(str(directory), "delete_empty_dir"),
    ]
    assert len(services.add_plan_items(plan_id, candidates, db_path=index)["rejected"]) == len(candidates)
    with closing(db.connect(index)) as con, con:
        con.execute("UPDATE dirs SET protected=1 WHERE path=?", (str(directory),))
    assert (
        "Защищённый"
        in services.add_plan_items(
            plan_id,
            [services.PlanItemIn(str(file))],
            db_path=index,
        )["rejected"][0]["reason"]
    )
    with closing(db.connect(index)) as con, con:
        con.execute("UPDATE dirs SET protected=0 WHERE path=?", (str(directory),))
        con.execute("UPDATE files SET mtime=NULL WHERE name=?", (file.name,))
    assert (
        "не содержит даты"
        in services.add_plan_items(
            plan_id,
            [services.PlanItemIn(str(directory))],
            db_path=index,
        )["rejected"][0]["reason"]
    )
    monkeypatch.setattr(plans.safety, "checked_action_path", lambda path, **_options: Path(path))
    remote = Path("Z:/destination" if file.drive.lower() != "z:" else "Y:/destination")
    with pytest.raises(ValueError, match="одного диска"):
        plans.validate_operation(file, "file", "move", str(remote))
    assert services.list_plans(db_path=index)[0]["item_count"] == 0


def test_plan_size_limit_and_action_edit(
    index: Path, fake_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = fake_tree / "Документы" / "отчёт.txt"
    candidate = services.PlanItemIn(str(file), safety="unknown")
    plan = services.create_plan("Проверка", db_path=index)
    plan_id = plan["id"]
    monkeypatch.setattr(plans, "MAX_ITEMS", 1)
    with pytest.raises(ValueError, match="За раз"):
        services.add_plan_items(plan_id, [candidate, candidate], db_path=index)
    item = services.add_plan_items(plan_id, [candidate], db_path=index)["accepted"][0]
    assert item["safety"] == "review"
    assert (
        "В плане уже" in services.add_plan_items(plan_id, [candidate], db_path=index)["rejected"][0]["reason"]
    )
    target = fake_tree / "Новое имя"
    edited = services.edit_plan_item(plan_id, item["id"], action="move", target=str(target), db_path=index)
    assert edited["items"][0]["target"] == str(target)
    assert (
        services.edit_plan_item(plan_id, item["id"], action="quarantine", db_path=index)["items"][0]["target"]
        is None
    )
