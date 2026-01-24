@echo off
REM Run full session with all tasks
REM Note: Activate your Python environment before running

setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

if not exist "logs" mkdir "logs"
for /d /r "%SCRIPT_DIR%" %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d" 2>nul

python -u main.py %*
