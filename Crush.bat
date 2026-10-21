@echo off
REM ===========================================================================
REM  Crush - launcher
REM  Checks everything it needs BEFORE opening a window, so a problem shows up
REM  as a message you can read rather than a window that stops responding.
REM ===========================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Crush

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python was not found on this PC.
  echo.
  echo   Install it from https://www.python.org/downloads/
  echo   On the FIRST screen of the installer, tick "Add python.exe to PATH".
  echo   Then double-click this file again.
  echo.
  pause
  exit /b 1
)

echo   Checking components...
python -c "import numpy, zstandard, brotli, pyppmd, cryptography, argon2" >nul 2>nul
if errorlevel 1 goto install
python -c "import webview" >nul 2>nul
if errorlevel 1 goto install
rem round 11: the Share panel (paper backup, sound) - installed once, never blocks start-up
python -c "import segno, reportlab, PIL" >nul 2>nul
if errorlevel 1 python -m pip install segno reportlab pillow >nul 2>nul
goto preflight

:install
echo   Installing what Crush needs (one time, about a minute)...
python -m pip install --upgrade pip >nul 2>nul
python -m pip install pywebview zstandard brotli pyppmd numpy cryptography argon2-cffi
if errorlevel 1 (
  echo.
  echo   The install did not finish. Check your internet connection, then
  echo   run this file again. If your workplace blocks pip, you can still use
  echo   "Crush (Browser Mode).bat" once the four packages are installed.
  echo.
  pause
  exit /b 1
)

:preflight
REM Load the engine once with the console visible. If anything is broken this
REM prints the reason instead of hiding it behind a frozen window.
python -c "import sys; sys.path.insert(0,'engine'); import crush_engine" 2>nul
if errorlevel 1 (
  echo.
  echo   The compression engine could not load. Details:
  echo.
  python -c "import sys; sys.path.insert(0,'engine'); import crush_engine"
  echo.
  echo   Try:  python -m pip install --force-reinstall numpy zstandard brotli pyppmd
  echo.
  pause
  exit /b 1
)

echo   Starting Crush...
start "" pythonw app.py

REM Give it a moment; if it died immediately, show the log rather than nothing.
timeout /t 4 /nobreak >nul
tasklist /fi "imagename eq pythonw.exe" | find /i "pythonw.exe" >nul
if errorlevel 1 (
  echo.
  echo   Crush closed straight away. The log says:
  echo.
  if exist crush-debug.log type crush-debug.log
  echo.
  echo   Try "Crush ^(Browser Mode^).bat" - it runs the same app in your
  echo   web browser and does not need the Windows WebView2 component.
  echo.
  pause
)
exit /b 0
