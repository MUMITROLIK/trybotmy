@echo off
chcp 65001 >nul
cls
color 0C

echo.
echo ╔════════════════════════════════════════════════════════════════╗
echo ║          FuturesBot - Остановка                                ║
echo ╚════════════════════════════════════════════════════════════════╝
echo.

echo 🛑 Останавливаем все процессы FuturesBot...
echo.

REM Останавливаем main.py
taskkill /F /FI "WINDOWTITLE eq FuturesBot*" >nul 2>&1
taskkill /F /FI "IMAGENAME eq python.exe" /FI "MEMUSAGE gt 50000" >nul 2>&1

echo ✅ Все процессы остановлены
echo.

timeout /t 2 /nobreak >nul
