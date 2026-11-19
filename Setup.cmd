@echo off
rem Double-click this to set up Atlas. It makes sure Python 3.12 and the
rem packages are here, then opens the Atlas app, which walks through the rest.
setlocal
cd /d "%~dp0"
title Atlas setup

set "PY="
for %%P in (py.exe) do if not "%%~$PATH:P"=="" set "PY=py -3.12"
if defined PY (
  %PY% -c "import sys" >nul 2>&1 || set "PY="
)
if not defined PY (
  echo Installing Python 3.12. Windows may ask for permission.
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  if errorlevel 1 (
    echo.
    echo Python didn't install. Get Python 3.12 from python.org, tick "Add python.exe to PATH",
    echo then double-click Setup.cmd again.
    pause
    exit /b 1
  )
  set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)

echo Installing the packages Atlas uses. This takes a few minutes the first time.
%PY% -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo.
  echo The package install failed. Check the internet connection and run Setup.cmd again.
  pause
  exit /b 1
)

rem pythonw has no console window, so this window can close once the app is up.
for /f "delims=" %%W in ('%PY% -c "import sys,pathlib;print(pathlib.Path(sys.executable).with_name('pythonw.exe'))"') do set "PYW=%%W"
start "" "%PYW%" "%~dp0App\Atlas.pyw" --setup
endlocal
