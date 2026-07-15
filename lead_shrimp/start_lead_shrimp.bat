@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

set "LEADSHRIMP_STANDALONE=1"
set "LEADSHRIMP_DATA_DIR=%LOCALAPPDATA%\LeadShrimp\data"
rem The independent repository keeps the required shared modules at its root.
set "PYTHONPATH=%CD%"
set "LOG_DIR=%LOCALAPPDATA%\LeadShrimp\logs"
set "LOG_FILE=%LOG_DIR%\LeadShrimp-launch.log"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>nul

set "PYTHON_EXE=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE="
if not defined PYTHON_EXE for /f "delims=" %%I in ('where python 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%I"

if not defined PYTHON_EXE (
  echo [ERROR] Python 3.11+ was not found. Install Python or add it to PATH.
  pause
  exit /b 1
)

start "LeadShrimp" /B "%PYTHON_EXE%" -m lead_shrimp.launcher --port 8922 --no-browser 1>"%LOG_FILE%" 2>&1
timeout /t 2 /nobreak >nul
PowerShell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8922/api/license/status' -TimeoutSec 4; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; exit 1"
if errorlevel 1 (
  echo [ERROR] LeadShrimp did not start. Log: %LOG_FILE%
  if exist "%LOG_FILE%" type "%LOG_FILE%"
  pause
  exit /b 1
)
start "" "http://127.0.0.1:8922/"
endlocal
