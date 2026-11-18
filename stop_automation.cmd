@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" "scripts\stop_automation.py"
pause
