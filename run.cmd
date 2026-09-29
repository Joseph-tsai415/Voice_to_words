@echo off
REM 雙擊即可啟動會議逐字稿工具
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo   尚未安裝，正在執行 setup.ps1 ...
    powershell -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
)
if not exist ".venv\Scripts\python.exe" (
    echo   安裝失敗，請手動執行 setup.ps1
    pause
    exit /b 1
)
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
chcp 65001 >nul
".venv\Scripts\python.exe" -m app %*
pause
