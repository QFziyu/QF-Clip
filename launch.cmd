@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  set "QF_PYTHON=.venv\Scripts\python.exe"
  goto run
)
if exist ".runtime\python.exe" (
  set "QF_PYTHON=.runtime\python.exe"
  goto run
)
echo First launch: preparing the local environment. API configuration is optional.
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\setup.ps1"
if errorlevel 1 goto failed
set "QF_PYTHON=.venv\Scripts\python.exe"
:run
if "%~1"=="" (
  "%QF_PYTHON%" "scripts\start.py" --profile cpu
) else (
  "%QF_PYTHON%" "scripts\start.py" %*
)
if errorlevel 1 goto failed
exit /b 0
:failed
echo Startup failed. Keep this window open and check the message above.
pause
exit /b 1
