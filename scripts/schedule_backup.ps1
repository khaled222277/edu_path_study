<#
.SYNOPSIS
  Register (or remove) a daily Windows Task Scheduler job that runs scripts\backup.py.

.DESCRIPTION
  Nothing is registered unless you run it with -Register. Use -DryRun to see exactly what
  would be created. The task runs as the CURRENT user and only while you are signed in (the simple,
  password-free option). For an always-on
  server, create the task by hand with "Run whether user is logged on or not".

  The job runs   <repo>\.venv\Scripts\python.exe scripts\backup.py backup --keep N
  backup.py verifies the new backup, prunes old ones, appends a line to backups\backup.log and
  exits non-zero on failure (Task Scheduler shows it as "Last Run Result" != 0x0).

.EXAMPLE
  .\scripts\schedule_backup.ps1 -DryRun
  .\scripts\schedule_backup.ps1 -Register -At 02:30 -Keep 14
  .\scripts\schedule_backup.ps1 -Unregister
#>
[CmdletBinding()]
param(
  [switch]$Register,
  [switch]$Unregister,
  [switch]$DryRun,
  [string]$At = "02:00",
  [int]$Keep = 7,
  [string]$TaskName = "EduPath Backup"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\python.exe"
$script = Join-Path $repo "scripts\backup.py"
$arguments = "`"$script`" backup --keep $Keep"

if ($At -notmatch '^([01]\d|2[0-3]):[0-5]\d$') { throw "-At must be HH:MM (24 hour), got '$At'" }
if ($Keep -lt 0) { throw "-Keep must be 0 or more" }

if ($Unregister) {
  if ($DryRun) { Write-Output "DRY RUN: would remove scheduled task '$TaskName'"; return }
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Write-Output "Removed scheduled task '$TaskName'"
  return
}

Write-Output "Task      : $TaskName"
Write-Output "Runs      : daily at $At"
Write-Output "Command   : $python $arguments"
Write-Output "Start in  : $repo"
Write-Output "Keeps     : newest $Keep backups (log: $repo\backups\backup.log)"

if (-not $Register) {
  Write-Output "DRY RUN: nothing registered. Add -Register to create the task (and -Unregister later to remove it)."
  return
}
if ($DryRun) { Write-Output "DRY RUN: nothing registered."; return }
if (-not (Test-Path $python)) { throw "Python not found at $python (create the virtualenv first)" }

$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "EduPath: Postgres + data backup with verify and retention" | Out-Null
Write-Output "Registered scheduled task '$TaskName'. Check: Get-ScheduledTaskInfo -TaskName '$TaskName'"
