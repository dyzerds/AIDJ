@echo off
rem Double-click this file to start AIDJ. The first start installs everything it needs.
cd /d "%~dp0"

set PY=py -3
%PY% --version >nul 2>&1 || set PY=python
%PY% --version >nul 2>&1 || goto nopython

if not exist .venv\Scripts\python.exe (
  echo Setting up AIDJ for the first time. This takes a minute or two...
  %PY% -m venv .venv || goto nopython
)
echo Checking requirements...
.venv\Scripts\python -m pip install --disable-pip-version-check -q -r requirements.txt || goto failed
rem YouTube changes often, so always use the newest downloader.
.venv\Scripts\python -m pip install --disable-pip-version-check -q -U "yt-dlp[default]"

.venv\Scripts\python app.py
pause
exit /b

:nopython
echo.
echo Python was not found. Install it from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" during the install), then double-click start.bat again.
pause
exit /b 1

:failed
echo.
echo Installing the requirements failed. Check your internet connection and try again.
pause
exit /b 1
