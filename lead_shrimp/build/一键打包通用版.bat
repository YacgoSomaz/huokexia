@echo off
setlocal
cd /d "%~dp0"
set /p VERSION=请输入版本号（例如 0.1.0）：
if "%VERSION%"=="" set "VERSION=0.1.0"
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_public_release.ps1" -Version "%VERSION%"
if errorlevel 1 (
  echo.
  echo 构建失败，请检查上方错误信息。
  pause
  exit /b 1
)
echo.
echo 通用安装包构建完成。
pause
