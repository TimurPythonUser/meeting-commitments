# syntax=docker/dockerfile:1.7
#
# Образ приложения «Реальные обязательства из созвона».
#
# Ключевое решение: ВЕСА МОДЕЛЕЙ СКАЧИВАЮТСЯ НА ЭТАПЕ СБОРКИ.
# Иначе первый же пользователь ждёт полторы минуты, пока с Hugging Face
# приедут faster-whisper и pyannote, и это выглядит как «сервис завис».
# Скачанное лежит в слое образа: контейнер стартует офлайн и отвечает сразу.
#
# Сборка (HF_TOKEN передаётся секретом, в образ не попадает):
#   export DOCKER_BUILDKIT=1
#   docker build --secret id=hf_token,env=HF_TOKEN -t meeting-commitments .
#
# Запуск:
#   docker run --rm -p 8000:8000 -e DEEPSEEK_API_KEY=... meeting-commitments
#
# HF_TOKEN нужен и в рантайме. Веса лежат внутри образа и по сети не
# качаются (HF_HUB_OFFLINE=1), но app/vendor/transcriber.py требует токен
# перед загрузкой пайплайна — без него диаризация падает с RuntimeError.

# ── Этап 1: зависимости и веса ───────────────────────────────────────────────

FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# git нужен pyannote при разрешении ревизий моделей, build-essential — для
# сборки колёс, которых нет под 3.13.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# torch ставим ОТДЕЛЬНО с CPU-индекса. Дефолтный torch с PyPI тянет за собой
# CUDA-рантайм: это +5 ГБ образа, бесполезных на CPU-инстансе.
RUN pip install --index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.0" "torchaudio>=2.0"

COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# ── Веса моделей ─────────────────────────────────────────────────────────────

ENV HF_HOME=/opt/models \
    HF_HUB_DISABLE_TELEMETRY=1

# --secret вместо --build-arg: build-arg остаётся в истории слоёв, и токен
# можно вытащить из готового образа командой `docker history`.
#
# Веса качаются здесь, а не при первом запросе. Скрипт лежит прямо в
# Dockerfile, чтобы вся сборка была описана одним файлом.
RUN --mount=type=secret,id=hf_token,required=false \
    HF_TOKEN="$(cat /run/secrets/hf_token 2>/dev/null || echo "${HF_TOKEN:-}")" \
    python <<'PY'
import os
import sys

# 1. faster-whisper small. Скачивание происходит в конструкторе: модель
#    приезжает в HF_HOME и остаётся в слое образа.
from faster_whisper import WhisperModel

size = os.environ.get("WHISPER_MODEL", "small")
print(f"[build] faster-whisper {size}…", flush=True)
WhisperModel(size, device="cpu", compute_type="int8")
print("[build] faster-whisper готов", flush=True)

# 2. pyannote. Модель гейтед: нужен токен и принятые условия на Hugging Face.
#    Без токена сборка НЕ падает — образ остаётся рабочим для транскрипции.
#    Это осознанный компромисс: иначе образ не собрать в CI без секрета.
#    Но в рантайме стоит HF_HUB_OFFLINE=1, так что диаризация в таком образе
#    просто не заработает: чинится пересборкой, а не на проде.
token = os.environ.get("HF_TOKEN") or None
model_id = os.environ.get("DIARIZATION_MODEL", "pyannote/speaker-diarization-community-1")
if not token:
    print("[build] ВНИМАНИЕ: HF_TOKEN не передан — веса pyannote НЕ скачаны.", flush=True)
    print("[build] Диаризации в этом образе не будет: пересобери с", flush=True)
    print("[build] --secret id=hf_token,env=HF_TOKEN.", flush=True)
    sys.exit(0)

print(f"[build] pyannote {model_id}…", flush=True)
try:
    from pyannote.audio import Pipeline

    # pyannote.audio 4.x принимает token=, 3.x — use_auth_token=. Та же
    # развилка стоит в app/vendor/transcriber.py; здесь её сперва забыли,
    # и веса молча не скачивались — образ собирался без диаризации.
    try:
        Pipeline.from_pretrained(model_id, token=token)
    except TypeError:
        Pipeline.from_pretrained(model_id, use_auth_token=token)
    print("[build] pyannote готов", flush=True)
except Exception as exc:  # noqa: BLE001
    # Чаще всего — не приняты условия модели на странице Hugging Face.
    print(f"[build] pyannote НЕ скачан: {type(exc).__name__}: {exc}", flush=True)
    sys.exit(0)
PY

# ── Этап 2: рантайм ──────────────────────────────────────────────────────────

FROM python:3.13-slim AS runtime

# ffmpeg обязателен: с него начинается конвейер.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/models \
    HF_HUB_OFFLINE=1 \
    OMP_NUM_THREADS=4 \
    WHISPER_MODEL=small

COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /opt/models /opt/models

WORKDIR /app
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Непривилегированный пользователь: процессу незачем быть root.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app /opt/models
USER appuser

EXPOSE 8000

# Проверяет ровно то, что должно отвечать: ручку API, а не факт живости порта.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

# Один воркер: модели занимают ~3.5 ГБ, второй процесс удвоит память и не
# ускорит ничего — узкое место CPU, а не обработка HTTP.
CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
