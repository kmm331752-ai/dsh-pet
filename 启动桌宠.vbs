' 启动 DSH 桌宠（静默，不弹控制台窗口）
Option Explicit

Dim fso, sh, here, pyw
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)

' 优先用 DSH 自带运行时里的 pythonw，找不到则退回 PATH 上的 pythonw
pyw = sh.ExpandEnvironmentStrings("%DSH_HOME%") & _
      "\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe"
If Not fso.FileExists(pyw) Then
  pyw = "C:\Users\PC\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe"
End If
If Not fso.FileExists(pyw) Then
  pyw = "pythonw.exe"
End If

sh.CurrentDirectory = here
sh.Run """" & pyw & """ """ & here & "\dsh_pet.py""", 0, False
