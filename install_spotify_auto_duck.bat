@echo off
setlocal EnableDelayedExpansion
echo.
echo  Spotify Auto Duck - installer
echo  =============================
echo.

set PYTHON=
set PYTHONW=

call :find_python
if not defined PYTHON (
    echo  Python is not installed yet. Installing it now with winget...
    echo  This can take a minute or two.
    echo.
    winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements --silent
    echo.
    call :find_python
)

if not defined PYTHON (
    echo  Python still could not be found.
    echo  Please install it from https://www.python.org/downloads/
    echo  tick "Add python.exe to PATH" during setup, then run this file again.
    pause
    exit /b 1
)

echo  Using Python at: !PYTHON!
echo.
echo  Stopping any copy that is already running...
taskkill /f /im pythonw.exe >nul 2>&1

echo  Installing the libraries it needs...
"!PYTHON!" -m pip install --quiet --upgrade pycaw comtypes psutil pystray pillow
if errorlevel 1 (
    echo  The libraries failed to install. Check your internet connection and try again.
    pause
    exit /b 1
)

echo  Setting it to start automatically with Windows...
set SCRIPT=%~dp0spotify_auto_duck.py
set SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Spotify Auto Duck.lnk

powershell -NoProfile -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT%');" ^
  "$s.TargetPath = '!PYTHONW!';" ^
  "$s.Arguments = '\"%SCRIPT%\" --minimized';" ^
  "$s.WorkingDirectory = '%~dp0';" ^
  "$s.WindowStyle = 7;" ^
  "$s.Save()"

echo  Starting it now...
start "" "!PYTHONW!" "%SCRIPT%"

echo.
echo  Done. The Spotify Auto Duck window should be open.
echo  Closing the window hides it in the system tray, where it keeps working.
echo  It will start in the tray every time you log in to Windows.
echo  To remove it from startup, delete "Spotify Auto Duck" from the folder
echo  that opens when you press Win+R and type shell:startup
echo.
pause
exit /b 0


:find_python
rem Look in the usual install folders first, newest version first.
for %%v in (313 312 311 310) do (
    if not defined PYTHON (
        if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" (
            set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe"
            set "PYTHONW=%LOCALAPPDATA%\Programs\Python\Python%%v\pythonw.exe"
        )
    )
    if not defined PYTHON (
        if exist "%ProgramFiles%\Python%%v\python.exe" (
            set "PYTHON=%ProgramFiles%\Python%%v\python.exe"
            set "PYTHONW=%ProgramFiles%\Python%%v\pythonw.exe"
        )
    )
)
if defined PYTHON goto :eof

rem Then try the "py" launcher, which the Store stub never provides.
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)"') do set "PYTHON=%%i"
    set "PYTHONW=!PYTHON:python.exe=pythonw.exe!"
)
goto :eof
