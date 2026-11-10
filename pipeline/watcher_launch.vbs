' watcher_launch.vbs — 隐藏启动 watcher supervisor（计划任务动作入口）
'
' 背景：计划任务直接调 powershell.exe 属于控制台程序，每次触发都会在实际桌面闪一下黑窗。
'       wscript.exe 是 GUI 宿主，本脚本用隐藏窗口样式（Run 第二参数 0）拉起 PowerShell，
'       全程不出现可见控制台；supervisor 内部再用 pythonw 起 watcher，因此整条链都没有窗口。
'
' 退出：立即返回（不等待）；真正的拉起/日志由 watcher_supervisor.ps1 负责。

Option Explicit

Dim fso, shell, base, ps1, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = fso.BuildPath(base, "watcher_supervisor.ps1")

If Not fso.FileExists(ps1) Then
    WScript.Quit 2
End If

cmd = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & ps1 & """"
shell.Run cmd, 0, False
WScript.Quit 0
