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

:: Auto-open browser after 2 seconds
start "" /b cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5001"

:: Run the kiosk agent
python kiosk_agent.py

echo.
echo Kiosk Agent stopped. Press any key to close.
pause >nul
