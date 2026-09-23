Option Explicit

Dim shell, files, folder, command, errorText, logFolder, logFile, item, launcherPath
On Error Resume Next
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
For Each item In files.GetFolder(folder).Files
    If LCase(files.GetExtensionName(item.Name)) = "pyw" Then
        launcherPath = item.Path
        Exit For
    End If
Next
If launcherPath = "" Then
    errorText = ChrW(&H542F) & ChrW(&H52A8) & ChrW(&H5931) & ChrW(&H8D25) & ChrW(&HFF1A) & ChrW(&H672A) & ChrW(&H627E) & ChrW(&H5230) & " Python"
    MsgBox errorText, 16, "Bilibili Downloader"
    WScript.Quit 1
End If
command = "pythonw.exe " & Chr(34) & launcherPath & Chr(34)
shell.Run command, 0, False
If Err.Number <> 0 Then
    errorText = ChrW(&H542F) & ChrW(&H52A8) & ChrW(&H5931) & ChrW(&H8D25) & ChrW(&HFF1A) & ChrW(&H672A) & ChrW(&H627E) & ChrW(&H5230) & " Python"
    logFolder = shell.ExpandEnvironmentStrings("%APPDATA%") & "\BilibiliDownloader\logs"
    If Not files.FolderExists(shell.ExpandEnvironmentStrings("%APPDATA%") & "\BilibiliDownloader") Then
        files.CreateFolder shell.ExpandEnvironmentStrings("%APPDATA%") & "\BilibiliDownloader"
    End If
    If Not files.FolderExists(logFolder) Then
        files.CreateFolder logFolder
    End If
    Set logFile = files.OpenTextFile(logFolder & "\launcher.log", 8, True, -1)
    logFile.WriteLine Now & " ERROR " & errorText
    logFile.Close
    MsgBox errorText, 16, "Bilibili Downloader"
End If
