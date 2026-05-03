@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0start_bot.ps1" -Safe -WorkerCount 1
pause
