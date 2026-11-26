@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title GitArchive - Windows launcher

where py >nul 2>nul
if errorlevel 1 (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python 3.11 or newer was not found.
        echo Install Python from https://www.python.org/downloads/windows/
        echo Enable the Python launcher or add Python to PATH, then run start.bat again.
        pause
        exit /b 1
    )
    set "PYTHON=python"
) else (
    set "PYTHON=py -3"
)

%PYTHON% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
    echo [ERROR] GitArchive requires Python 3.11 or newer.
    pause
    exit /b 1
)

where cargo >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Rust Cargo was not found.
    echo Install the stable Rust toolchain from https://rustup.rs/ and restart this terminal.
    pause
    exit /b 1
)

where git >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Git was not found.
    echo Install Git for Windows from https://git-scm.com/download/win/ and restart this terminal.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [SETUP] Creating Python virtual environment...
    %PYTHON% -m venv .venv
    if errorlevel 1 goto :failed
)

echo [SETUP] Installing Python dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo [BUILD] Building the Rust archive engine...
cargo build --release --manifest-path rust-core\Cargo.toml
if errorlevel 1 goto :failed

echo [RUN] Starting GitArchive...
".venv\Scripts\python.exe" -m gitarchive
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" (
    echo [ERROR] GitArchive exited with code %APP_EXIT%.
    pause
)
exit /b %APP_EXIT%

:failed
echo.
echo [ERROR] Setup or build failed. Read the message above and try again.
pause
exit /b 1
