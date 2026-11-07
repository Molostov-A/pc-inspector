@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
pushd "%~dp0" || exit /b 1

if not exist ".venv\Scripts\python.exe" (
    echo Не найдено виртуальное окружение .venv.
    echo Выполните из папки проекта:
    echo   python -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -e ".[dev]"
    pause
    popd
    exit /b 1
)

".venv\Scripts\python.exe" -m pcinspector serve --open %*
set "pci_exit=%errorlevel%"
if not "%pci_exit%"=="0" pause
popd
exit /b %pci_exit%
