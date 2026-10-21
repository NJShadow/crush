@echo off
REM ===========================================================================
REM  Crush - diagnosis
REM  Runs the engine with no interface at all and prints what it finds. If this
REM  passes, the engine is healthy and any problem is in the window; if it
REM  fails, it tells you exactly which piece is missing.
REM ===========================================================================
setlocal
cd /d "%~dp0"
title Crush diagnosis
where python >nul 2>nul || (echo   Python not found - see README.md & pause & exit /b 1)
echo.
python app.py --selftest
echo.
echo   ---------------------------------------------------------------
echo   Last run log:
echo   ---------------------------------------------------------------
if exist crush-debug.log (type crush-debug.log) else (echo   (no log yet^))
echo.
pause
