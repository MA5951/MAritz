@echo off
setlocal
REM Change to App Data directory
pushd "%~dp0\App Data"

REM Prefer a local venv pythonw
if exist ".venv\Scripts\pythonw.exe" (
    set "PYTHON=.venv\Scripts\pythonw.exe"
    set "PIP=.venv\Scripts\pip.exe"
) else if exist "venv\Scripts\pythonw.exe" (
    set "PYTHON=venv\Scripts\pythonw.exe"
    set "PIP=venv\Scripts\pip.exe"
)

REM Fallback to system pythonw and pip
if not defined PYTHON where pythonw >nul 2>&1 && set "PYTHON=pythonw"
if not defined PIP where pip >nul 2>&1 && set "PIP=pip"

if not defined PYTHON (
    echo ERROR: Could not find pythonw.
    pause
    exit /b 1
)

if not defined PIP (
    echo ERROR: Could not find pip.
    pause
    exit /b 1
)

REM Install PySide6 if missing
%PIP% show PySide6 >nul 2>&1
if errorlevel 1 (
    echo Installing PySide6...
    %PIP% install PySide6
)

REM Install robotpy if missing
%PIP% show robotpy >nul 2>&1
if errorlevel 1 (
    echo Installing robotpy...
    %PIP% install robotpy
)

REM Install robotpy if missing
%PIP% show keyboard >nul 2>&1
if errorlevel 1 (
    echo Installing keyboard...
    %PIP% install keyboard
)

REM Launch without console
start "" "%PYTHON%" main.py

popd
endlocal
