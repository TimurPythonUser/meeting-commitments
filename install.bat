@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo   Установка: Зобов'язання з дзвінка
echo   ─────────────────────────────────
echo.

REM ── Python ───────────────────────────────────────────────────────────────
REM Нужен 3.11+: pyannote 4.x и faster-whisper на 3.9 не встанут.
set "PY_LAUNCH="
for %%V in (3.13 3.12 3.11) do (
    if not defined PY_LAUNCH (
        py -%%V -c "import sys" >nul 2>nul && set "PY_LAUNCH=py -%%V"
    )
)

if not defined PY_LAUNCH (
    echo [ОШИБКА] Не найден Python 3.11, 3.12 или 3.13.
    echo Скачайте с https://www.python.org/downloads/ и поставьте заново.
    echo.
    pause
    exit /b 1
)
echo [1/5] Python: %PY_LAUNCH%

REM ── ffmpeg ───────────────────────────────────────────────────────────────
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ОШИБКА] ffmpeg не найден в PATH — без него распознавание не работает.
    echo Установите: winget install Gyan.FFmpeg
    echo Затем откройте новое окно консоли и запустите install.bat снова.
    echo.
    pause
    exit /b 1
)
echo [2/5] ffmpeg на месте

REM ── venv ─────────────────────────────────────────────────────────────────
if exist ".venv\Scripts\python.exe" (
    echo [3/5] venv уже есть, пропускаю
) else (
    echo [3/5] Создаю .venv...
    %PY_LAUNCH% -m venv .venv
    if errorlevel 1 (
        echo [ОШИБКА] Не удалось создать виртуальное окружение.
        pause
        exit /b 1
    )
)

set "PY=.venv\Scripts\python.exe"
"%PY%" -m pip install --quiet --upgrade pip

REM ── Зависимости ──────────────────────────────────────────────────────────
REM torch ставим отдельно с CPU-индекса: обычный пакет с PyPI тянет
REM CUDA-сборку на несколько гигабайт, а видеокарта здесь не нужна.
echo [4/5] Ставлю torch (CPU-сборка, ~200 МБ)...
"%PY%" -m pip install --quiet --index-url https://download.pytorch.org/whl/cpu "torch>=2.0" "torchaudio>=2.0"
if errorlevel 1 (
    echo [ОШИБКА] Не удалось поставить torch.
    pause
    exit /b 1
)

echo [5/5] Ставлю остальные зависимости...
"%PY%" -m pip install --quiet -r backend\requirements.txt
if errorlevel 1 (
    echo [ОШИБКА] Не удалось поставить зависимости.
    pause
    exit /b 1
)

REM ── .env ─────────────────────────────────────────────────────────────────
if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo.
    echo   Создан файл .env — впишите в него ключи:
    echo     DEEPSEEK_API_KEY  — https://platform.deepseek.com
    echo     HF_TOKEN          — https://huggingface.co/settings/tokens
    echo.
    echo   Для HF_TOKEN нужно принять условия моделей:
    echo     https://huggingface.co/pyannote/speaker-diarization-community-1
    echo     https://huggingface.co/pyannote/segmentation-3.0
    echo.
) else (
    echo.
    echo   Файл .env уже существует, не трогаю.
    echo.
)

echo   Готово. Дальше: впишите ключи в .env и запустите run.bat
echo.
echo   Первый запуск дольше на несколько минут: качаются модели
echo   faster-whisper и pyannote (~1.5 ГБ, один раз).
echo.
pause
endlocal
