"""Сбор программ и признаков использования на подставном реестре (F2.2, F2.3)."""

from __future__ import annotations

import codecs
import json
import os
import struct
import time
import winreg
from concurrent.futures import CancelledError
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Self

import pytest
from fastapi.testclient import TestClient
from test_api import ORIGIN, _token

from pcinspector import cli, config, services
from pcinspector.api.app import create_app
from pcinspector.collectors import apps, usage
from pcinspector.core import db, jobs, scanner


@dataclass
class _Handle:
    hive: int
    view: int
    path: str

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        pass


def _end() -> OSError:
    error = OSError("Конец перечисления")
    error.winerror = 259
    return error


def _assist(path: str, stamp: int, count: int) -> tuple[str, bytes, int]:
    blob = bytearray(72)
    struct.pack_into("<I", blob, 4, count)
    struct.pack_into("<Q", blob, 60, usage._FILETIME_EPOCH + stamp * 10_000_000)
    return codecs.encode(path, "rot_13"), bytes(blob), winreg.REG_BINARY


@pytest.fixture
def inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "tree"
    exe = {}
    now = int(time.time())
    old = now - 400 * 86_400
    for key in ("editor", "player", "tool"):
        directory = root / "software" / key
        directory.mkdir(parents=True)
        exe[key] = directory / f"{key}.exe"
        exe[key].write_bytes(key.encode() * 4)
        os.utime(exe[key], (old, old))
    for area, folder in (
        ("Roaming", "Modern Editors"),
        ("Local", "Studio Group LLC"),
        ("Local", "Microsoft"),
        ("Roaming", "Orphaned"),
        ("ProgramData", "OldCorp"),
        ("Roaming", "Orphaned/nested"),
    ):
        directory = root / area / folder
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "data.bin").write_bytes(b"test")
    for key, value in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local"), ("PROGRAMDATA", "ProgramData")):
        monkeypatch.setenv(key, str(root / value))
    system = tmp_path / "win"
    prefetch = system / "Prefetch"
    prefetch.mkdir(parents=True)
    recent = prefetch / "EDITOR.EXE-1234ABCD.pf"
    recent.touch()
    os.utime(recent, (now - 100, now - 100))
    (prefetch / "ignore.txt").touch()
    monkeypatch.setattr(config, "WINDOWS_DIR", system)

    commands = {
        "editor": f'"{exe["editor"]}" /remove',
        "player": f'"{exe["player"]}" /remove',
    }
    records = {
        "hklm": {
            "Editor": {
                "DisplayName": "Modern Editor",
                "Publisher": "Studio Group Inc",
                "DisplayVersion": "1.0",
                "InstallLocation": str(exe["editor"].parent),
                "EstimatedSize": 123,
                "UninstallString": commands["editor"],
                "QuietUninstallString": commands["editor"] + " /S",
            },
            "Patch": {"DisplayName": "Update", "ReleaseType": "Security Update"},
            "System": {"DisplayName": "System component", "SystemComponent": 1},
        },
        "hklm_wow": {
            "Player": {
                "DisplayName": "Old Player",
                "DisplayIcon": f'"{exe["player"]}",0',
                "EstimatedSize": 10,
                "UninstallString": commands["player"],
            },
            "Child": {"DisplayName": "Child package", "ParentKeyName": "Player"},
        },
        "hkcu": {
            "Tool": {
                "DisplayName": "Quiet Tool",
                "InstallLocation": str(exe["tool"].parent),
                "EstimatedSize": 1,
            },
            "Unknown": {"DisplayName": "No Evidence", "EstimatedSize": "bad"},
        },
    }
    blocked: set[str] = set()
    lookup = {(hive, view): name for name, (hive, view) in apps.REGISTRY_VIEWS.items()}
    guid = "{00000000-0000-0000-0000-000000000001}"
    monkeypatch.setattr(usage, "_known_folder", lambda _value: str(exe["player"].parent))
    signals = [
        _assist(guid + "\\player.exe", old, 19),
        _assist(str(exe["editor"]), old, 1),
        ("junk", "not binary", winreg.REG_SZ),
    ]

    def open_key(
        key: _Handle | int, subkey: str, _reserved: int = 0, access: int = winreg.KEY_READ
    ) -> _Handle:
        if isinstance(key, _Handle):
            return _Handle(key.hive, key.view, key.path + "\\" + subkey)
        view = access & (winreg.KEY_WOW64_32KEY | winreg.KEY_WOW64_64KEY)
        source = lookup[(key, view)]
        if source in blocked and subkey.startswith(apps.UNINSTALL_KEY):
            raise PermissionError("Нет доступа")
        return _Handle(key, view, subkey)

    def enum_key(key: _Handle, index: int) -> str:
        values = [guid] if key.path == usage.USER_ASSIST else list(records[lookup[(key.hive, key.view)]])
        if index >= len(values):
            raise _end()
        return values[index]

    def query_value(key: _Handle, name: str) -> tuple[Any, int]:
        data = records[lookup[(key.hive, key.view)]].get(key.path.rsplit("\\", 1)[-1], {})
        if name not in data:
            raise FileNotFoundError(name)
        return data[name], winreg.REG_SZ

    def enum_value(_key: _Handle, index: int) -> tuple[str, bytes, int]:
        if index >= len(signals):
            raise _end()
        return signals[index]

    monkeypatch.setattr(winreg, "OpenKey", open_key)
    monkeypatch.setattr(winreg, "EnumKey", enum_key)
    monkeypatch.setattr(winreg, "QueryValueEx", query_value)
    monkeypatch.setattr(winreg, "EnumValue", enum_value)
    index = tmp_path / "index.db"
    with closing(db.connect(index)) as con:
        scanner.scan(str(root), con)
    return {
        "index": index,
        "exe": exe,
        "now": now,
        "old": old,
        "blocked": blocked,
        "records": records,
        "prefetch": prefetch,
    }


