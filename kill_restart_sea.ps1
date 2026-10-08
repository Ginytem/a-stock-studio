# -*- coding: utf-8 -*-
# 强杀 8000 监听进程并用 start_sea.bat 重启服务（需管理员权限，UAC 提权运行）
# 用 netstat 查找监听 PID（Get-NetTCPConnection 在部分环境下查不到监听）

$ErrorActionPreference = "Stop"
$listener = $null
try {
    # netstat 输出行如: TCP 0.0.0.0:8000 0.0.0.0:0 LISTENING 11148
    $lines = netstat -ano | Where-Object { $_ -match 'TCP\s+0\.0\.0\.0:8000\s+\S+\s+LISTENING\s+(\d+)$' -or $_ -match 'TCP\s+\[::\]:8000\s+\S+\s+LISTENING\s+(\d+)$' }
    if ($lines) {
        foreach ($ln in $lines) {
            if ($ln -match 'LISTENING\s+(\d+)$') {
                $listener = [int]$Matches[1]
                break
            }
        }
    }
} catch {
    Write-Host "netstat probe error: $_"
}
if ($listener) {
    Write-Host "killed listener $listener"
    Stop-Process -Id $listener -Force -ErrorAction SilentlyContinue
} else {
    Write-Host "no listener on 8000"
}
Start-Sleep -Seconds 3
Start-Process cmd.exe -ArgumentList '/c','start_sea.bat' -WorkingDirectory 'E:\daily_stock_analysis-main' -WindowStyle Hidden
Write-Host "restart launched, waiting for health..."
$ok = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 3
        if ($r.status -eq 'ok') { Write-Host "health OK after ${i}s"; $ok = $true; break }
    } catch {}
}
if (-not $ok) { Write-Host "NOT OK after 40s" }
