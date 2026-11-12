' ---------------------------------------------------------------------------
'  EOU Bond Tracker - start in the background (no window to close by mistake).
'
'  Double-click this file. The app starts hidden and your browser opens.
'  If the app is already running, it only opens the browser.
'  To stop the app: double-click stop.bat.
'  Messages and errors are written to  %LOCALAPPDATA%\EOU Bond Tracker\app.log
'
'  First use on a PC: run start.bat once (it installs everything); after that
'  you can always use this file instead.
'
'  Optional:  start-background.vbs <port> [/nobrowser]
' ---------------------------------------------------------------------------
Option Explicit
Dim sh, fso, here, data, py, port, logf, url, q, i, arg, openBrowser, portArg

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
q = Chr(34)
here = fso.GetParentFolderName(WScript.ScriptFullName)
data = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\EOU Bond Tracker"
py = data & "\venv\Scripts\python.exe"
logf = data & "\app.log"

openBrowser = True
port = ""
For i = 0 To WScript.Arguments.Count - 1
    arg = WScript.Arguments(i)
    If LCase(arg) = "/nobrowser" Then
        openBrowser = False
    ElseIf IsNumeric(arg) Then
        port = arg
    End If
Next
portArg = ""
If port <> "" Then
    portArg = " --port " & port
Else
    port = ReadSetting(here & "\.env", "PORT", "5000")
End If
url = "http://localhost:" & port

If Not fso.FileExists(py) Then
    MsgBox "This PC hasn't been set up yet. start.bat will open now and install everything." & vbCrLf & _
           "After that, you can use start-background.vbs.", 64, "EOU Bond Tracker"
    sh.Run q & here & "\start.bat" & q, 1, False
    WScript.Quit
End If

If IsRunning(url) Then
    If openBrowser Then sh.Run url
    WScript.Quit
End If

' keep the log small: start a fresh one when it passes 5 MB
If fso.FileExists(logf) Then
    If fso.GetFile(logf).Size > 5000000 Then
        If fso.FileExists(logf & ".old") Then fso.DeleteFile logf & ".old"
        fso.MoveFile logf, logf & ".old"
    End If
End If

sh.CurrentDirectory = here
' 1. bring the database up to date (hidden, wait for it)
sh.Run "cmd /c " & q & q & py & q & " -u -m eoubond.cli upgrade >> " & q & logf & q & " 2>&1" & q, 0, True
' 2. start the app (hidden, keep running)
sh.Run "cmd /c " & q & q & py & q & " -u -m eoubond.cli serve" & portArg & " >> " & q & logf & q & " 2>&1" & q, 0, False

' wait up to 40 seconds for it to answer
For i = 1 To 40
    WScript.Sleep 1000
    If IsRunning(url) Then
        If openBrowser Then sh.Run url
        WScript.Quit
    End If
Next
MsgBox "EOU Bond Tracker did not start." & vbCrLf & "See the log for the reason:" & vbCrLf & logf, 48, "EOU Bond Tracker"
sh.Run "notepad " & q & logf & q


Function IsRunning(u)
    Dim http
    IsRunning = False
    On Error Resume Next
    Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    http.setTimeouts 1000, 1000, 2000, 2000
    http.Open "GET", u & "/login", False
    http.Send
    If Err.Number = 0 Then
        If http.Status > 0 And http.Status < 500 Then IsRunning = True
    End If
    On Error GoTo 0
End Function

Function ReadSetting(path, key, def)
    Dim f, line, p
    ReadSetting = def
    If Not fso.FileExists(path) Then Exit Function
    Set f = fso.OpenTextFile(path, 1)
    Do Until f.AtEndOfStream
        line = Trim(f.ReadLine)
        If UCase(Left(line, Len(key) + 1)) = UCase(key) & "=" Then
            p = Trim(Mid(line, Len(key) + 2))
            If p <> "" Then ReadSetting = p
        End If
    Loop
    f.Close
End Function
