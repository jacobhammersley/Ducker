@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
echo.
echo  Ducker - build
echo  ==============
echo.

set PYTHON=
call :find_python
if not defined PYTHON (
    echo  Python was not found. Install it from https://www.python.org/downloads/
    echo  tick "Add python.exe to PATH" during setup, and try again.
    pause
    exit /b 1
)
echo  Using Python at: !PYTHON!

echo.
echo  [1/5] Closing any open copies of the app and the installer...
taskkill /f /im "Ducker.exe" >nul 2>&1
taskkill /f /im "Ducker - Install Menu.exe" >nul 2>&1
taskkill /f /im "Ducker - Installer.exe" >nul 2>&1
taskkill /f /im "Ducker - Uninstall.exe" >nul 2>&1
taskkill /f /im "Music Ducker.exe" >nul 2>&1
taskkill /f /im "Music Ducker - Install Menu.exe" >nul 2>&1
taskkill /f /im "Auto Duck.exe" >nul 2>&1
taskkill /f /im "Auto Duck - Install Menu.exe" >nul 2>&1
taskkill /f /im "Spotify Auto Duck.exe" >nul 2>&1
taskkill /f /im "Spotify Auto Duck - Install Menu.exe" >nul 2>&1
taskkill /f /im "Spotify Auto Duck Setup.exe" >nul 2>&1
taskkill /f /im "Uninstall.exe" >nul 2>&1
taskkill /f /im pythonw.exe >nul 2>&1
timeout /t 2 /nobreak >nul

echo  Clearing the old build output...
set OUT=dist
if exist dist rmdir /s /q dist
if exist dist (
    rem Something in dist is locked, often Windows Defender holding a file it
    rem flagged. Build into a fresh folder instead so nothing is blocked.
    set OUT=dist_%RANDOM%
    echo  Some old files in dist are locked, so this build goes into !OUT! instead.
    echo  If Windows Security has blocked them, allow them in Protection history and
    echo  add this folder to its exclusions.
)
if exist build rmdir /s /q build

echo  [2/5] Installing the libraries needed to build...
"!PYTHON!" -m pip install --quiet --upgrade pyinstaller customtkinter pycaw comtypes psutil pystray pillow pywin32 miniaudio
if errorlevel 1 (
    echo  The libraries failed to install. Check your internet connection and try again.
    pause
    exit /b 1
)

rem Windows media controls support (optional: the app falls back without it)
"!PYTHON!" -m pip install --quiet --upgrade winrt-runtime winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Media.Control >nul 2>&1
set WINRT_ARG=
"!PYTHON!" -c "import winrt.windows.media.control" >nul 2>&1
if not errorlevel 1 (
    set WINRT_ARG=--collect-all winrt
    echo  Media controls support is available and will be included.
) else (
    echo  Media controls support could not be installed. Pausing will use the fallback method.
)

echo  [3/5] Fetching the Poppins font (skipped if it fails, Segoe UI is used instead)...
if not exist fonts mkdir fonts
for %%f in (Poppins-Regular Poppins-Medium Poppins-SemiBold Poppins-Bold) do (
    if not exist "fonts\%%f.ttf" (
        powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/%%f.ttf' -OutFile 'fonts\%%f.ttf' } catch { }" >nul 2>&1
    )
)

set FONTS_ARG=
if exist "fonts\*.ttf" set FONTS_ARG=--add-data "fonts;fonts"

echo  Preparing the test recordings...
"!PYTHON!" make_voice.py
if not exist "sounds\voice.wav" (
    echo.
    echo  The test announcement is missing. The build carries on, but the Test page
    echo  will only have the music until sounds\voice.wav exists.
    echo.
)

echo  [4/5] Building Ducker.exe ...
"!PYTHON!" -m PyInstaller --noconfirm --clean --onefile --windowed --distpath "!OUT!" ^
    --name "Ducker" --icon icon.ico ^
    --add-data "icon.ico;." --add-data "art;art" --add-data "sounds;sounds" !FONTS_ARG! ^
    --collect-all customtkinter !WINRT_ARG! --hidden-import comtypes.stream ^
    --hidden-import pystray._win32 --hidden-import PIL._tkinter_finder ^
    --hidden-import win32com.client --hidden-import pythoncom ^
    ducker.py >build_app.log 2>&1
if not exist "!OUT!\Ducker.exe" (
    echo  Building the app failed. See build_app.log for details.
    pause
    exit /b 1
)

echo  [5/5] Building Ducker - Installer.exe (with the app inside it)...
"!PYTHON!" -m PyInstaller --noconfirm --clean --onefile --windowed --distpath "!OUT!" ^
    --name "Ducker - Installer" --icon icon.ico ^
    --add-data "icon.ico;." !FONTS_ARG! ^
    --add-data "!OUT!\Ducker.exe;." ^
    --collect-all customtkinter --hidden-import win32com.client ^
    setup.py >build_setup.log 2>&1
if not exist "!OUT!\Ducker - Installer.exe" (
    echo  Building the installer failed. See build_setup.log for details.
    pause
    exit /b 1
)

rem Tidy up the PyInstaller leftovers
rmdir /s /q build >nul 2>&1
del /q *.spec >nul 2>&1

rem Remove any old script-based startup shortcut, the installer makes its own
del /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Spotify Auto Duck.lnk" >nul 2>&1

rem Tell Windows to redraw icons so the circle logo shows everywhere
ie4uinit.exe -show >nul 2>&1

echo.
echo  Done. Your two programs are in the !OUT! folder:
echo    !OUT!\Ducker - Installer.exe      - the installer (this is the file you share)
echo    !OUT!\Ducker.exe                  - the app itself (also inside the installer)
echo.
echo  Opening the folder now.
start "" "%~dp0!OUT!"
pause
exit /b 0


:find_python
for %%v in (313 312 311 310) do (
    if not defined PYTHON (
        if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe"
    )
    if not defined PYTHON (
        if exist "%ProgramFiles%\Python%%v\python.exe" set "PYTHON=%ProgramFiles%\Python%%v\python.exe"
    )
)
if defined PYTHON goto :eof
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)"') do set "PYTHON=%%i"
)
goto :eof
