@echo off
title Install Cloudflared Tunnel Guard
echo Installing cloudflared tunnel guard task (run as administrator)...
powershell -ExecutionPolicy Bypass -File "%~dp0fix_tunnel_guard.ps1"
echo.
echo Press any key to close.
pause >nul
