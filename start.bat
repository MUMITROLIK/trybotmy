@echo off
cls
color 0A

echo.
echo ========================================================================
echo           FuturesBot - Full Start
echo           Bot + WebServer
echo ========================================================================
echo.

REM Check Python 3.11
py -3.11 --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.11 not found
    echo Install Python 3.11 from https://www.python.org
    pause
    exit /b 1
)

echo OK: Python 3.11 found
echo.

REM Check .env file
if not exist .env (
    echo WARNING: .env file not found!
    echo Copy .env.example to .env and configure it
    pause
    exit /b 1
)

echo OK: Configuration found
echo.

REM Install dependencies
echo Checking dependencies...
py -3.11 -m pip install -r requirements.txt --quiet >nul 2>&1

echo.
echo OK: All ready!
echo.
echo Starting system...
echo.
echo Web dashboard: http://localhost:8000
echo Telegram bot: active
echo Position tracking: active
echo.
echo To stop: close both windows or press Ctrl+C
echo.

REM Start server in separate window
start "FuturesBot WebServer" cmd /k "color 0B && py -3.11 server.py"

REM Wait 3 seconds for server to start
timeout /t 3 /nobreak >nul

REM Open browser in incognito mode
echo Opening dashboard in incognito mode...
start chrome --incognito http://localhost:8000 >nul 2>&1
if errorlevel 1 (
    REM Try Edge if Chrome not found
    start msedge --inprivate http://localhost:8000 >nul 2>&1
    if errorlevel 1 (
        REM Try Firefox if Edge not found
        start firefox -private-window http://localhost:8000 >nul 2>&1
        if errorlevel 1 (
            echo Browser not found, open manually: http://localhost:8000
        )
    )
)
echo.

REM Start main bot in current window
echo.
echo ========================================================================
echo                    MAIN BOT
echo ========================================================================
echo.
py -3.11 main.py

pause
