param(
    [string]$TaskName = "XAUUSD MT5 Bridge",
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "[bridge-cutover] $msg" }

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$action = @($task.Actions)[0]
if (-not $action) { throw "Scheduled task '$TaskName' has no action." }

$exe = $action.Execute
$args = [string]$action.Arguments
$cwd = [string]$action.WorkingDirectory

if ($args -notmatch 'mt5_bridge_daemon\.py') {
    throw "Task action does not reference mt5_bridge_daemon.py. Current arguments: $args"
}

$newArgs = $args -replace 'mt5_bridge_daemon\.py', 'mt5_bridge_daemon_hardened.py'

# Resolve the target script from the existing arguments/working directory.
$scriptMatch = [regex]::Match($newArgs, '(?i)([A-Z]:\\[^\"'']*mt5_bridge_daemon_hardened\.py|[^\s\"'']*mt5_bridge_daemon_hardened\.py)')
$targetScript = $null
if ($scriptMatch.Success) {
    $candidate = $scriptMatch.Groups[1].Value.Trim('"', "'")
    if ([System.IO.Path]::IsPathRooted($candidate)) {
        $targetScript = $candidate
    } elseif ($cwd) {
        $targetScript = Join-Path $cwd $candidate
    }
}
if (-not $targetScript -and $cwd) {
    $targetScript = Join-Path $cwd 'deploy\mt5_bridge_daemon_hardened.py'
}
if ($targetScript -and -not (Test-Path $targetScript)) {
    throw "Hardened bridge script not found at '$targetScript'. Pull the takeover branch/merged main first."
}

Write-Step "Task: $TaskName"
Write-Step "Executable: $exe"
Write-Step "Current args: $args"
Write-Step "Proposed args: $newArgs"
Write-Step "Working dir: $cwd"
if ($targetScript) { Write-Step "Validated script: $targetScript" }

if (-not $Apply) {
    Write-Host ""
    Write-Host "DRY RUN ONLY — no task changes made. Re-run with -Apply after review."
    exit 0
}

$backupDir = Join-Path $env:TEMP "xauusd-bridge-task-backups"
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupPath = Join-Path $backupDir ("{0}-{1}.xml" -f ($TaskName -replace '[^A-Za-z0-9_-]','_'), $stamp)
Export-ScheduledTask -TaskName $TaskName | Set-Content -Encoding UTF8 $backupPath
Write-Step "Backup written: $backupPath"

$newAction = New-ScheduledTaskAction -Execute $exe -Argument $newArgs -WorkingDirectory $cwd
Set-ScheduledTask -TaskName $TaskName -Action $newAction | Out-Null

# Restart the task so the hardened launcher is the running process.
try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

$verify = Get-ScheduledTask -TaskName $TaskName
$verifyAction = @($verify.Actions)[0]
if ([string]$verifyAction.Arguments -notmatch 'mt5_bridge_daemon_hardened\.py') {
    throw "Cutover verification failed: task arguments did not persist. Backup: $backupPath"
}

Write-Step "HARDENED bridge task is configured and started."
Write-Step "Rollback: Register-ScheduledTask -TaskName '$TaskName' -Xml (Get-Content -Raw '$backupPath') -Force"
