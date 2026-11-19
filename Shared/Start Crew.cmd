@echo off
rem Starts the Atlas and Iris watchdogs, which start (and keep reviving) the bots.
rem Safe to run twice: each watchdog exits if one is already on duty.
rem Paths are relative to this script (%~dp0 is the Shared folder), so the kit
rem works from any folder. pythonw is the Python launcher's windowless "pyw -3"
rem when it's installed, else the first real pythonw.exe on PATH (the Microsoft
rem Store stub in WindowsApps is skipped), so no user-specific path is needed.
setlocal
set "KIT=%~dp0.."
set "PYW="
set "PYARGS="
where pyw >nul 2>&1 && set "PYW=pyw" && set "PYARGS=-3"
if not defined PYW for /f "delims=" %%P in ('where pythonw 2^>nul ^| findstr /v /i "WindowsApps"') do if not defined PYW set "PYW=%%P"
if not defined PYW (
  echo Couldn't find pythonw. Install Python 3 from python.org, then run this again.
  exit /b 1
)
start "" "%PYW%" %PYARGS% "%KIT%\Atlas\atlas_watchdog.pyw"
start "" "%PYW%" %PYARGS% "%KIT%\Iris\iris_watchdog.pyw"
