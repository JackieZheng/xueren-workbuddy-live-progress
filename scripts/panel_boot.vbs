' Live Progress Panel - hidden boot launcher.
' Replaces LiveProgressPanel.bat in the Startup folder: a .bat launched by
' explorer flashes a cmd console window at every logon; wscript with window
' style 0 is fully hidden. pythonw.exe itself owns no console at all.
' panel_ctl.py start is idempotent and uses WMI (session-external) internally.
Dim sh, fso, pyw, ctl, log, ts
Set sh  = CreateObject("Wscript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
pyw = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
ctl = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.workbuddy\skills\xueren-workbuddy-live-progress\scripts\panel_ctl.py"
If Not fso.FileExists(pyw) Then pyw = "pythonw.exe"
If Not fso.FileExists(ctl) Then ctl = "C:\Users\JackieZheng\.workbuddy\skills\xueren-workbuddy-live-progress\scripts\panel_ctl.py"
log = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.workbuddy\scripts\panel_boot.log"
On Error Resume Next
Set ts = fso.OpenTextFile(log, 8, True)
If Err.Number = 0 Then
    ts.WriteLine "[" & Now & "] panel boot (vbs hidden launcher)"
    ts.Close
End If
On Error GoTo 0
sh.Run """" & pyw & """ """ & ctl & """ start --port 8791 --no-browser", 0, False
