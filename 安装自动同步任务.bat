@echo off
title Install THS Daily Sync Task
echo Installing DSA-ThsDailySync task (run as administrator)...
echo Daily 15:30 auto-sync THS ledger (no manual export needed)
powershell -ExecutionPolicy Bypass -File "%~dp0fix_ths_sync_task.ps1"
echo.
echo Press any key to close.
pause >nul
