# DSAWebUI guard task installer (run as administrator)
# Plan A: AtStartup + S4U (no login required, no password stored)
# Effects:
#   1. Service auto-starts on boot (LAN/public access works even at lock screen)
#   2. Health probe every 10 minutes; auto-restart if service is down
#   3. Battery mode does NOT stop the service
$proj = "E:\daily_stock_analysis-main"

# Trigger 1: at system startup (independent of user login)
$startup = New-ScheduledTaskTrigger -AtStartup
# Trigger 2: every 10 minutes, infinite repetition (health guard)
$time = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Days 3650)
# Action: run start_sea.bat (it probes :8000 first; exits immediately if alive, starts service if not)
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c start_sea.bat" -WorkingDirectory $proj
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 72)
$settings.DisallowStartIfOnBatteries = $false
$settings.StopIfGoingOnBatteries = $false
# S4U logon type: runs without password and without user login (as current user)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited

# Disable the old daily 23:59 task to avoid conflicts (kept for recovery)
Get-ScheduledTask -TaskName "DSAWebUI" -ErrorAction SilentlyContinue | Disable-ScheduledTask -ErrorAction SilentlyContinue

# Remove any previous guard task, then register the new one
Get-ScheduledTask -TaskName "DSAWebUI-Guard" -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName "DSAWebUI-Guard" -Action $action -Trigger $startup,$time -Settings $settings -Principal $principal -Force | Out-Null

$v = Get-ScheduledTask -TaskName "DSAWebUI-Guard"
Write-Host ""
Write-Host "=== Guard task installed ===" -ForegroundColor Green
Write-Host "State: $($v.State)"
Write-Host "Triggers: $($v.Triggers.Count) (AtStartup + every 10 min)"
Write-Host "Action: $($v.Actions[0].Execute) $($v.Actions[0].Arguments)"
Write-Host "LogonType: $($v.Principal.LogonType) (no login required)"
Write-Host "Battery: Disallow=$($v.Settings.DisallowStartIfOnBatteries) Stop=$($v.Settings.StopIfGoingOnBatteries)"
$old = Get-ScheduledTask -TaskName "DSAWebUI" -ErrorAction SilentlyContinue
if ($old) { Write-Host "Old task DSAWebUI disabled: $($old.State)" }
