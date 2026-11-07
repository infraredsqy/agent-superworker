@echo off
rem The shim cmd.exe finds on PATH: a bare word there resolves through PATHEXT, so `sw` is this
rem `sw.cmd`; the `.ps1` and the extensionless `sw` beside it are what the other hosts take.
rem Which interpreter is a probe, not a fallback: the probe picks one, that one runs `sw.py`
rem exactly once, and its exit code is the exit code of `sw` -- a command that failed must not be
rem asked to run again (`tools/bin/sw` is the same shim for sh: probe, then one exec).
setlocal
set "D=%~dp0"
where python >nul 2>&1
if %ERRORLEVEL% EQU 0 goto run_python
where py >nul 2>&1
if %ERRORLEVEL% EQU 0 goto run_py
echo sw: python not found on PATH 1>&2
exit /b 127

:run_python
python -X utf8 "%D%sw.py" %*
exit /b %ERRORLEVEL%

:run_py
py -3 -X utf8 "%D%sw.py" %*
exit /b %ERRORLEVEL%