# Register the patrol scheduled task: run patrol --check every 10 minutes.
# Notes (E-13): repetition duration omitted = repeat forever; IgnoreNew avoids overlap;
# ALWAYS verify after registering (a successful register does not guarantee a working schedule).
# Also removes the legacy `flowwatch-patrol` task (its capability moved to this product).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts/register_task.ps1
$ErrorActionPreference = 'Stop'
$Project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Pythonw = (Get-Command pythonw.exe).Source
$TaskName = 'patrol'

if (Get-ScheduledTask -TaskName 'flowwatch-patrol' -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName 'flowwatch-patrol' -Confirm:$false
    Write-Output 'removed legacy task: flowwatch-patrol'
}

$action = New-ScheduledTaskAction -Execute $Pythonw `
    -Argument ('"' + (Join-Path $Project 'patrol.py') + '" --check') `
    -WorkingDirectory $Project
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 10)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 8) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Force `
    -Description 'patrol: prefilter + agent forensics every 10 minutes' | Out-Null

# Verify after registering (never trust a bare success)
$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Output ("STATE: " + $task.State)
Write-Output ("NEXTRUN: " + $info.NextRunTime)
Write-Output ("REPEAT: " + $task.Triggers[0].Repetition.Interval + " / duration=" + $task.Triggers[0].Repetition.Duration)
Write-Output ("ACTION: " + $task.Actions[0].Execute + " " + $task.Actions[0].Arguments)
Write-Output ("SETTINGS: IgnoreNew=" + ($task.Settings.MultipleInstances) + " limit=" + $task.Settings.ExecutionTimeLimit)
