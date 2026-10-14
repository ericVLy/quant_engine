' ============================================================================
' run_hidden.vbs —— 以「隐藏窗口」方式启动一个 .cmd 脚本（Windows 单机生产部署用）
'
' 背景：计划任务若直接执行 cmd.exe /c xxx.cmd，用户登录时会弹出黑色控制台窗口。
' 本脚本用 WScript.Shell.Run 以 0（隐藏）方式拉起，从而不弹窗。
'
' 用法（通常由 install.ps1 注册计划任务时自动调用）：
'     wscript.exe "<deploy\windows>\run_hidden.vbs" "<要启动的 cmd 脚本全路径>"
'
' 关键设计：第三个参数 bWait=True —— **阻塞等待**子进程结束。
' 若用 False 先返回，WScript 立刻退出，计划任务会认为「任务已完成」，
' 状态变成 Ready 而非 Running，任务设置里的「失败后重启」也就不会生效。
' 阻塞等待才能让任务在整个服务运行期间保持 Running 状态。
' ============================================================================
Option Explicit

Dim shell, fso, scriptPath, cmdLine

If WScript.Arguments.Count = 0 Then
    WScript.Echo "用法: wscript.exe run_hidden.vbs <要静默启动的 .cmd 脚本路径>"
    WScript.Quit 1
End If

Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")

scriptPath = WScript.Arguments(0)

If Not fso.FileExists(scriptPath) Then
    WScript.Echo "找不到脚本: " & scriptPath
    WScript.Quit 1
End If

cmdLine = "cmd.exe /c """ & scriptPath & """"

' 第二个参数 0 = 隐藏窗口（不弹控制台）
' 第三个参数 True = 等待其结束（保持计划任务为「正在运行」状态）
shell.Run cmdLine, 0, True