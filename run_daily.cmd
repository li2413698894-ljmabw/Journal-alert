@echo off
REM ---------------------------------------------------------------------------
REM  journal-alert daily runner (portable - no machine-specific paths)
REM
REM  Finds any usable Python 3 on this PC, then runs the pipeline.
REM  Register with Windows Task Scheduler (see README.md section 8.2).
REM
REM  No arguments -> scheduled mode: all output goes to logs\run-last.log
REM  Any argument -> interactive mode: output goes to the console
REM                  (e.g. run_daily.cmd --doctor / --check / --test-push)
REM
REM  Exit codes: 0 = success, 2 = no Python found, 3 = Python too old/incomplete.
REM ---------------------------------------------------------------------------
setlocal enabledelayedexpansion
chcp 65001 >nul

set "PROJECT=%~dp0"
set "PYEXE="
set "MARKER=%PROJECT%logs\runner.log"
set "RUNLOG=%PROJECT%logs\run-last.log"

if not exist "%PROJECT%logs" mkdir "%PROJECT%logs" >nul 2>&1

REM --- 1) The Windows Python launcher knows about every installed version. -----
for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (
    if not defined PYEXE (
        "%%I" -c "import sqlite3,urllib.request" >nul 2>&1 && set "PYEXE=%%I"
    )
)

REM --- 2) python.exe on PATH (the Microsoft Store stub fails the import test). -
if not defined PYEXE (
    for /f "delims=" %%I in ('where python.exe 2^>nul') do (
        if not defined PYEXE (
            "%%I" -c "import sqlite3,urllib.request" >nul 2>&1 && set "PYEXE=%%I"
        )
    )
)

REM --- 3) Common per-user and machine-wide install locations. -----------------
if not defined PYEXE (
    for %%R in ("%LOCALAPPDATA%\Programs\Python" "C:\Program Files\Python" "C:\Program Files (x86)\Python" "C:\Python") do (
        if not defined PYEXE (
            for /f "delims=" %%I in ('dir /b /o-n "%%~R\Python3*" 2^>nul') do (
                if not defined PYEXE (
                    if exist "%%~R\%%I\python.exe" set "PYEXE=%%~R\%%I\python.exe"
                )
            )
        )
    )
)

REM --- 4) A DSH bundled runtime, if DSH happens to be installed for this user. -
if not defined PYEXE (
    for /f "delims=" %%I in ('dir /b /o-n "%USERPROFILE%\.dsh\dsh-runtimes" 2^>nul') do (
        if not defined PYEXE (
            if exist "%USERPROFILE%\.dsh\dsh-runtimes\%%I\dependencies\python\python.exe" (
                set "PYEXE=%USERPROFILE%\.dsh\dsh-runtimes\%%I\dependencies\python\python.exe"
            )
        )
    )
)

if not defined PYEXE (
    echo [journal-alert] %DATE% %TIME% ERROR: no usable Python 3 found.>>"%MARKER%"
    echo [journal-alert] Install Python 3.10+ from https://www.python.org/downloads/ and tick "Add python.exe to PATH".>>"%MARKER%"
    exit /b 2
)

REM --- Final guard: the chosen interpreter must import what the pipeline needs. -
"%PYEXE%" -c "import sqlite3,urllib.request,xml.etree.ElementTree" >nul 2>&1
if errorlevel 1 (
    echo [journal-alert] %DATE% %TIME% ERROR: "%PYEXE%" lacks required standard-library modules.>>"%MARKER%"
    exit /b 3
)

echo [journal-alert] %DATE% %TIME% using "%PYEXE%" >>"%MARKER%"

REM Scheduled runs pass no arguments: capture everything, including crash
REM tracebacks, so a failure leaves evidence even with no console attached.
if "%~1"=="" goto scheduled

"%PYEXE%" "%PROJECT%run.py" --once %*
set "CODE=%ERRORLEVEL%"
goto finish

:scheduled
"%PYEXE%" "%PROJECT%run.py" --once >"%RUNLOG%" 2>&1
set "CODE=%ERRORLEVEL%"

:finish
echo [journal-alert] %DATE% %TIME% exit=%CODE%  (details: logs\jalert-*.log, logs\run-last.log) >>"%MARKER%"
exit /b %CODE%
