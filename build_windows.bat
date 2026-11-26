@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title GitArchive - Windows build

where py >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python launcher was not found. Install Python 3.11+.
    exit /b 1
)
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.11 or newer is required.
    exit /b 1
)
where cargo >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Rust Cargo was not found. Install Rust stable from https://rustup.rs/
    exit /b 1
)
where git >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Git for Windows was not found. Install it from https://git-scm.com/download/win/
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt -r requirements-build.txt
if errorlevel 1 exit /b 1

echo [BUILD] Building the Rust engine...
cargo build --release --jobs 1 --manifest-path rust-core\Cargo.toml
if errorlevel 1 goto :failed

echo [BUILD] Packaging the Windows application...
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --windowed --onedir --name GitArchive run_gitarchive.py
if errorlevel 1 goto :failed

if not exist "rust-core\target\release\gitarchive-core.exe" (
    echo [ERROR] Rust engine executable is missing.
    goto :failed
)
if not exist "dist\GitArchive\rust-core" mkdir "dist\GitArchive\rust-core"
copy /Y "rust-core\target\release\gitarchive-core.exe" "dist\GitArchive\rust-core\gitarchive-core.exe" >nul
if errorlevel 1 goto :failed

echo [SUCCESS] Portable application is ready: dist\GitArchive\GitArchive.exe

set "ISCC="
where ISCC.exe >nul 2>nul
if not errorlevel 1 set "ISCC=ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 7\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 7\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if not defined ISCC (
    echo [INFO] Inno Setup was not found. Portable build is complete.
    echo Install Inno Setup 7, then run this script again to create Setup.exe.
    exit /b 0
)

echo [BUILD] Creating the installer...
"%ISCC%" installer\GitArchive.iss
if errorlevel 1 goto :failed
echo [SUCCESS] Installer: dist-installer\GitArchive-Setup.exe
exit /b 0

:failed
echo [ERROR] Build failed. Review the first error above.
exit /b 1
