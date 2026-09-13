@echo off
set PY=%~dp0.venv\Scripts\pythonw.exe
if not exist "%PY%" set PY=pythonw
start "" "%PY%" "%~dp0app\lantern_shell.py" %*
