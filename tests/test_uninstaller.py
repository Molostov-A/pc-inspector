"""Подтверждённый запуск безопасно передаётся Windows (F2.2, N8)."""

from __future__ import annotations

from pathlib import Path

import pytest
import win32api
from pywintypes import error as WindowsError

from pcinspector import config
from pcinspector.actions import uninstaller


@pytest.fixture(autouse=True)
def no_processes(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls = []
    monkeypatch.setattr(win32api, "ShellExecute", lambda *args: calls.append(args))
    return calls


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("  ", "недействительна"),
        ("msiexec /x bad", "Нестандартная"),
        (r"C:\Windows\System32\cmd.exe /c remove.bat", "требует оболочку"),
        (r"C:\Tests\uninstall.bat", "требует оболочку"),
        ("uninstall.exe", "требует оболочку"),
        (r"C:\Tests\file.exe:stream.exe", "требует оболочку"),
        (r"C:\Tests\uninstall.exe /VERYSILENT", "тихое"),
        (r"C:\Tests\uninstall.exe --mode unattended", "тихое"),
    ],
)
def test_invalid_or_silent_command_rejected(command: str, message: str, no_processes: list[tuple]) -> None:
    with pytest.raises(ValueError, match=message):
        uninstaller.launch(command)
    assert not no_processes


def test_valid_interactive_msi_uses_system_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_processes: list[tuple],
) -> None:
    system = tmp_path / "System32"
    system.mkdir()
    exe = system / "msiexec.exe"
    exe.touch()
    monkeypatch.setattr(config, "WINDOWS_DIR", tmp_path)
    product = "{0A000000-0000-0000-0000-000000000000}"
    uninstaller.launch(f"MsiExec.exe /I{product}")
    assert no_processes == [(0, "open", str(exe), f"/x {product}", str(system), 1)]


def test_only_file_is_launched_and_windows_error_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_processes: list[tuple],
) -> None:
    directory = tmp_path / "directory.exe"
    directory.mkdir()
    with pytest.raises(ValueError, match="Исполняемый файл"):
        uninstaller.launch(f'"{directory}"')
    assert not no_processes
    exe = tmp_path / "uninstaller with spaces.exe"
    exe.touch()

    def denied(*_args: object) -> None:
        raise WindowsError(5, "ShellExecute", "Нет доступа")

    monkeypatch.setattr(win32api, "ShellExecute", denied)
    with pytest.raises(ValueError, match="Windows не смогла"):
        uninstaller.launch(f'"{exe}" /remove')
