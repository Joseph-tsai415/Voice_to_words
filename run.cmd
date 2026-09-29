@echo off
REM 雙擊即可啟動會議逐字稿工具。
REM 第一次執行會自動安裝（虛擬環境、套件、GPU、模型），之後直接啟動。
REM 判斷要不要安裝的邏輯只寫在 run.ps1 一個地方，這裡只負責轉交。
setlocal
cd /d "%~dp0"
chcp 65001 >nul
powershell -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
pause
