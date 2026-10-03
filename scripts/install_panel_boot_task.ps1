# Register the live progress panel self-start as a Windows Scheduled Task
# (trigger: AtLogOn, current user) with NO console window.
#
# The task starts pythonw.exe directly (not powershell.exe, not python.exe):
#   - python.exe owns a console window -> a cmd window pops up at every logon;
#   - a powershell.exe -File launcher would leave a powershell window too.
# panel_ctl.py start is idempotent, so running it twice (Startup folder + task)
# is harmless.
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File install_panel_boot_task.ps1
# Remove: powershell -NoProfile -ExecutionPolicy Bypass -File uninstall_panel_boot_task.ps1
#
# NOTE: ASCII-only comments on purpose. Windows PowerShell 5.1 parses a UTF-8
# .ps1 containing Chinese text as GBK, and mojibake in a string literal fails.
$ErrorActionPreference = "Stop"

$taskName = "WorkBuddyLiveProgressPanel-Boot"
$userPath = "\" + $env:USERNAME + "\"          # per-user path: no elevation needed
$pyw  = Join-Path $env:USERPROFILE ".workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
$ctl  = Join-Path $env:USERPROFILE ".workbuddy\skills\xueren-workbuddy-live-progress\scripts\panel_ctl.py"
if (-not (Test-Path $pyw))  { $pyw  = "pythonw.exe" }

if (-not (Test-Path $ctl)) {
    Write-Host ("ERROR: panel_ctl.py not found -> " + $ctl)
    exit 1
}

$tr = "`"" + $pyw + "`"" + " `" + $ctl + "`"" + " start --port 8791 --no-browser"

# schtasks.exe creates the task in the caller's own namespace, so a plain user
# account can do it. Fall back to the cmdlet (needs elevation).
$out = & schtasks.exe /Create /SC ONLOGON /TN ($userPath + $taskName) /TR $tr /F 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host ("ERROR: schtasks /Create failed: " + ($out -join " "))
    try {
        $action  = New-ScheduledTaskAction -Execute $pyw -Argument ("`"" + $ctl + "`"" + " start --port 8791 --no-browser")
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        Register-ScheduledTask -TaskName $taskName -TaskPath $userPath `
            -Action $action -Trigger $trigger -Force | Out-Null
        Write-Host ("registered via cmdlet -> " + $userPath + $taskName)
        exit 0
    } catch {
        Write-Host ("FAILED: " + $_.Exception.Message)
        Write-Host ("Hint: run this file from a PowerShell started with")
        Write-Host ("      'Run as administrator', or just rely on the Startup")
        Write-Host ("      folder batch file LiveProgressPanel.bat (already present).")
        exit 2
    }
}
Write-Host ("OK: task registered -> " + $userPath + $taskName + " (ONLOGON, windowless)")
Write-Host ("     " + $tr)
