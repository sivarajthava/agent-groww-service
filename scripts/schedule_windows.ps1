# Registers a Windows Task Scheduler job that runs `pulse run` every Monday at 09:00 IST.
#
# Usage (from the repo root, in PowerShell):
#   .\scripts\schedule_windows.ps1                 # publish (Doc + Gmail draft)
#   .\scripts\schedule_windows.ps1 -DryRun         # write data/runs/<week>/pulse.md only
#   .\scripts\schedule_windows.ps1 -Unregister     # remove the job
#
# The job runs as the current user, only when logged on, from the repo root so config.yaml, .env and data/ resolve.
param(
    [switch]$DryRun,
    [switch]$Unregister,
    [string]$TaskName = "GrowwWeeklyPulse"
)

$ErrorActionPreference = "Stop"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task $TaskName"
    return
}

$repo = Split-Path -Parent $PSScriptRoot
$pulse = Join-Path $repo ".venv\Scripts\pulse.exe"
if (-not (Test-Path $pulse)) {
    throw "pulse.exe not found at $pulse; run: python -m venv .venv; .venv\Scripts\pip install -e ."
}

# Task Scheduler uses the machine's local time: convert Monday 09:00 IST to local time.
$ist = [TimeZoneInfo]::FindSystemTimeZoneById("India Standard Time")
$today = [DateTime]::Today
$monday = $today.AddDays(((7 + [int][DayOfWeek]::Monday - [int]$today.DayOfWeek) % 7))
$istRun = [DateTime]::SpecifyKind($monday.AddHours(9), [DateTimeKind]::Unspecified)
$localRun = [TimeZoneInfo]::ConvertTime($istRun, $ist, [TimeZoneInfo]::Local)

$runArgs = if ($DryRun) { "run --dry-run" } else { "run" }
$log = Join-Path $repo "data\scheduled-run.log"
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"`"$pulse`" $runArgs >> `"$log`" 2>&1`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $localRun.DayOfWeek -At $localRun.ToString("HH:mm")
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Groww weekly review pulse (pulse $runArgs), Mondays 09:00 IST" -Force | Out-Null
Write-Host "Registered $TaskName : pulse $runArgs every $($localRun.DayOfWeek) at $($localRun.ToString('HH:mm')) local time (09:00 IST); log: $log"
