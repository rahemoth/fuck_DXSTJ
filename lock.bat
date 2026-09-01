@echo off
rem Maintainer only: regenerate locked requirements.txt after upgrading deps.
rem Flow: change deps -> run this -> run tests -> commit
rem NOTE: keep this file ASCII-only to avoid codepage issues.
setlocal

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run setup.bat first.
    pause & exit /b 1
)

echo Current Python version:
.venv\Scripts\python.exe --version

echo Regenerating requirements.txt ...
.venv\Scripts\python.exe -m pip freeze > requirements.txt

echo.
echo Now run tests to confirm: .venv\Scripts\python.exe -m pytest tests/ -q
echo Commit requirements.txt only after tests pass.
pause
