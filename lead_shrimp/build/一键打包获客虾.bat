@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set /p VERSION=请输入版本号（例如 0.1.0）：
if "%VERSION%"=="" set "VERSION=0.1.0"
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_commercial_release.ps1" -Version "%VERSION%"
if errorlevel 1 (
  echo.
  echo 构建失败。请查看上方错误；不要手工复制文件绕过安全扫描。
  pause
  exit /b 1
)
echo.
echo 构建完成。
pause
