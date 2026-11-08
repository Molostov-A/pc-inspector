"""Двухпроходный поиск, кэш и отчёт о дубликатах (F2.1)."""

from __future__ import annotations

import json
import os
import time
from contextlib import closing
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient
from test_api import ORIGIN, _token

from pcinspector import cli, services
from pcinspector.analyzers.duplicates import _PREFERENCE
from pcinspector.api.app import create_app
from pcinspector.collectors import hashing
from pcinspector.core import db, jobs, scanner


def _wait(job_id: str) -> jobs.JobStatus:
    deadline = time.monotonic() + 10
    while (state := services.job_status(job_id)).state in ("queued", "running"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    return state


def _hash(index: Path, root: str = "") -> jobs.HashResult:
    status = _wait(services.start_hash(root, 1, db_path=index))
    assert status.state == "completed", status.errors
    assert status.hash_result is not None
    return status.hash_result


def test_duplicate_groups_cache_and_rescan(
    index: Path,
    fake_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "hash" in services.run_analyzer("duplicates", db_path=index).notes[0]
    result = _hash(index)
    assert (result["candidates"], result["partial_read"], result["full_read"], result["errors"]) == (
        2,
        2,
        2,
        0,
    )
    report = services.run_analyzer("duplicates", min_size=1, db_path=index)
    [group] = report.findings
    assert group.safety == "review" and group.size == 72 and group.meta["count"] == 2
    assert sum(copy["keep_suggested"] for copy in group.meta["copies"]) == 1
    assert {Path(copy["path"]).parent.name for copy in group.meta["copies"]} == {"Документы", "Архив"}
    assert not services.run_analyzer(
        "duplicates", min_size=1, root=str(fake_tree / "Архив"), db_path=index
    ).findings

    def unexpected_read(*_args: object) -> str:
        raise AssertionError("Содержимое файла должно браться из кэша")

    monkeypatch.setattr(hashing, "_digest", unexpected_read)
    with closing(db.connect(index)) as con:
        scanner.scan(str(fake_tree), con)
        assert con.execute("SELECT count(*) FROM file_hashes").fetchone()[0] == 2
    assert any(
        "обновился" in note for note in services.run_analyzer("duplicates", min_size=1, db_path=index).notes
    )
    repeated = _hash(index)
    assert (repeated["cached"], repeated["partial_read"], repeated["full_read"]) == (4, 0, 0)
    assert not services.run_analyzer("duplicates", min_size=1, db_path=index).notes


def test_partial_collision_and_hardlinks(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    first = root / "a.bin"
    data = b"x" * hashing.PART + b"1" + b"z" * hashing.PART
    for name in ("a.bin", "b.bin", "linked.bin"):
        (root / name).write_bytes(data)
    (root / "collision.bin").write_bytes(data.replace(b"1", b"2"))
    (root / "other.bin").write_bytes(b"k" * len(data))
    try:
        os.link(root / "linked.bin", root / "alias.bin")
    except OSError as ex:
        pytest.skip(f"Жёсткие ссылки недоступны: {ex}")
    index = tmp_path / "duplicates.db"
    with closing(db.connect(index)) as con:
        scanner.scan(str(root), con)
    result = _hash(index, str(root))
    assert result["hardlinks"] == 2
    assert (result["candidates"], result["partial_read"], result["full_read"], result["errors"]) == (
        6,
        4,
        3,
        0,
    )
    report = services.run_analyzer("duplicates", min_size=1, root=str(root), db_path=index)
    [group] = report.findings
    assert group.size == len(data)
    assert {Path(copy["path"]).name for copy in group.meta["copies"]} == {first.name, "b.bin"}


def test_changed_file_invalidates_group(index: Path, fake_tree: Path) -> None:
    _hash(index)
    source = fake_tree / "Архив" / "копия.bin"
    source.write_bytes(b"x" * 72)
    os.utime(source, (1700000000, 1700000000))
    failed = _hash(index)
    assert failed["errors"] == 1
    assert not services.run_analyzer("duplicates", min_size=1, db_path=index).findings
    with closing(db.connect(index)) as con:
        scanner.scan(str(fake_tree), con)
    result = _hash(index)
    assert (result["partial_read"], result["full_read"], result["errors"]) == (1, 0, 0)


def test_background_hash_can_cancel(index: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, resume = Event(), Event()
    digest = hashing._digest

    def slow(*args: object) -> str:
        entered.set()
        assert resume.wait(5)
        return digest(*args)

    monkeypatch.setattr(hashing, "_digest", slow)
    job_id = services.start_hash(min_size=1, db_path=index)
    try:
        assert entered.wait(5)
        state = services.job_status(job_id)
        assert state.hash_progress is not None and state.hash_progress.phase == "partial"
        assert services.cancel_job(job_id)
    finally:
        resume.set()
    assert _wait(job_id).state == "cancelled"


def test_hash_api_and_cli(index: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with TestClient(create_app(db_path=index), base_url=ORIGIN) as client:
        assert client.post("/api/hash", json={"min_size": 1}).status_code == 403
        headers = {"Origin": ORIGIN, "X-PCI-Token": _token(client)}
        response = client.post("/api/hash", json={"min_size": 1}, headers=headers)
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        assert _wait(job_id).state == "completed"
        assert client.get(f"/api/jobs/{job_id}").json()["hash_result"]["full_read"] == 2
        assert client.get("/api/analyzers/duplicates", params={"min_size": 1}).json()["findings"]
    assert cli.main(["hash", "--min-size", "1", "--db", str(index), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["hash_result"]["cached"] == 4


def test_selected_copy_cannot_already_be_in_plan(index: Path, fake_tree: Path) -> None:
    draft = services.create_plan("Дубликаты", db_path=index)
    first = fake_tree / "Документы" / "копия.bin"
    second = fake_tree / "Архив" / "копия.bin"
    plan_id = draft["id"]
    with pytest.raises(ValueError, match="должна быть файлом"):
        services.add_plan_items(
            plan_id, [services.PlanItemIn(str(first))], keep_path=str(second.parent), db_path=index
        )
    result = services.add_plan_items(
        plan_id,
        [
            services.PlanItemIn(str(second)),
            services.PlanItemIn(str(first), "move", target=str(second / "nested")),
            services.PlanItemIn(str(first)),
        ],
        keep_path=str(second),
        db_path=index,
    )
    assert len(result["rejected"]) == 2 and all("выбранную" in item["reason"] for item in result["rejected"])
    assert len(result["accepted"]) == 1
    with pytest.raises(ValueError, match="уже затронута"):
        services.add_plan_items(
            plan_id, [services.PlanItemIn(str(second))], keep_path=str(first), db_path=index
        )
    assert first.exists() and second.exists()


def test_zero_byte_files_if_requested(tmp_path: Path) -> None:
    root = tmp_path / "zero"
    root.mkdir()
    (root / "a").touch()
    (root / "b").touch()
    index = tmp_path / "index.db"
    with closing(db.connect(index)) as con:
        scanner.scan(str(root), con)
    assert _hash(index)["candidates"] == 0
    result = _wait(services.start_hash(min_size=0, db_path=index)).hash_result
    assert result is not None and result["full_read"] == 2
    [group] = services.run_analyzer("duplicates", min_size=0, db_path=index).findings
    assert group.size == 0 and group.meta["count"] == 2


def test_recommendation_uses_ctime_then_location() -> None:
    paths = (r"X:\Downloads\old.bin", r"X:\Desktop\recent.bin", r"X:\Documents\recent.bin")
    with closing(db.empty_index()) as con:
        rows = con.execute(
            "WITH copies(path,ctime) AS (VALUES (?,1),(?,2),(?,2)) "
            f"SELECT path FROM copies ORDER BY {_PREFERENCE}",
            paths,
        ).fetchall()
    assert [row["path"] for row in rows] == [paths[0], paths[2], paths[1]]


def test_protected_and_cloud_candidates_are_excluded(index: Path, fake_tree: Path) -> None:
    with closing(db.connect(index)) as con, con:
        con.execute(
            "UPDATE files SET attrs=? WHERE name='копия.bin' AND dir_id IN (SELECT id FROM dirs WHERE path=?)",
            (scanner.CLOUD, str(fake_tree / "Архив")),
        )
    assert _hash(index)["candidates"] == 0
    with closing(db.connect(index)) as con, con:
        con.execute("UPDATE files SET attrs=0")
        con.execute("UPDATE dirs SET protected=1 WHERE path=?", (str(fake_tree / "Архив"),))
    assert _hash(index)["candidates"] == 0
