@echo off
rem Survival Dome: integrated server in DEBUG mode (Windows 10/11).
rem Differences from a normal start:
rem   DOME_BLACKOUT_RELAYS=0 - a blackout of the virtual power system (E02/E04/E07)
rem                            does NOT switch the real panel relays off.
rem   DOME_INTERLOCKS=0      - automation interlocks (IL_LOAD_SHED, IL_OVERCURRENT,
rem                            IL_SMOKE, ...) never trip and switch nothing off.
rem Any DOME_* variable already set in this console is kept, e.g. to keep interlocks:
rem   set DOME_INTERLOCKS=1
rem   run_debug_server.bat
rem Do NOT use this mode for the 90-minute shift: crises rely on blackout and interlocks.

setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not defined DOME_BLACKOUT_RELAYS set "DOME_BLACKOUT_RELAYS=0"
if not defined DOME_INTERLOCKS set "DOME_INTERLOCKS=0"
set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"

set "PYTHON="
where py >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON where python >nul 2>nul && set "PYTHON=python"
if not defined PYTHON (
    echo [debug] Python not found: install Python 3 or add it to PATH.
    pause
    exit /b 1
)

echo [debug] Survival Dome server, DEBUG mode
echo [debug]   DOME_BLACKOUT_RELAYS=%DOME_BLACKOUT_RELAYS%
echo [debug]   DOME_INTERLOCKS=%DOME_INTERLOCKS%
if defined DOME_ENERGY_MODE echo [debug]   DOME_ENERGY_MODE=%DOME_ENERGY_MODE%
if defined DOME_TIME_MODE echo [debug]   DOME_TIME_MODE=%DOME_TIME_MODE%
if defined DOME_PORT echo [debug]   DOME_PORT=%DOME_PORT%
echo [debug] Ctrl+C to stop.
echo.

%PYTHON% server_integrated.py
set "RC=%ERRORLEVEL%"

echo.
echo [debug] Server exited with code %RC%.
pause
exit /b %RC%
