@echo off
chcp 65001 >nul
cls
color 0A

echo.
echo ╔════════════════════════════════════════════════════════════════╗
echo ║          FuturesBot WebSocket Server                           ║
echo ║          🚀 Запуск на http://localhost:8000                    ║
echo ╚════════════════════════════════════════════════════════════════╝
echo.

REM Проверяем наличие Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python не установлен или не в PATH
    echo Установите Python с https://www.python.org
    pause
    exit /b 1
)

echo ✅ Python найден
echo.

REM Устанавливаем зависимости
echo 📦 Проверка зависимостей...
pip install fastapi uvicorn --quiet

echo.
echo ✅ Всё готово!
echo.
echo 🔌 WebSocket сервер запускается...
echo 📡 Адрес: http://localhost:8000
echo 🔗 WebSocket: ws://localhost:8000/ws
echo.
echo 💡 Откройте в браузере: http://localhost:8000
echo 💡 Для остановки нажмите Ctrl+C
echo.

REM Запускаем сервер
python server.py

pause
