Dim shell
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\") - 1)
shell.Run "pythonw main.py", 0, False
