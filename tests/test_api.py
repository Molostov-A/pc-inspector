"""Локальный API, защита запросов и выдача индекса (F5, §9.7)."""

from __future__ import annotations

import re
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pcinspector import config, services
from pcinspector.api.app import create_app
from pcinspector.core import db, safety, scanner

ORIGIN = "http://127.0.0.1:8765"


def _token(client: TestClient) -> str:
    response = client.get("/")
    assert response.status_code == 200
    match = re.search(r'name="pci-token" content="([^"]+)"', response.text)
    assert match and match[1] != "__PCI_TOKEN__"
    assert response.headers["cache-control"] == "no-store"
    return match[1]


def _job(client: TestClient, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        status = response.json()
        if status["state"] not in ("queued", "running"):
            return status
        time.sleep(0.01)
    raise AssertionError("Сканирование не завершилось")


def test_host_origin_and_token(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    with TestClient(create_app(db_path=path), base_url=ORIGIN) as client:
        token = _token(client)
        assert _token(client) == token
        assert client.get("/", headers={"Host": "untrusted.example:8765"}).status_code == 403
        assert client.get("/api/roots", headers={"Host": "127.0.0.1:9999"}).status_code == 403
        assert (
            client.get("/api/roots", headers=[("Host", "127.0.0.1:8765"), ("Host", "untrusted")]).status_code
            == 403
        )
        for headers in (
            {"Origin": ORIGIN},
            {"X-PCI-Token": token},
            {"Origin": "http://untrusted.example", "X-PCI-Token": token},
            {"Origin": "http://localhost:8765", "X-PCI-Token": token},
            {"Origin": ORIGIN, "X-PCI-Token": "bad"},
        ):
            response = client.post("/api/scan", json={"paths": [str(tmp_path)]}, headers=headers)
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "forbidden"
        assert client.post("/api/jobs/unknown/cancel").status_code == 403
        assert not path.exists()
    with TestClient(create_app(db_path=path), base_url=ORIGIN) as other:
        assert _token(other) != token


def test_empty_index_and_static(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    with TestClient(create_app(db_path=path), base_url=ORIGIN) as client:
        assert client.get("/api/roots").json()["findings"] == []
        assert client.get("/api/analyzers/junk").json()["summary"]["items"] == 0
        assert client.get("/api/treemap").json() == []
        assert client.get("/css/app.css").status_code == 200
        assert client.get("/js/app.js").status_code == 200
        assert client.get("/vendor/echarts.min.js").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        assert not path.exists()


def test_reports_and_validation(index: Path, fake_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        scanner,
        "local_drives",
        lambda: [
            {"path": str(fake_tree), "label": "Тест", "filesystem": "NTFS", "total": 1000, "free": 500},
            {"path": "X:\\", "label": "Другой", "filesystem": "NTFS", "total": 2000, "free": 1000},
        ],
    )
    with TestClient(create_app(db_path=index), base_url=ORIGIN) as client:
        assert client.get("/api/roots").json()["findings"][0]["size"] == 270
        drives = client.get("/api/drives").json()
        assert [(drive["label"], drive["scanned"]) for drive in drives] == [("Тест", True), ("Другой", False)]
        catalog = client.get("/api/analyzers").json()
        types = next(entry for entry in catalog if entry["id"] == "types")
        assert types["schema"]["properties"]["by"]["enum"] == ["category", "ext"]
        assert (
            client.get("/api/analyzers/types", params={"by": "ext"}).json()["findings"][0]["title"] == "bin"
        )
        assert client.get("/api/analyzers/types", params={"by": "invalid"}).status_code == 422
        assert client.get("/api/analyzers/types", params={"db_path": "other.db"}).status_code == 422
        assert client.get("/api/analyzers/unknown").json()["error"]["code"] == "not_found"
        assert client.get("/api/tree", params={"path": str(fake_tree)}).json()["summary"]["total_files"] == 6
        assert client.get("/api/folders/top").json()["findings"][0]["title"] == "Документы"
        assert client.get("/api/tree", params={"limit": 0}).json()["error"]["code"] == "invalid_request"
        assert client.get("/api/treemap", params={"depth": 3}).status_code == 422


def test_treemap_depth_and_other(fake_tree: Path, index: Path) -> None:
    large = fake_tree / "Large"
    small = fake_tree / "Small"
    (large / "Child").mkdir(parents=True)
    small.mkdir()
    (large / "Child" / "bytes.bin").write_bytes(b"x" * 10_000)
    (small / "bytes.bin").write_bytes(b"x" * 12)
    with closing(db.connect(index)) as con:
        scanner.scan(str(fake_tree), con)
    with TestClient(create_app(db_path=index), base_url=ORIGIN) as client:
        response = client.get("/api/treemap", params={"path": str(fake_tree), "depth": 2})
        assert response.status_code == 200
        chart = response.json()
        assert chart[0]["children"] == [
            {"name": "Child", "value": 10000, "path": str(large / "Child"), "children": []}
        ]
        other = chart[-1]
        assert other["name"] == "Прочее" and other["value"] > 12 and other["path"] is None
        assert sum(node["value"] for node in chart) == 10282
        shallow = client.get("/api/treemap", params={"path": str(fake_tree), "depth": 1}).json()
        assert not any(node["children"] for node in shallow)


def test_scan_and_job_status(tmp_path: Path, fake_tree: Path) -> None:
    path = tmp_path / "web.db"
    with TestClient(create_app(db_path=path), base_url=ORIGIN) as client:
        headers = {"Origin": ORIGIN, "X-PCI-Token": _token(client)}
        response = client.post("/api/scan", json={"paths": [str(fake_tree)]}, headers=headers)
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        result = _job(client, job_id)
        assert result["state"] == "completed"
        assert result["progress"]["files"] == 6
        assert result["results"][0]["root"] == str(fake_tree)
        assert client.get("/api/roots").json()["findings"][0]["path"] == str(fake_tree)
        assert client.post(f"/api/jobs/{job_id}/cancel", headers=headers).json() == {"cancelled": False}
        assert client.post("/api/jobs/unknown/cancel", headers=headers).status_code == 404


def test_open_requires_indexed_path_and_token(
    index: Path,
    fake_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launches: list[list[str]] = []
    monkeypatch.setattr(safety, "local_path_without_links", lambda path: path)
    monkeypatch.setattr(services, "subprocess", SimpleNamespace(Popen=launches.append))
    directory = fake_tree / "Документы"
    file = directory / "отчёт.txt"
    with TestClient(create_app(db_path=index), base_url=ORIGIN) as client:
        assert client.post("/api/open", json={"path": str(directory)}).status_code == 403
        headers = {"Origin": ORIGIN, "X-PCI-Token": _token(client)}
        assert (
            client.post("/api/open", json={"path": str(fake_tree / "missing")}, headers=headers).status_code
            == 404
        )
        for path in (directory, file):
            assert client.post("/api/open", json={"path": str(path)}, headers=headers).json() == {
                "opened": True
            }
        assert launches == [
            [str(config.EXPLORER_EXE), str(directory)],
            [str(config.EXPLORER_EXE), "/select,", str(file)],
        ]


def test_tree_and_treemap_show_large_files(fake_tree: Path, index: Path) -> None:
    (fake_tree / "large.bin").write_bytes(b"x" * 10_000)
    (fake_tree / "tiny.bin").write_bytes(b"x" * 10)
    with closing(db.connect(index)) as con:
        scanner.scan(str(fake_tree), con)
    with TestClient(create_app(db_path=index), base_url=ORIGIN) as client:
        params = {"path": str(fake_tree), "files": "true", "limit": 1}
        report = client.get("/api/tree", params=params).json()
        files = [item for item in report["findings"] if item["kind"] == "file"]
        assert [(item["title"], item["size"]) for item in files] == [("large.bin", 10_000)]
        assert report["summary"]["total_size"] == 10280
        assert report["notes"]
        chart = client.get("/api/treemap", params=params).json()
        assert [node["name"] for node in chart if node.get("kind") == "file"] == ["large.bin"]
        assert sum(node["value"] for node in chart) == 10280
        documents = next(node for node in chart if node["name"] == "Документы")
        assert {child["name"] for child in documents["children"]} == {"копия.bin", "отчёт.txt"}