def _wait(job_id: str) -> jobs.JobStatus:
    deadline = time.monotonic() + 8
    while (job := services.job_status(job_id)).state in ("queued", "running"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert job.state == "completed", job.errors
    return job


def test_sources_usage_and_leftovers(inventory: dict[str, Any]) -> None:
    index = inventory["index"]
    assert "Сначала соберите" in services.run_analyzer("unused_apps", db_path=index).notes[0]
    job = _wait(services.collect_apps(db_path=index))
    assert job.apps_result == {"count": 4, "notes": []}
    all_apps = {
        item.title: item for item in services.run_analyzer("unused_apps", days=0, db_path=index).findings
    }
    assert set(all_apps) == {"Modern Editor", "Old Player", "Quiet Tool", "No Evidence"}
    assert all_apps["Modern Editor"].meta["last_run"] == inventory["now"] - 100
    assert all_apps["Modern Editor"].meta["confidence"] == "high"
    assert all_apps["Modern Editor"].size == len(b"editor" * 4)
    assert all_apps["Old Player"].meta["run_count"] == 19
    assert all_apps["Old Player"].meta["confidence"] == "medium"
    assert all_apps["Old Player"].size == 10 * 1024
    assert all_apps["Quiet Tool"].meta["confidence"] == "low"
    assert all_apps["Quiet Tool"].meta["last_run"] == inventory["old"]
    assert all_apps["No Evidence"].meta["last_run"] is None
    assert {item.title for item in services.run_analyzer("unused_apps", db_path=index).findings} == {
        "Old Player",
        "Quiet Tool",
        "No Evidence",
    }
    leftovers = services.run_analyzer("app_leftovers", db_path=index)
    assert {item.title: item.safety for item in leftovers.findings} == {
        "Orphaned": "review",
        "OldCorp": "keep",
    }


def test_prefetch_unavailable_and_partial_inventory_preserved(
    inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    index = inventory["index"]
    _wait(services.collect_apps(db_path=index))

    def denied(_path: Path) -> None:
        raise PermissionError("Prefetch недоступен")

    monkeypatch.setattr(usage, "os", SimpleNamespace(scandir=denied))
    inventory["blocked"].add("hklm_wow")
    inventory["records"]["hkcu"].pop("Unknown")
    status = _wait(services.collect_apps(db_path=index))
    assert status.apps_result is not None and status.apps_result["count"] == 3
    assert any("Prefetch недоступен" in note for note in status.apps_result["notes"])
    items = {
        item.title: item for item in services.run_analyzer("unused_apps", days=0, db_path=index).findings
    }
    assert items["Modern Editor"].meta["confidence"] == "medium"
    assert "Old Player" in items and "No Evidence" not in items
    report = services.run_analyzer("app_leftovers", db_path=index)
    assert not report.findings and "неполный" in report.notes[0]


def test_collection_api_cli_and_uninstall_guard(
    inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    index = inventory["index"]
    calls = []
    import win32api

    monkeypatch.setattr(win32api, "ShellExecute", lambda *args: calls.append(args))
    with TestClient(create_app(db_path=index), base_url=ORIGIN) as client:
        assert client.post("/api/apps/collect").status_code == 403
        headers = {"Origin": ORIGIN, "X-PCI-Token": _token(client)}
        started = client.post("/api/apps/collect", headers=headers)
        assert started.status_code == 202
        _wait(started.json()["job_id"])
        report = client.get("/api/analyzers/unused_apps", params={"days": 0}).json()
        app_id = next(item["meta"]["app_id"] for item in report["findings"] if item["title"] == "Old Player")
        endpoint = f"/api/apps/{app_id}/uninstall"
        assert client.post(endpoint, json={"confirmed": True}).status_code == 403
        assert client.post(endpoint, json={}, headers=headers).status_code == 422
        assert not calls
        assert client.post(endpoint, json={"confirmed": True}, headers=headers).json() == {"opened": True}
        assert calls[0][1:4] == ("open", str(inventory["exe"]["player"]), "/remove")
        inventory["records"]["hklm_wow"]["Player"]["UninstallString"] += " /other"
        assert client.post(endpoint, json={"confirmed": True}, headers=headers).status_code == 422
        assert len(calls) == 1
    assert cli.main(["apps", "--db", str(index), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["apps_result"]["count"] == 4


def test_userassist_ignores_invalid_binary() -> None:
    assert usage.decode_user_assist("a", b"short", {}) is None
    assert usage.decode_user_assist("a", bytes(72), {}) is None
    name, value, _ = _assist(r"Z:\test\file.lnk", 1700000000, 2)
    assert usage.decode_user_assist(name, value, {}) is None
    assert usage._known_folder("invalid") is None
    assert apps._display_icon(r'"X:\Folder With Space\App.exe,0"') == r"X:\Folder With Space\App.exe"
    with pytest.raises(CancelledError):
        usage._check(_cancelled())


def _cancelled() -> Event:
    cancel = Event()
    cancel.set()
    return cancel


def test_ambiguous_prefetch_basename_does_not_override_userassist() -> None:
    first, second = r"X:\first\update.exe", r"X:\second\update.exe"
    prefetch = {"update.exe": usage.Usage("update.exe", None, 200, None, "prefetch")}
    user_assist = {first.casefold(): usage.Usage("update.exe", first, 100, 3, "userassist")}
    names = {"update.exe": {first.casefold(), second.casefold()}}
    assert usage._signals({first: None}, names, prefetch, user_assist) == list(user_assist.values())
    assert not usage._signals({second: None}, names, prefetch, user_assist)
