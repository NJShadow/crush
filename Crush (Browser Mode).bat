@echo off
REM ===========================================================================
REM  Crush - browser mode
REM  Runs the identical app, but draws the interface in your default web
REM  browser instead of a native window. Use this if the normal launcher
REM  opens a window that never comes alive - that is almost always the
REM  Windows WebView2 component, which this mode does not use at all.
REM  Keep this console window open while you work; closing it stops Crush.
REM ===========================================================================
setlocal
cd /d "%~dp0"
title Crush (browser mode)
where python >nul 2>nul || (echo   Python not found - see README.md & pause & exit /b 1)
python -c "import numpy, zstandard, brotli, pyppmd, cryptography, argon2" >nul 2>nul || (
  echo   Installing what Crush needs...
  python -m pip install zstandard brotli pyppmd numpy cryptography argon2-cffi || (pause & exit /b 1)
)
python -c "import segno, reportlab, PIL" >nul 2>nul || python -m pip install segno reportlab pillow >nul 2>nul
echo.
echo   Starting Crush in your browser. Leave this window open.
echo.
python app.py --browser
pause
