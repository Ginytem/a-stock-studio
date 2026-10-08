@echo off
title Install DSAWebUI Guard Task
echo Installing DSAWebUI guard task (run as administrator)...
powershell -ExecutionPolicy Bypass -File "%~dp0fix_guard.ps1"
echo.
echo Press any key to close.
pause >nul
