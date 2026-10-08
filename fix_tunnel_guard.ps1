# Cloudflared tunnel guard task installer (run as administrator)
# Plan A: AtStartup + S4U (no login required, no password stored)
# Effects:
#   1. Tunnel auto-starts on boot -> public access (seajn.dpdns.org) works even at lock screen
#   2. Health probe every 10 minutes; auto-restart if tunnel process is down
#   3. Battery mode does NOT stop the tunnel
$tunnelBat = "C:\Users\admin\cloudflared\start_tunnel.bat"

$startup = New-ScheduledTaskTrigger -AtStartup
$time = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Days 3650)
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$tunnelBat`""
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 72)
$settings.DisallowStartIfOnBatteries = $false
$settings.StopIfGoingOnBatteries = $false
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited

Get-ScheduledTask -TaskName "Cloudflared-Guard" -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName "Cloudflared-Guard" -Action $action -Trigger $startup,$time -Settings $settings -Principal $principal -Force | Out-Null

$v = Get-ScheduledTask -TaskName "Cloudflared-Guard"
Write-Host ""
Write-Host "=== Tunnel guard task installed ===" -ForegroundColor Green
Write-Host "State: $($v.State)"
Write-Host "Triggers: $($v.Triggers.Count) (AtStartup + every 10 min)"
Write-Host "Action: $($v.Actions[0].Execute) $($v.Actions[0].Arguments)"
Write-Host "LogonType: $($v.Principal.LogonType) (no login required)"
Write-Host "Battery: Disallow=$($v.Settings.DisallowStartIfOnBatteries) Stop=$($v.Settings.StopIfGoingOnBatteries)"
