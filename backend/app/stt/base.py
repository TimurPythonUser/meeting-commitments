"""Интерфейс распознавания речи: что бэкенд ждёт от любого STT-провайдера.

Смысл слоя — отвязать пайплайн от faster-whisper. Локальный провайдер
(local_whisper.py) сегодня единственный, но заменить его на облачный
(Whisper API, Deepgram) можно, не трогая extract/verify/jobs: им нужен
только list[Utterance].
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

# Колбэк прогресса: (этап, сделано, всего). done/total в секундах аудио либо
# в шагах — фронт рисует полосу, если оба не None, иначе только подпись.
ProgressFn = Callable[[str, float | None, float | None], None]


@dataclass
class Utterance:
    """Одна реплика: кусок текста с таймкодом и спикером.

    Именно её ест LLM, и по ней же verify.py
    пересчитывает таймкоды найденных цитат.
    """

    index: int
    start: float      # сек от начала записи
    end: float
    speaker: str      # "SPEAKER_00" / "SPEAKER_01"
    text: str


@dataclass
class SttResult:
    utterances: list[Utterance]
    language: str
    duration_sec: float
    # Тайминги этапов — уходят в metrics.timing_sec как есть.
    timing_sec: dict[str, float]


@runtime_checkable
class SttProvider(Protocol):
    """Контракт провайдера распознавания."""

    def warmup(self) -> None:
        """Прогреть модели заранее, чтобы первый запрос не ждал загрузку."""
        ...

    def set_progress(self, fn: ProgressFn) -> None:
        """Подписать вызывающего на прогресс. Троттлить обязан подписчик."""
        ...

    def transcribe(self, media_path: str, num_speakers: int | None = 2) -> SttResult:
        """Файл (аудио или видео) -> реплики с таймкодами и спикерами."""
        ...
