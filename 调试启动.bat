@echo off
chcp 65001 >nul
rem 带控制台的启动方式 —— 出问题时用这个，能看到报错
set PY=%DSH_HOME%\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe
if not exist "%PY%" set PY=C:\Users\PC\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe
if not exist "%PY%" set PY=python
cd /d "%~dp0"
"%PY%" dsh_pet.py %*
pause
