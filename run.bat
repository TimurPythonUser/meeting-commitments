@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PORT=8000"
if not "%~1"=="" set "PORT=%~1"
set "URL=http://127.0.0.1:%PORT%"
set "PY=.venv\Scripts\python.exe"

REM Ключі лежать у .env. Без цього подвійний клік із Провідника не побачить
REM жодної змінної оточення — вони є лише в тій консолі, де їх задали.
if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"
)

REM Сервер уже піднятий? Тоді нічого не запускаємо, просто відкриваємо браузер.
curl.exe -s -m 2 "%URL%/api/health" >nul 2>nul
if not errorlevel 1 (
    echo Сервер уже працює. Відкриваю %URL%
    start "" "%URL%/"
    exit /b 0
)

if not exist "%PY%" (
    echo.
    echo [ПОМИЛКА] Не знайдено %PY%
    echo Запускати треба з кореня проєкту, venv має бути на місці.
    echo.
    pause
    exit /b 1
)

if not defined DEEPSEEK_API_KEY (
    echo.
    echo [ПОМИЛКА] DEEPSEEK_API_KEY не заданий.
    echo Додай рядок DEEPSEEK_API_KEY=sk-... у файл .env поруч із цим run.bat
    echo.
    pause
    exit /b 1
)

echo.
echo   Запис розмови в домовленості
echo   %URL%
echo.
echo   Перший запуск довший на 1-2 хвилини: гріються моделі.
echo   Браузер відкриється сам. Ctrl+C — зупинити.
echo.

REM Чекаємо готовності у фоні й відкриваємо браузер, щоб не ловити
REM «не вдалося підключитися», поки вантажаться моделі.
start "" /b powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$u='%URL%/api/health'; for($i=0;$i -lt 600;$i++){ try{ if((Invoke-WebRequest -Uri $u -TimeoutSec 2 -UseBasicParsing).StatusCode -eq 200){ Start-Process '%URL%/'; break } }catch{}; Start-Sleep -Seconds 1 }"

"%PY%" -m uvicorn app.main:app --app-dir backend --port %PORT%
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo Сервер завершився з кодом %RC%.
    echo Якщо порт %PORT% зайнятий — запусти з іншим: run.bat 8001
    echo.
    pause
)
endlocal
