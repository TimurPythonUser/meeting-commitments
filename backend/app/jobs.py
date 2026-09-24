"""Очередь задач: приняли файл -> обсчитали в фоне -> отдали результат.

Хранилище in-memory и воркер ровно один: три минуты аудио на CPU грузят
машину целиком, и параллельная вторая задача не ускорит ничего, а только
отнимет ядра у первой. Очередь честная — FIFO, лишние ждут.

Перезапуск сервера обнуляет задачи. Для демо этого достаточно; переезд на
Redis/БД затронет только этот файл.
"""

from __future__ import annotations

import os
import queue
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field

from .extract import extract_commitments
from .llm.deepseek import DeepSeekClient
from .metrics import MetricsCollector
from .recording import resolve_recording_date
from .schemas import (
    JobResult,
    JobStatus,
    Progress,
    RecordingDateInfo,
    TranscriptLine,
)
from .stt.base import SttProvider
from .stt.local_whisper import LocalWhisperStt
from .verify import verify_result

# Прогресс от STT прилетает десятки раз в секунду. Фронт опрашивает раз в
# 1.5 сек — чаще, чем раз в полсекунды, обновлять запись смысла нет.
PROGRESS_THROTTLE_SEC = 0.5

# Сколько держим загруженный файл после обработки. Сразу удалять нельзя:
# плеер на экране результата тянет аудио с сервера, а вкладку могут не
# закрывать часами. Сутки покрывают рабочий сеанс с запасом.
JOB_TTL_SEC = float(os.environ.get("JOB_TTL_HOURS", "24")) * 3600


@dataclass
class Job:
    job_id: str
    audio_path: str
    filename: str
    # Дата записи, если её прислали с формой загрузки: «recorded_at» —
    # указанная вручную, «client_file_date» — дата файла из браузера.
    recorded_at: str | None = None
    client_file_date: str | None = None
    status: JobStatus = "queued"
    progress: Progress = field(default_factory=Progress)
    result: JobResult | None = None
    error: str | None = None
    _last_tick: float = 0.0


