@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        python -m venv .venv
    )
    if errorlevel 1 goto error
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto error

echo Open http://127.0.0.1:8765/ in your browser. Keep this window open.
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto error
exit /b 0

:error
echo Startup failed. Check that Python and pip are installed and try again.
pause
exit /b 1
