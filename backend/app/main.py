"""FastAPI: три роута из контракта, отдача аудио с Range и статика фронта.

Запуск:
    .venv\\Scripts\\python.exe -m uvicorn app.main:app --app-dir backend --port 8000
"""

from __future__ import annotations

import mimetypes
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .jobs import JobQueue
from .presence import BrowserPresence
from .schemas import JobCreated, JobResult
from .vendor.transcriber import SUPPORTED_EXT, check_ffmpeg

# Три минуты аудио — это единицы мегабайт. Лимит отсекает случайную заливку
# фильма, из-за которой сервер молотил бы полчаса.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "100")) * 1024 * 1024

# Размер куска при отдаче аудио по Range.
CHUNK_SIZE = 256 * 1024

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "frontend")

jobs = JobQueue()
presence = BrowserPresence()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Прогрев моделей на старте.

    Грузим в фоновом потоке: faster-whisper и pyannote поднимаются десятки
    секунд, и блокировать этим старт сервера незачем — статика и /api/health
    должны отвечать сразу. Задача, пришедшая раньше готовности, подождёт
    внутри воркера.
    """
    if not check_ffmpeg():
        print("[main] ВНИМАНИЕ: ffmpeg не найден в PATH — распознавание упадёт.", flush=True)

    jobs.start()

    def _warm() -> None:
        try:
            jobs.warmup()
            print("[main] модели прогреты, готов к работе", flush=True)
        except Exception as exc:                          # noqa: BLE001
            print(f"[main] прогрев не удался: {type(exc).__name__}: {exc}", flush=True)

    threading.Thread(target=_warm, name="warmup", daemon=True).start()

    # Сторож присутствия страницы. Пока браузер не прислал первое
    # сердцебиение, он молчит, так что curl и тесты сервер не гасят.
    presence.start()
    try:
        yield
    finally:
        presence.stop()


app = FastAPI(
    title="Зобовʼязання з робочого дзвінка",
    description="Аудіо робочого дзвінка -> перелік реальних домовленостей "
                "з дослівними цитатами й таймкодами.",
    version="1.0.0",
    lifespan=lifespan,
)

# Фронт отдаётся этим же сервером, значит запросы идут с того же origin
# и CORS не нужен. Список включается через CORS_ORIGINS — только если фронт
# поднимают отдельно. Открывать всем по умолчанию в проде незачем.
_cors_origins = [
    o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ── Роуты ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health() -> dict:
    """Живость сервера и готовность моделей — фронту, чтобы не пугать юзера."""
    return {
        "status": "ok",
        "models_ready": jobs.ready.is_set(),
        "ffmpeg": check_ffmpeg(),
    }


@app.post("/api/alive", status_code=204)
async def alive(request: Request) -> Response:
    """Сердцебиение открытой страницы. Фронт шлёт его раз в 5 секунд."""
    presence.touch(request.client.host if request.client else None)
    return Response(status_code=204)


@app.post("/api/bye", status_code=204)
async def bye(request: Request) -> Response:
    """Маячок «страница ушла» с pagehide.

    Не выключение: pagehide прилетает и на обычной перезагрузке. Сторож
    выжидает и гасит сервер, только если вкладка не вернулась.
    """
    presence.farewell(request.client.host if request.client else None)
    return Response(status_code=204)


@app.post("/api/jobs", status_code=202, response_model=JobCreated)
async def create_job(
    file: UploadFile = File(...),
    recorded_at: str | None = Form(default=None),
    client_file_date: str | None = Form(default=None),
) -> JobCreated:
    """Принять файл и поставить в очередь. Обработка асинхронная.

    Оба поля даты необязательные и отвечают за точку отсчёта сроков:
        recorded_at       — дату записи указал человек, главнее всего;
        client_file_date  — дата файла на диске, её знает браузер
                            (file.lastModified), ISO YYYY-MM-DD.
    Ничего не прислали — дата берётся из метаданных файла, а если и там
    пусто, принимается день загрузки и помечается как допущение.
    """
    filename = file.filename or "upload.wav"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SUPPORTED_EXT:
        raise HTTPException(
            status_code=415,
            detail=f"Формат {ext or '?'} не підтримується. "
                   f"Можна: {', '.join(sorted(SUPPORTED_EXT))}",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Порожній файл.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Файл більший за {MAX_UPLOAD_BYTES // 1024 // 1024} МБ.",
        )

    return JobCreated(
        job_id=jobs.submit(
            data,
            filename,
            recorded_at=recorded_at,
            client_file_date=client_file_date,
        )
    )


@app.get("/api/jobs/{job_id}", response_model=JobResult)
async def get_job(job_id: str) -> JobResult:
    """Статус, прогресс и — когда готово — результат целиком."""
    snapshot = jobs.snapshot(job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Задачу не знайдено.")
    return snapshot


@app.get("/api/jobs/{job_id}/audio")
async def get_audio(job_id: str, request: Request) -> Response:
    """Исходное аудио для плеера.

    С поддержкой Range: без неё браузер не даёт перемотку, а весь смысл
    карточки — прыгнуть на таймкод цитаты и послушать.
    """
    job = jobs.get(job_id)
    if job is None or not os.path.isfile(job.audio_path):
        raise HTTPException(status_code=404, detail="Аудіо не знайдено.")

    path = job.audio_path
    size = os.path.getsize(path)
    media_type = mimetypes.guess_type(job.filename)[0] or "application/octet-stream"
    common = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=3600",
    }

    range_header = request.headers.get("range") or request.headers.get("Range")
    if not range_header:
        return FileResponse(path, media_type=media_type, headers=common)

    start, end = _parse_range(range_header, size)
    if start is None:
        # Диапазон вне файла — по RFC отвечаем 416 и говорим реальный размер.
        return Response(
            status_code=416,
            headers={**common, "Content-Range": f"bytes */{size}"},
        )

    length = end - start + 1

    def stream():
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(CHUNK_SIZE, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    return StreamingResponse(
        stream(),
        status_code=206,
        media_type=media_type,
        headers={
            **common,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
        },
    )


def _parse_range(header: str, size: int) -> tuple[int | None, int]:
    """«bytes=0-1023» -> (0, 1023). (None, 0) — диапазон невалиден.

    Плееру нужны две формы: «с байта N до конца» и «последние N байт».
    Множественные диапазоны браузеры для аудио не шлют, поддержки не делаем.
    """
    try:
        units, _, spec = header.partition("=")
        if units.strip().lower() != "bytes":
            return None, 0
        first, _, last = spec.split(",")[0].strip().partition("-")
        if not first:                       # bytes=-500 — хвост файла
            length = int(last)
            if length <= 0:
                return None, 0
            start = max(0, size - length)
            return start, size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except (ValueError, TypeError):
        return None, 0

    end = min(end, size - 1)
    if start > end or start >= size:
        return None, 0
    return start, end


# ── Статика фронта ───────────────────────────────────────────────────────────
# Монтируется последней: все /api/* объявлены выше и разбираются раньше.
# Каталога нет — падаем на старте, это ошибка сборки, а не штатный режим.

app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
