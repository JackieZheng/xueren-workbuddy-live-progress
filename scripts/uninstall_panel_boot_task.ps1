# Remove the live progress panel AtLogOn Scheduled Task (undo of install_panel_boot_task.ps1).
# ASCII-only comments on purpose (UTF-8 ps1 with Chinese turns into mojibake on PS 5.1).
$ErrorActionPreference = "Stop"

$taskName = "WorkBuddyLiveProgressPanel-Boot"
$userPath = "\" + $env:USERNAME + "\"

Unregister-ScheduledTask -TaskName $taskName -TaskPath $userPath -Confirm:$false -ErrorAction SilentlyContinue
Write-Host ("done (task " + $userPath + $taskName + " removed if it existed)")
