"""
Pydantic-модели ответа API. Форма ответа задаётся здесь и больше нигде.

Эти же модели используются для валидации сырого ответа LLM: модель обязана
вернуть JSON ровно такой формы, иначе extract.py уходит в ретрай.

ВАЖНО для окон C и D: любая правка этого файла — ломающая. Перечисления
(status / resolution_status / lifecycle event) заданы дословно по контракту
и менять их нельзя.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ── Перечисления (дословно по контракту) ─────────────────────────────────────

CommitmentStatus = Literal["agreed", "proposed_not_accepted", "cancelled"]

DeadlineResolution = Literal["resolved", "relative_unresolved", "none"]

LifecycleEvent = Literal[
    "proposed",
    "accepted",
    "rejected",
    "owner_assigned",
    "deadline_set",
    "deadline_corrected",
    "cancelled",
]

JobStatus = Literal["queued", "processing", "done", "error"]

# Откуда взялась дата записи. Порядок — по убыванию доверия, подробности
# в app/recording.py.
DateSource = Literal["file_metadata", "client_file_date", "manual", "unknown"]


# ── Базовая модель ───────────────────────────────────────────────────────────

class _Base(BaseModel):
    # Лишние поля от LLM молча выбрасываем, а не падаем: модель любит
    # дорисовывать «пояснения». На валидность ответа это не влияет.
    model_config = ConfigDict(extra="ignore")


# ── Evidence: единый вид всюду ───────────────────────────────────────────────

class Evidence(_Base):
    """Дословная цитата из транскрипта с таймкодом.

    start/end модель заполняет наугад — им верить нельзя. Реальные значения
    проставляет verify.py из найденной позиции цитаты, поэтому здесь у них
    есть дефолт: сырой ответ LLM без таймкодов всё равно валиден.
    """

    text: str
    start: float = 0.0
    end: float = 0.0
    speaker_id: str | None = None


# ── Участники ────────────────────────────────────────────────────────────────

class Participant(_Base):
    speaker_id: str
    name: str | None = None
    # Чем подтверждается имя: реплика, где человек представился или его назвали.
    name_evidence: Evidence | None = None


# ── Части обязательства ──────────────────────────────────────────────────────

class Owner(_Base):
    # Имя может быть неизвестно: человек взял задачу на себя, но по имени его
    # в записи так и не назвали. Владелец при этом есть — он опознан по голосу.
    name: str | None = None
    speaker_id: str | None = None
    evidence: Evidence | None = None


class Deadline(_Base):
    # Дословно, как прозвучало: «до следующего вторника».
    raw_text: str | None = None
    # ISO-дата. Заполняется, только если она выводится из записи.
    resolved_date: str | None = None
    resolution_status: DeadlineResolution = "none"
    note: str | None = None
    evidence: Evidence | None = None


class LifecycleStep(_Base):
    event: LifecycleEvent
    evidence: Evidence | None = None


class Verification(_Base):
    """Результат работы verify.py. LLM это поле не заполняет."""

    quote_verified: bool = False
    match_score: float = 0.0
    # true, если таймкоды пересчитаны из найденной позиции цитаты.
    timestamps_recomputed: bool = False


class Commitment(_Base):
    id: str
    title: str
    status: CommitmentStatus
    owner: Owner | None = None
    # Почему владельца нет. Заполняется, только когда owner is null.
    owner_missing_reason: str | None = None
    deadline: Deadline | None = None
    # Полная траектория: предложено → принято → срок поправлен → отменено.
    lifecycle: list[LifecycleStep] = Field(default_factory=list)
    # Главная цитата пункта — её и проверяет валидатор.
    primary_evidence: Evidence | None = None
    verification: Verification = Field(default_factory=Verification)

    @model_validator(mode="after")
    def _collapse_empty_owner(self) -> "Commitment":
        """owner без имени и без speaker_id — это отсутствие владельца.

        Модель иногда возвращает пустую болванку {"name": null} вместо null.
        Считать такое владельцем нельзя: фронт покажет карточку «ответственный
        есть», хотя в записи его не назвали.
        """
        if self.owner is not None and not (self.owner.name or self.owner.speaker_id):
            self.owner = None
            if not self.owner_missing_reason:
                self.owner_missing_reason = "Відповідального в записі не назвали."
        return self


class RecordingDateInfo(_Base):
    """Точка отсчёта для относительных сроков и её происхождение.

    Фронт обязан показывать source/detail рядом с датой: «завтра» имеет
    смысл только вместе с тем, от какого дня оно посчитано и откуда этот
    день взялся.

    date = null и source = "unknown" — даты нет. Это нормальный исход, а не
    ошибка: все сроки в таком прогоне остаются текстом.
    """

    date: str | None = None            # ISO YYYY-MM-DD
    source: DateSource = "unknown"
    detail: str = ""


class OpenQuestion(_Base):
    id: str
    question: str
    evidence: Evidence | None = None


class Clarification(_Base):
    reason: str
    evidence: Evidence | None = None


# ── Транскрипт ───────────────────────────────────────────────────────────────

class TranscriptLine(_Base):
    index: int
    start: float
    end: float
    speaker: str
    text: str


# ── Метрики ──────────────────────────────────────────────────────────────────

class TimingSec(_Base):
    ffmpeg: float = 0.0
    stt: float = 0.0
    diarization: float = 0.0
    llm: float = 0.0
    validation: float = 0.0
    total: float = 0.0


class LlmMetrics(_Base):
    model: str = "deepseek-flash"
    input_tokens: int = 0
    output_tokens: int = 0
    # Сколько раз пришлось перезапросить из-за невалидного JSON.
    retries: int = 0


class CostUsd(_Base):
    stt: float = 0.0
    llm: float = 0.0
    total: float = 0.0
    per_audio_minute: float = 0.0

    # Хостинг. В total НЕ входит — он там отдельной строкой по заданию.
    hosting_name: str = ""
    hosting_per_month: float = 0.0
    # Аренда помесячная, поэтому разносим её по прогонам:
    # $/мес ÷ число прогонов в месяц.
    hosting_per_run: float = 0.0
    runs_per_month: int = 0
    total_with_hosting: float = 0.0
    per_audio_minute_with_hosting: float = 0.0


class Metrics(_Base):
    timing_sec: TimingSec = Field(default_factory=TimingSec)
    llm: LlmMetrics = Field(default_factory=LlmMetrics)
    cost_usd: CostUsd = Field(default_factory=CostUsd)
    assumptions: list[str] = Field(default_factory=list)


# ── Ответ LLM (до проверки цитат) ────────────────────────────────────────────

class ExtractionResult(_Base):
    """Ровно то, что обязана вернуть модель. Схема промпта в extract.py."""

    participants: list[Participant] = Field(default_factory=list)
    commitments: list[Commitment] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    clarifications_needed: list[Clarification] = Field(default_factory=list)


# ── HTTP ─────────────────────────────────────────────────────────────────────

class Progress(_Base):
    stage: str = "У черзі"
    done: float | None = None
    total: float | None = None


class JobCreated(_Base):
    job_id: str


class JobResult(_Base):
    """Тело ответа GET /api/jobs/{id}.

    Пока status != "done" заполнены только job_id, status и progress.
    """

    job_id: str
    status: JobStatus
    progress: Progress = Field(default_factory=Progress)
    error: str | None = None

    audio_url: str | None = None
    duration_sec: float | None = None
    language: str | None = None
    # От какого дня раскрывались «завтра» и «до четверга».
    recording_date: RecordingDateInfo | None = None

    participants: list[Participant] = Field(default_factory=list)
    commitments: list[Commitment] = Field(default_factory=list)
    # Пункты, чью цитату не удалось найти в транскрипте. В активный список
    # они не попадают — фронт показывает их отдельной секцией «не подтверждено».
    unverified_commitments: list[Commitment] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    clarifications_needed: list[Clarification] = Field(default_factory=list)

    metrics: Metrics | None = None
    transcript: list[TranscriptLine] = Field(default_factory=list)
