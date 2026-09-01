@echo off
rem One-click setup: find Python 3.11 -> create .venv -> install locked deps -> verify
rem Usage: clone repo, double-click this file (or run setup.bat)
rem NOTE: keep this file ASCII-only to avoid codepage issues on other machines.
setlocal EnableDelayedExpansion

echo ===== fuck_DXSTJ environment setup =====

rem ---- 1. locate Python 3.11 (py launcher first, then PATH python) ----
set "PYCMD="
py -3.11 --version >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3.11"

if not defined PYCMD (
    python -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,11) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYCMD=python"
)

if not defined PYCMD (
    echo [ERROR] Python 3.11 not found.
    echo Install it from https://www.python.org/downloads/release/python-3119/
    echo and check "Add to PATH" during installation.
    pause & exit /b 1
)
echo [1/4] Using Python: !PYCMD!

rem ---- 2. create venv ----
if exist ".venv\Scripts\python.exe" (
    echo [2/4] .venv already exists, skip
) else (
    !PYCMD! -m venv .venv
    if errorlevel 1 (echo [ERROR] venv creation failed & pause & exit /b 1)
    echo [2/4] Created .venv
)

rem ---- 3. install locked dependencies ----
echo [3/4] Installing locked dependencies (may take a few minutes)...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Install failed. For CN mainland network, try mirror:
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    pause & exit /b 1
)

rem ---- 4. verify ----
echo [4/4] Verifying (running all tests)...
.venv\Scripts\python.exe -m pytest tests/ -q
if errorlevel 1 (echo [ERROR] Tests failed & pause & exit /b 1)
.venv\Scripts\python.exe -c "import PySide6, rapidocr_onnxruntime, win32gui; print('core imports OK')"

echo.
echo ===== Setup complete =====
echo Run app  : .venv\Scripts\python.exe main.py
echo Run tests: .venv\Scripts\python.exe -m pytest tests/ -q
echo First run: copy config.example.yaml to config.yaml and fill in your API key
pause
