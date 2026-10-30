@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem One-time setup for running Race Overlay from source on Windows:
rem makes .venv, installs the Python packages, downloads ffmpeg + libmpv.
rem Safe to run again (it skips what's already done). The installer, when it
rem exists, makes all of this unnecessary.
setlocal
cd /d "%~dp0.."

set "PY="
for %%V in (3.13 3.12 3.11) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
    )
)
if not defined PY (
    python -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo.
    echo Python 3.11, 3.12 or 3.13 is needed. Install it with:
    echo     winget install Python.Python.3.13
    echo or from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^),
    echo then run this again.
    echo.
    pause
    exit /b 1
)
echo Using %PY%

if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv ...
    %PY% -m venv .venv || goto :fail
)
echo Installing Python packages ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip || goto :fail
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt py7zr || goto :fail
".venv\Scripts\python.exe" packaging\windows\fetch_deps.py || goto :fail

echo.
echo All set. Start the apps with windows\RaceOverlay.bat or windows\RaceAnalysis.bat
echo.
pause
exit /b 0

:fail
echo.
echo Setup failed (see the messages above).
pause
exit /b 1
