Option Explicit
Dim sh, fso, baseDir, ps1, id, cmd

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = fso.BuildPath(baseDir, "QoderCN-Multi.ps1")

If WScript.Arguments.Count < 1 Then
    MsgBox "Missing account name.", 16, "Qoder CN Multi"
    WScript.Quit 2
End If

id = WScript.Arguments(0)
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """ launch """ & id & """"
sh.Run cmd, 0, False
