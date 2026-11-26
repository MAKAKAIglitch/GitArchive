@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title GitArchive - Windows launcher

rem Prefer the Python launcher, but fall back to python.exe if its default
rem interpreter is older than 3.11.
set "PYTHON="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYTHON=py -3"
)

if not defined PYTHON (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
        if not errorlevel 1 set "PYTHON=python"
    )
)

if not defined PYTHON (
    echo [ERROR] Python 3.11 or newer was not found.
    echo Install Python from https://www.python.org/downloads/windows/
    echo Make sure the Python launcher or python.exe is available in PATH.
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

".venv\Scripts\python.exe" -c "import PySide6; v=tuple(map(int,PySide6.__version__.split('.')[:2])); raise SystemExit(0 if (6,8) <= v < (7,0) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [SETUP] Installing or updating Python dependencies...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :failed
) else (
    echo [SETUP] PySide6 is already installed.
)

echo [BUILD] Building the Rust archive engine...
cargo build --release --manifest-path rust-core\Cargo.toml
if errorlevel 1 (
    echo [TIP] If Cargo reports that link.exe or the MSVC linker is missing,
    echo install Visual Studio Build Tools with the C++ build tools workload.
    echo https://visualstudio.microsoft.com/visual-cpp-build-tools/
    goto :failed
)

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
