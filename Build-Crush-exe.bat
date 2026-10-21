@echo off
REM ===========================================================================
REM  Crush - build a standalone Crush.exe (no Python needed to run it)
REM ===========================================================================
setlocal
cd /d "%~dp0"
where python >nul 2>nul || (echo   Python is required to BUILD the exe. & pause & exit /b 1)
echo   Installing build requirements...
python -m pip install --upgrade pip >nul 2>nul
python -m pip install pyinstaller pywebview zstandard brotli pyppmd numpy cryptography argon2-cffi segno reportlab pillow || (pause & exit /b 1)
echo.
echo   Building Crush.exe ...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name Crush ^
  --add-data "ui;ui" ^
  --add-data "engine;engine" ^
  --add-data "share;share" ^
  --add-data "channels;channels" ^
  --hidden-import zstandard --hidden-import brotli --hidden-import pyppmd ^
  --hidden-import numpy --hidden-import webview ^
  --hidden-import cryptography --hidden-import argon2 --hidden-import segno ^
  --hidden-import reportlab --hidden-import PIL ^
  app.py
if errorlevel 1 (echo   Build failed. & pause & exit /b 1)
echo.
echo   Done. dist\Crush.exe is ready.
echo   Copy the "priors" and "tools" folders next to Crush.exe before running it.
echo.
pause
