Option Explicit
Dim sh, fso, appDir, localAppData, uri, appExe, cmd, env, selfCmd, i

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

If WScript.Arguments.Count < 2 Then
    WScript.Quit 2
End If

appDir = WScript.Arguments(0)
localAppData = WScript.Arguments(1)
If WScript.Arguments.Count >= 3 Then
    uri = WScript.Arguments(2)
Else
    uri = ""
End If

appExe = fso.BuildPath(appDir, "Qoder CN.exe")
If Not fso.FileExists(appExe) Then
    MsgBox "Qoder CN account installation not found:" & vbCrLf & appExe, 16, "Qoder CN"
    WScript.Quit 3
End If

Set env = sh.Environment("PROCESS")
env("LOCALAPPDATA") = localAppData
sh.CurrentDirectory = appDir

If Len(uri) > 0 Then
    cmd = """" & appExe & """ """ & uri & """"
Else
    cmd = """" & appExe & """"
End If
sh.Run cmd, 1, False

' The app re-registers qoder-cn:// to its own launcher during startup, which
' loses the per-account LOCALAPPDATA. Keep this shim installed for a while.
selfCmd = """" & WScript.FullName & """ """ & WScript.ScriptFullName & """ """ & appDir & """ """ & localAppData & """ ""%1"""
For i = 1 To 20
    WScript.Sleep 1000
    sh.RegWrite "HKCU\Software\Classes\qoder-cn\shell\open\command\", selfCmd, "REG_SZ"
Next
