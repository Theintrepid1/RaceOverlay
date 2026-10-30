@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Start Race Analysis from source (run setup.bat once first).
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\pythonw.exe" call "%~dp0setup.bat" || exit /b 1
start "" ".venv\Scripts\pythonw.exe" packaging\windows\run_from_source.py --analysis %*
