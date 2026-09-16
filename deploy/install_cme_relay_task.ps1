# Install the CME bulletin relay as a separate read-only HOME LAPTOP task.
# Run from an elevated PowerShell if Register-ScheduledTask requires it.
# This task never launches MT5 and never touches the execution poller.

$ErrorActionPreference = 'Stop'

$TaskName = 'XAUUSD CME Bulletin Relay'
$RepoRoot = 'C:\XAUUSD'
$Python = 'C:\XAUUSD\.venv\Scripts\python.exe'
$Script = 'C:\XAUUSD\deploy\cme_bulletin_relay.py'
$LogDir = 'C:\XAUUSD\logs'
$LogFile = 'C:\XAUUSD\logs\cme_relay.log'

if (-not (Test-Path $Python)) {
    throw "Python venv not found: $Python"
}
if (-not (Test-Path $Script)) {
    throw "CME relay script not found: $Script"
}
if (-not (Test-Path "$RepoRoot\deploy\.env.bridge")) {
    throw "Bridge environment file not found: $RepoRoot\deploy\.env.bridge"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Keep all secret material in deploy\.env.bridge; nothing sensitive is written
# into the scheduled-task command line.
$CmdArgs = '/c "cd /d C:\XAUUSD && C:\XAUUSD\.venv\Scripts\python.exe C:\XAUUSD\deploy\cme_bulletin_relay.py --loop >> C:\XAUUSD\logs\cme_relay.log 2>&1"'
$Action = New-ScheduledTaskAction -Execute 'C:\Windows\System32\cmd.exe' -Argument $CmdArgs -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

$Task = New-ScheduledTask -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description 'Read-only CME Metals bulletin acquisition edge; relays Section 64/62 to VPS.'
Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null

# Start immediately so commissioning does not require a reboot/logoff.
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

Write-Host "=== CME RELAY TASK ==="
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
Write-Host ""
Write-Host "=== TASK ACTION ==="
(Get-ScheduledTask -TaskName $TaskName).Actions | Format-List Execute,Arguments,WorkingDirectory
Write-Host ""
Write-Host "=== LATEST LOG ==="
if (Test-Path $LogFile) {
    Get-Content $LogFile -Tail 30
} else {
    Write-Host "Log has not been created yet: $LogFile"
}
