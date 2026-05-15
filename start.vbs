Set ws = CreateObject("WScript.Shell")
folder = ws.CurrentDirectory
ws.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
ws.Run "powershell -NoExit -File ""start.ps1""", 1, False
