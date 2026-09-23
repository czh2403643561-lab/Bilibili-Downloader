Option Explicit

Dim shell, files, folder, powershellPath, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
powershellPath = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"
command = Chr(34) & powershellPath & Chr(34) & " -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & folder & "\启动工具.ps1" & Chr(34)
shell.Run command, 0, False
