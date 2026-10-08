# -*- coding: utf-8 -*-
# 强杀 8000 监听进程并用 start_sea.bat 重启服务（需管理员权限，UAC 提权运行）
$conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    $conn | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    Write-Host "killed listener"
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
