@echo off
setlocal

cd /d "%~dp0"

echo ================================================================
echo SmartIoTPrinting Local Print Agent
echo Project path: %CD%
echo Starting local print agent...
echo Keep this window open to process paid print jobs from MongoDB.
echo ================================================================

python print_agent.py

echo.
echo Agent stopped. Press any key to close this window.
pause >nul
