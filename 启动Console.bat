@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\console_launcher.ps1" -Action start
if errorlevel 1 pause
