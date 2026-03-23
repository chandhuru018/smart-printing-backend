@echo off
setlocal

cd /d "%~dp0"

echo ================================================================
echo SmartIoTPrinting startup
echo Project path: %CD%
echo Starting Flask server...
echo Keep this window open to see upload, payment and print progress.
echo ================================================================

python app.py

echo.
echo Server stopped. Press any key to close this window.
pause >nul