class JobQueue:
    """Очередь на одну задачу + фоновый воркер."""

    def __init__(self, stt: SttProvider | None = None, workdir: str | None = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stt = stt or LocalWhisperStt()
        self._llm: DeepSeekClient | None = None
        # Прогрев идёт в отдельном потоке, чтобы сервер отвечал сразу. Если
        # задача прилетит раньше, чем модели догрузились, воркер подождёт
        # здесь, а не полезет грузить их второй раз параллельно.
        self._model_lock = threading.Lock()
        self.ready = threading.Event()
        self._workdir = workdir or os.path.join(tempfile.gettempdir(), "commitments_jobs")
        os.makedirs(self._workdir, exist_ok=True)

    # -- жизненный цикл ------------------------------------------------------

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, name="job-worker", daemon=True)
        self._worker.start()

    def warmup(self) -> None:
        """Прогреть модели. Зовётся на старте сервера, до первого запроса."""
        with self._model_lock:
            self._stt.warmup()
            # Клиент LLM дешёвый, но пусть ошибка «нет ключа» вылезет на
            # старте, а не через две минуты обсчёта.
            self._llm = DeepSeekClient()
        self.ready.set()

    # -- API очереди ---------------------------------------------------------

    def submit(
        self,
        data: bytes,
        filename: str,
        recorded_at: str | None = None,
        client_file_date: str | None = None,
    ) -> str:
        """Сохранить загруженный файл и поставить задачу в очередь."""
        self._sweep()
        job_id = uuid.uuid4().hex
        ext = os.path.splitext(filename)[1].lower() or ".wav"
        path = os.path.join(self._workdir, f"{job_id}{ext}")
        with open(path, "wb") as f:
            f.write(data)

        job = Job(
            job_id=job_id,
            audio_path=path,
            filename=filename,
            recorded_at=recorded_at,
            client_file_date=client_file_date,
        )
        job.progress = Progress(stage="У черзі")
        with self._lock:
            self._jobs[job_id] = job
        self._queue.put(job_id)
        self.start()
        return job_id

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _sweep(self) -> None:
        """Убрать давние загрузки: иначе temp растёт до бесконечности.

        Чистим по возрасту файла, а не по числу задач: свежая задача может
        ещё обрабатываться, и удалять её файл посреди распознавания нельзя.
        Записи в памяти уходят вместе с файлами — в них лежит транскрипт.
        """
        cutoff = time.time() - JOB_TTL_SEC
        removed: list[str] = []
        try:
            with os.scandir(self._workdir) as entries:
                for entry in entries:
                    try:
                        if entry.is_file() and entry.stat().st_mtime < cutoff:
                            os.remove(entry.path)
                            removed.append(os.path.splitext(entry.name)[0])
                    except OSError:
                        continue          # занят другим процессом или уже удалён
        except OSError:
            return

        if removed:
            with self._lock:
                for job_id in removed:
                    self._jobs.pop(job_id, None)

    def snapshot(self, job_id: str) -> JobResult | None:
        """Тело ответа GET /api/jobs/{id} на текущий момент."""
        job = self.get(job_id)
        if job is None:
            return None
        if job.status == "done" and job.result is not None:
            return job.result
        return JobResult(
            job_id=job.job_id,
            status=job.status,
            progress=job.progress,
            error=job.error,
            audio_url=f"/api/jobs/{job.job_id}/audio",
        )

    # -- воркер --------------------------------------------------------------

    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self.get(job_id)
            if job is None:
                continue
            try:
                job.status = "processing"
                job.result = self._process(job)
                job.status = "done"
                job.progress = Progress(stage="Готово", done=1.0, total=1.0)
            except Exception as exc:                      # noqa: BLE001
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.progress = Progress(stage="Помилка")
                traceback.print_exc()
            finally:
                self._queue.task_done()

    def _tick(self, job: Job):
        """Колбэк прогресса для STT, с троттлингом."""

        def report(stage: str, done: float | None = None, total: float | None = None) -> None:
            now = time.monotonic()
            if now - job._last_tick < PROGRESS_THROTTLE_SEC and stage == job.progress.stage:
                return
            job._last_tick = now
            job.progress = Progress(stage=stage, done=done, total=total)

        return report

    def _process(self, job: Job) -> JobResult:
        metrics = MetricsCollector()
        report = self._tick(job)

        # 1. Распознавание + диаризация. Тайминги ffmpeg/stt/diarization
        # меряет сам провайдер — он один знает, где кончается этап.
        self._stt.set_progress(report)
        if not self.ready.is_set():
            report("Завантажую моделі", None, None)
        with self._model_lock:
            stt = self._stt.transcribe(job.audio_path, num_speakers=2)
        metrics.add_timing(stt.timing_sec)
        metrics.audio_duration_sec = stt.duration_sec
        metrics.stt_model = getattr(self._stt, "model_size", "small")

        if not stt.utterances:
            report("Мовлення не розпізнано", 1.0, 1.0)
            return JobResult(
                job_id=job.job_id,
                status="done",
                progress=Progress(stage="Мовлення не розпізнано", done=1.0, total=1.0),
                audio_url=f"/api/jobs/{job.job_id}/audio",
                duration_sec=stt.duration_sec,
                language=stt.language,
                metrics=metrics.build(),
            )

        # 2. Точка отсчёта для относительных сроков. Читается из файла,
        # а не из речи — «завтра» без неё раскрыть нечем.
        anchor = resolve_recording_date(
            job.audio_path,
            manual_date=job.recorded_at,
            client_file_date=job.client_file_date,
        )
        metrics.extra_assumptions.append(anchor.detail)

        # 3. Извлечение обязательств.
        report("Витягую зобовʼязання", None, None)
        if self._llm is None:
            self._llm = DeepSeekClient()
        with metrics.stage("llm"):
            run = extract_commitments(
                stt.utterances, client=self._llm, anchor=anchor
            )
        metrics.add_llm_usage(
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            cached_input_tokens=run.cached_input_tokens,
            reasoning_tokens=run.reasoning_tokens,
            retries=run.retries,
            model=run.model,
        )

        if run.dates_dropped:
            metrics.extra_assumptions.append(
                f"дат знято через розбіжність із датою запису: {run.dates_dropped}"
            )

        # 4. Проверка цитат. Всё, что не подтвердилось, из активных уходит.
        report("Перевіряю цитати", None, None)
        with metrics.stage("validation"):
            verified = verify_result(run.result, stt.utterances)

        if verified.dropped:
            metrics.extra_assumptions.append(
                f"пунктів відсіяно валідатором цитат: {verified.dropped} "
                "(цитату не знайдено в транскрипті)"
            )

        return JobResult(
            job_id=job.job_id,
            status="done",
            progress=Progress(stage="Готово", done=1.0, total=1.0),
            audio_url=f"/api/jobs/{job.job_id}/audio",
            duration_sec=stt.duration_sec,
            language=stt.language,
            recording_date=RecordingDateInfo(
                date=anchor.date,
                source=anchor.source,
                detail=anchor.detail,
            ),
            participants=verified.participants,
            commitments=verified.commitments,
            unverified_commitments=verified.unverified_commitments,
            open_questions=verified.open_questions,
            clarifications_needed=verified.clarifications_needed,
            metrics=metrics.build(),
            transcript=[
                TranscriptLine(
                    index=u.index, start=u.start, end=u.end, speaker=u.speaker, text=u.text
                )
                for u in stt.utterances
            ],
        )
