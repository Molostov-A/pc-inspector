"""Отказ от отправки объектов, которые Windows удалила бы мимо Корзины (F3.1, §9.1)."""

from __future__ import annotations

import shutil
import winreg
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest

from pcinspector.actions import executor, recycle_bin


class _Key:
    def __init__(self, values: dict[str, int]) -> None:
        self.values = values

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _registry(
    monkeypatch: pytest.MonkeyPatch,
    volume: dict[str, int] | None,
    machine: dict[str, int] | None = None,
    user: dict[str, int] | None = None,
) -> None:
    def open_key(hive: int, path: str) -> _Key:
        if path == recycle_bin.POLICY_KEY:
            values = machine if hive == winreg.HKEY_LOCAL_MACHINE else user
        else:
            values = volume
        if values is None:
            raise FileNotFoundError(path)
        return _Key(values)

    def query(key: _Key, name: str) -> tuple[int, int]:
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name], winreg.REG_DWORD

    monkeypatch.setattr(recycle_bin.winreg, "OpenKey", open_key)
    monkeypatch.setattr(recycle_bin.winreg, "QueryValueEx", query)
    monkeypatch.setattr(recycle_bin, "_volume_guid", lambda _anchor: "{guid}")


def test_quota_reads_volume_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _registry(monkeypatch, {"NukeOnDelete": 0, "MaxCapacity": 2})
    assert recycle_bin.quota("C:\\") == 2 * 1024**2


def test_quota_applies_size_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    _registry(monkeypatch, {"NukeOnDelete": 0, "MaxCapacity": 1024}, user={"RecycleBinSize": 1})
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(total=100 * 1024**2))
    assert recycle_bin.quota("C:\\") == 1024**2


@pytest.mark.parametrize(
    ("volume", "machine", "message"),
    [
        ({"NukeOnDelete": 1, "MaxCapacity": 9}, None, "отключена"),
        ({"NukeOnDelete": 0, "MaxCapacity": 9}, {"NoRecycleFiles": 1}, "Политика"),
        (None, None, "Не удалось проверить"),
        ({"NukeOnDelete": 0}, None, "Не удалось проверить"),
    ],
)
def test_quota_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    volume: dict[str, int] | None,
    machine: dict[str, int] | None,
    message: str,
) -> None:
    _registry(monkeypatch, volume, machine)
    with pytest.raises(ValueError, match=message):
        recycle_bin.quota("C:\\")


def test_real_volume_guid(tmp_path: Path) -> None:
    guid = recycle_bin._volume_guid(tmp_path.anchor)
    assert guid.startswith("{") and guid.endswith("}")


def test_unknown_volume_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake: Any = SimpleNamespace(kernel32=SimpleNamespace(GetVolumeNameForVolumeMountPointW=lambda *_args: 0))
    monkeypatch.setattr(recycle_bin.ctypes, "windll", fake)
    with pytest.raises(OSError, match="том"):
        recycle_bin._volume_guid("Q:\\")


def test_rejects_object_larger_than_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "папка"
    folder.mkdir()
    (folder / "a.bin").write_bytes(b"a" * 100)
    monkeypatch.setattr(recycle_bin, "quota", lambda _anchor: 100)
    with pytest.raises(ValueError, match="не помещается"):
        recycle_bin.ensure_recyclable(str(folder))
    monkeypatch.setattr(recycle_bin, "quota", lambda _anchor: 1000)
    recycle_bin.ensure_recyclable(str(folder))


def test_rejects_long_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "file").write_bytes(b"x")
    monkeypatch.setattr(recycle_bin, "MAX_SHELL_PATH", 3)
    with pytest.raises(ValueError, match="длинный"):
        recycle_bin.ensure_recyclable(str(tmp_path / "file"))


def test_wrapper_does_not_call_shell_after_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from send2trash.win import modern

    calls: list[str] = []

    def rejected(_path: str) -> None:
        raise ValueError("не помещается")

    monkeypatch.setattr(modern, "send2trash", calls.append)
    monkeypatch.setattr(recycle_bin, "ensure_recyclable", rejected)
    with pytest.raises(ValueError, match="не помещается"):
        executor.send2trash(str(tmp_path / "file"))
    assert not calls
