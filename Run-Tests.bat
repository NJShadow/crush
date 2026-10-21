@echo off
REM ===========================================================================
REM  Crush - run the verification suites on THIS computer
REM  The suites that matter for "will I ever lose data", run with the Python
REM  and the helpers that Crush.bat uses. Takes about 10 minutes. Nothing
REM  outside a temporary folder is written. The browser tests are not run here
REM  (they need Playwright); see CLAUDE.md for the full list.
REM ===========================================================================
setlocal
cd /d "%~dp0"
title Crush tests
where python >nul 2>nul || (echo   Python not found - see README.md & pause & exit /b 1)
set FAILED=0
echo.
echo   1/5  engine self-test
python app.py --selftest || set FAILED=1
echo.
echo   2/5  data safety (tests\test_safety.py)
python tests\test_safety.py || set FAILED=1
echo.
echo   3/5  container unwrapping, pinned deflate, JPEG (tests\test_unwrap.py)
python tests\test_unwrap.py || set FAILED=1
echo.
echo   4/5  learned code model (tests\test_nn.py)
python tests\test_nn.py || set FAILED=1
echo.
echo   5/5  encryption (tests\test_crypt.py --skip-slow)
python tests\test_crypt.py --skip-slow || set FAILED=1
echo.
echo   ---------------------------------------------------------------
if "%FAILED%"=="0" (echo   ALL SUITES PASSED) else (echo   SOMETHING FAILED - scroll up for the line marked FAIL)
echo   ---------------------------------------------------------------
echo.
pause
