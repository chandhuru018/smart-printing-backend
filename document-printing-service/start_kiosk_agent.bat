@echo off
setlocal
cd /d "%~dp0"

echo ================================================================
echo  Smart IoT Printing — Local Kiosk Agent
echo  Keep this window open. Closing it stops the printer agent.
echo ================================================================
echo.
echo  Kiosk UI will open at: http://localhost:5001
echo.

:: Auto-open browser after 3 seconds (ping waits 3 intervals of ~1s each)
start "" /b cmd /c "ping -n 3 127.0.0.1 >nul && start http://localhost:5001"

echo WARNING: DO NOT CLOSE THIS BLACK WINDOW!
echo If you close this window, the Kiosk Server will instantly disconnect!
echo.

set PYTHONIOENCODING=utf-8
python kiosk_agent.py

echo.
echo Server Stopped! Press any key to exit.
pause >nul
