"""Проверки защищённых зон и путей действий (F1.4, F3.2, N8)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from pcinspector.core import safety


@pytest.fixture(autouse=True)
def protected_locations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safety, "PROJECT_ROOT", Path(r"R:\Projects\pc-inspector"))
    monkeypatch.setattr(safety, "DATA_DIR", Path(r"S:\PCI-data"))
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Tester")


@pytest.mark.parametrize(
    "path",
    [
        r"c:/wiNdows",
        r"C:\Windows\Temp\cache.bin",
        r"C:\Other\..\Program Files\x",
        r"D:\$RECYCLE.BIN",
        r"C:\Windows. \file",
        r"\\?\C:\Recovery\file",
        r"\\?\UNC\server\share\Boot\file",
    ],
)
def test_system_zones(path: str) -> None:
    assert safety.is_protected(path)


@pytest.mark.parametrize("path", ["C:\\", r"C:\Work\Windows\file", r"Windows\Temp", r"C:\WindowsBackup"])
def test_non_system_paths(path: str) -> None:
    assert not safety.is_protected(path)


@pytest.mark.parametrize(
    "path",
    [
        "C:\\",
        "relative.txt",
        r"C:relative.txt",
        r"1:\file",
        r"\\server\share\file",
        r"\\?\GLOBALROOT\Device\file",
        r"C:\Windows\Temp\file",
        r"c:/users/tester",
        r"C:\Users",
        r"C:\Users\Tester\DoWnLoads",
        r"r:\projects\pc-inspector\data",
        r"R:\Projects",
        r"S:\PCI-data\file",
        r"C:\Work\file:stream",
        r"C:\Work\file.",
        r"C:\Work\CON.txt",
        "C:\\Work\\file\0",
    ],
)
def test_rejects_unsafe_action_paths(path: str) -> None:
    with pytest.raises(ValueError):
        safety.validate_action_path(path)


def test_file_inside_profile_directory_is_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    item = tmp_path / "Downloads" / "разрешённый файл"
    item.parent.mkdir()
    item.touch()
    safety.validate_action_path(item)


@pytest.mark.skipif(os.name != "nt", reason="Проверка разрешения пути Windows")
def test_resolved_target_is_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "protected"
    alias = tmp_path / "alias"
    target.mkdir()
    resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda path: target if path == alias else resolve(path))
    monkeypatch.setattr(safety, "DATA_DIR", target)
    with pytest.raises(ValueError, match="пересекается"):
        safety.validate_action_path(alias)


def test_resolution_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_path: Path) -> Path:
        raise OSError("Ошибка разрешения пути")

    monkeypatch.setattr(Path, "resolve", fail)
    with pytest.raises(ValueError, match="фактический"):
        safety.validate_action_path(tmp_path / "alias")


def test_lexical_validation_without_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USERPROFILE")
    monkeypatch.setattr(safety, "os", SimpleNamespace(name="posix", environ=os.environ, fspath=os.fspath))
    monkeypatch.setattr(Path, "home", lambda: Path(r"C:\Users\Tester"))
    safety.validate_action_path(r"C:\Users\Tester\Documents\file")


@pytest.mark.skipif(os.name != "nt", reason="Проверка локального просмотра Windows")
def test_local_view_path(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes

    inspected = []

    def stat_path(path: Path) -> SimpleNamespace:
        inspected.append(str(path))
        return SimpleNamespace(st_file_attributes=0x400 if path.name == "junction" else 0)

    monkeypatch.setattr(ctypes.windll.kernel32, "GetDriveTypeW", lambda anchor: 3 if anchor == "R:\\" else 4)
    monkeypatch.setattr(safety, "os", SimpleNamespace(name="nt", fspath=os.fspath, lstat=stat_path))
    assert safety.local_path_without_links("R:/folder/file") == r"R:\folder\file"
    assert inspected == ["R:\\", r"R:\folder", r"R:\folder\file"]
    for path in ("relative", r"\\server\share\file", r"1:\file", r"S:\file"):
        with pytest.raises(ValueError, match="локальном диске"):
            safety.local_path_without_links(path)
    inspected.clear()
    with pytest.raises(ValueError, match="точку повторной обработки"):
        safety.local_path_without_links(r"R:\junction\file")
    assert inspected == ["R:\\", r"R:\junction"]


def test_local_view_requires_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safety, "os", SimpleNamespace(name="posix"))
    with pytest.raises(ValueError, match="только в Windows"):
        safety.local_path_without_links("/path")
