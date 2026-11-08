"""Запуск штатного деинсталлятора после подтверждения в UI."""

from __future__ import annotations

import logging
import ntpath
import re
import subprocess
from pathlib import Path

from .. import config
from ..core.safety import local_path_without_links

logger = logging.getLogger(__name__)
_WRAPPERS = {
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "wscript.exe",
    "cscript.exe",
    "rundll32.exe",
    "mshta.exe",
    "regsvr32.exe",
    "explorer.exe",
    "wsl.exe",
    "bash.exe",
}
_SILENT = re.compile(
    r"^(?:[/\-]{1,2})(?:s|q[nbrf]?[!+\-]?|silent|verysilent|quiet|passive|unattended|"
    r"noconfirm|suppressmsgboxes)(?:[=:].*)?$",
    re.IGNORECASE,
)
_PRODUCT = r"\{[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\}"


def _argv(command: str) -> list[str]:
    from win32api import CommandLineToArgv

    if not command.strip() or any(char in command for char in "\0\r\n"):
        raise ValueError("Команда деинсталлятора недействительна")
    return list(CommandLineToArgv(config.expand_windows_vars(command.strip())))


def launch(command: str) -> None:
    from pywintypes import error as WindowsError
    from win32api import ShellExecute

    executable, *arguments = _argv(command)
    name = ntpath.basename(executable).casefold()
    if any(_SILENT.fullmatch(arg) or arg.casefold() in {"unattended", "silent"} for arg in arguments):
        raise ValueError("Команда запускает тихое удаление. Откройте «Программы и компоненты» Windows")
    if name in ("msiexec", "msiexec.exe"):
        product = re.fullmatch(r"[/\-][ix]\s*(" + _PRODUCT + r")", " ".join(arguments), re.IGNORECASE)
        if product is None:
            raise ValueError("Нестандартная команда MSI. Откройте «Программы и компоненты» Windows")
        executable = str(config.WINDOWS_DIR / "System32" / "msiexec.exe")
        arguments = ["/x", product[1]]
    elif (
        name in _WRAPPERS
        or not name.endswith(".exe")
        or not ntpath.isabs(executable)
        or ":" in ntpath.splitdrive(executable)[1]
    ):
        raise ValueError(
            "Команда требует оболочку или не указывает локальный EXE. Откройте «Программы и компоненты» Windows"
        )

    path = Path(local_path_without_links(executable))
    if not path.is_file():
        raise ValueError("Исполняемый файл деинсталлятора недоступен")
    try:
        ShellExecute(0, "open", str(path), subprocess.list2cmdline(arguments), str(path.parent), 1)
    except WindowsError as ex:
        raise ValueError(f"Windows не смогла открыть деинсталлятор: {ex}") from ex
    logger.info("Передано управление штатному деинсталлятору: %s", path)
