@echo off
REM Usage: stat.bat ssn-42ZDwQjdgfVcQMVvNXpehh

setlocal enabledelayedexpansion

if "%~1"=="" (
    echo Usage: stat.bat ^<session_id^>
    echo Example: stat.bat ssn-42ZDwQjdgfVcQMVvNXpehh
    exit /b 1
)

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

REM Find session directory containing the session_id
set "SESSION_DIR="
for /d %%d in (logs\sessions\*%1*) do (
    set "SESSION_DIR=%%d"
    goto :found
)

:notfound
echo Session not found: %1
exit /b 1

:found
python scripts/llm_time_stats.py "%SESSION_DIR%"
