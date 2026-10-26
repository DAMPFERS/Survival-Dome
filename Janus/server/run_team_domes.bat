@echo off
rem Survival Dome: four virtual domes (one per team) behind a single port (Windows 10/11).
rem   Participants: ws://<host>:8765          - their own team key picks their dome
rem   Admin:        ws://<host>:8765/team/N   - admin key, N = dome 1..4
rem     python admin\admin_panel.py --server ws://localhost:8765/team/2 --port 8082
rem Domes listen on 127.0.0.1:18765..18768 only; just 8765 must be open in the firewall.
rem Any DOME_* variable already set in this console is passed to all domes, e.g.:
rem   set DOME_TIME_MODE=real
rem   run_team_domes.bat
rem Ports: DOME_ROUTER_PORT (8765), DOME_BACKEND_BASE_PORT (18765).

setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"

set "PYTHON="
where py >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON where python >nul 2>nul && set "PYTHON=python"
if not defined PYTHON (
    echo [domes] Python not found: install Python 3 or add it to PATH.
    pause
    exit /b 1
)

echo [domes] Survival Dome: 4 team domes behind one port
if defined DOME_ROUTER_PORT echo [domes]   DOME_ROUTER_PORT=%DOME_ROUTER_PORT%
if defined DOME_BACKEND_BASE_PORT echo [domes]   DOME_BACKEND_BASE_PORT=%DOME_BACKEND_BASE_PORT%
if defined DOME_TIME_MODE echo [domes]   DOME_TIME_MODE=%DOME_TIME_MODE%
if defined DOME_START_HOUR echo [domes]   DOME_START_HOUR=%DOME_START_HOUR%
if defined DOME_INTERLOCKS echo [domes]   DOME_INTERLOCKS=%DOME_INTERLOCKS%
echo [domes] Ctrl+C to stop all.
echo.

%PYTHON% run_team_domes.py
set "RC=%ERRORLEVEL%"

echo.
echo [domes] Stopped with code %RC%.
pause
exit /b %RC%
