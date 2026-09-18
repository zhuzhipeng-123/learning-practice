@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\service.ps1" -Action Start
if errorlevel 1 pause
