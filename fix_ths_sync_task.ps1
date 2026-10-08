# DSA ThsDailySync task installer (run as administrator)
# Runs the online THS ledger sync every trading day at 15:30 (after close),
# without any manual export/download. Skips weekends; holidays are idempotent.
$proj = "E:\daily_stock_analysis-main"

# Trigger: daily at 15:30, start if missed (e.g. machine was off at 15:30)
$daily = New-ScheduledTaskTrigger -Daily -At "15:30"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument '/c ""E:\daily_stock_analysis-main\.venv\Scripts\python.exe" "E:\daily_stock_analysis-main\tools\ths_daily_sync.py" >> "E:\daily_stock_analysis-main\logs\ths_daily_sync_task.log" 2>&1' -WorkingDirectory $proj
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) -StartWhenAvailable
$settings.DisallowStartIfOnBatteries = $false
$settings.StopIfGoingOnBatteries = $false
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited

Get-ScheduledTask -TaskName "DSA-ThsDailySync" -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName "DSA-ThsDailySync" -Action $action -Trigger $daily -Settings $settings -Principal $principal -Force | Out-Null

$v = Get-ScheduledTask -TaskName "DSA-ThsDailySync"
Write-Host ""
Write-Host "=== DSA-ThsDailySync installed ===" -ForegroundColor Green
Write-Host "State: $($v.State)"
Write-Host "Trigger: $($v.Triggers[0].StartBoundary) (daily 15:30, start-if-missed)"
Write-Host "Action: $($v.Actions[0].Execute) $($v.Actions[0].Arguments)"
Write-Host "LogonType: $($v.Principal.LogonType) (no login required)"
Write-Host "Battery: Disallow=$($v.Settings.DisallowStartIfOnBatteries) Stop=$($v.Settings.StopIfGoingOnBatteries)"
Write-Host ""
Write-Host "To test immediately, run:"
Write-Host "  Start-ScheduledTask -TaskName 'DSA-ThsDailySync'"
Write-Host "Log: $proj\logs\ths_daily_sync.log"
