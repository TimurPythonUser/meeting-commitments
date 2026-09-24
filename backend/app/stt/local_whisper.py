"""Локальный STT: faster-whisper + pyannote поверх переиспользованного ядра.

Тонкий адаптер над app/vendor/transcriber.py. Своей ML-логики здесь нет —
только сборка шагов в нужном порядке и перевод результата в list[Utterance]:

    extract_audio -> transcribe -> diarize_audio(num_speakers=2)
                  -> assign_speakers -> list[Utterance]

Переменная стоимость этого провайдера — $0 (всё крутится на своём CPU),
хостинг считается отдельной строкой в метриках.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
import wave

from ..vendor.transcriber import (
    DiarSegment,
    Segment,
    Transcriber,
    _overlap,
    check_ffmpeg,
    is_supported,
)
from .base import ProgressFn, SttResult, Utterance

# «small» — сознательный выбор ради скорости: 3 минуты русской речи на CPU
# large-v3 молотит в разы дольше, а на разборчивом созвоне выигрыша почти нет.
DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "small")

# В записях тест-сета ровно два собеседника — говорим об этом pyannote прямо,
# иначе он иногда дробит одного человека на два голоса.
DEFAULT_NUM_SPEAKERS = 2

# Язык записей. Переопределяется через STT_LANGUAGE, если понадобится
# прогнать разговор на другом языке.
DEFAULT_LANGUAGE = os.environ.get("STT_LANGUAGE", "uk")


# Соседние куски одного спикера склеиваем, если пауза меньше этого — иначе
# реплика рассыпается на обрывки по границам 30-секундных окон whisper.
MERGE_GAP_SEC = 0.8
# Но не длиннее этого: длинная реплика делает таймкод цитаты размытым.
MERGE_MAX_SEC = 20.0


def _wav_duration(wav_path: str) -> float:
    """Длительность 16 kHz mono wav, который сделал ffmpeg."""
    try:
        with contextlib.closing(wave.open(wav_path, "rb")) as f:
            return f.getnframes() / float(f.getframerate() or 16000)
    except Exception:
        return 0.0


def _word_speaker(start: float, end: float, diar: list[DiarSegment]) -> str:
    """Чей это слово — по максимальному перекрытию с разметкой pyannote."""
    best, best_ov = "", 0.0
    for d in diar:
        ov = _overlap(start, end, d.start, d.end)
        if ov > best_ov:
            best_ov, best = ov, d.speaker
    if best:
        return best
    # Слово попало в паузу между сегментами диаризации — берём ближайший.
    return min(
        diar, key=lambda d: min(abs(d.start - start), abs(d.end - end))
    ).speaker


def _smooth(labels: list[str]) -> list[str]:
    """Убрать одиночные перескоки спикера.

    Границы pyannote стоят приблизительно, и на стыке одно слово часто
    улетает к соседу. Слово, у которого оба соседа — другой спикер, почти
    наверняка размечено неверно.
    """
    fixed = list(labels)
    for i in range(1, len(labels) - 1):
        if labels[i - 1] == labels[i + 1] != labels[i]:
            fixed[i] = labels[i - 1]
    return fixed


def split_by_speaker(segments: list[Segment], diar: list[DiarSegment]) -> list[Segment]:
    """Разрезать сегменты whisper по сменам говорящего, опираясь на слова.

    Зачем: faster-whisper отдаёт куски до 30 секунд, и в диалоге внутри
    одного такого куска успевают высказаться оба. Без разреза вся реплика
    получает один speaker_id, а таймкод цитаты уезжает на полминуты — то
    есть ломается ровно то, ради чего проект и делается.

    Сегмент без пословных таймкодов возвращается как есть: пусть его
    разметит assign_speakers целиком, это лучше, чем ничего.
    """
    if not diar:
        return segments

    pieces: list[Segment] = []
    for seg in segments:
        if not seg.words:
            pieces.append(seg)
            continue

        labels = _smooth([_word_speaker(w[0], w[1], diar) for w in seg.words])

        current: list[tuple[float, float, str]] = []
        current_label = labels[0]
        for word, label in zip(seg.words, labels):
            if label != current_label and current:
                pieces.append(_piece(current, current_label))
                current = []
            current_label = label
            current.append(word)
        if current:
            pieces.append(_piece(current, current_label))

    return _merge_adjacent(pieces)


def _piece(words: list[tuple[float, float, str]], speaker: str) -> Segment:
    text = "".join(w[2] for w in words).strip()
    return Segment(start=words[0][0], end=words[-1][1], text=text, speaker=speaker)


def _merge_adjacent(pieces: list[Segment]) -> list[Segment]:
    """Склеить подряд идущие куски одного спикера, разорванные окном whisper."""
    merged: list[Segment] = []
    for piece in pieces:
        if not piece.text:
            continue
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.speaker == piece.speaker
            and piece.start - prev.end <= MERGE_GAP_SEC
            and piece.end - prev.start <= MERGE_MAX_SEC
        ):
            prev.text = f"{prev.text} {piece.text}".strip()
            prev.end = piece.end
        else:
            merged.append(piece)
    return merged


class LocalWhisperStt:
    """Реализация SttProvider на локальных моделях."""

    def __init__(
        self,
        model_size: str = DEFAULT_MODEL,
        language: str | None = DEFAULT_LANGUAGE,
        hf_token: str | None = None,
    ) -> None:
        self._engine = Transcriber(
            model_size=model_size,
            language=language,
            hf_token=hf_token or os.environ.get("HF_TOKEN"),
            diarize=True,
        )
        # Логи ядра в stdout сервера, а не в прогресс-бар.
        self._engine.set_logger(lambda msg: print(f"[stt] {msg}", flush=True))
        self.model_size = model_size
        self.language = language

    # -- прогресс ------------------------------------------------------------

    def set_progress(self, fn: ProgressFn) -> None:
        """Прокинуть колбэк прогресса наружу — из него питается полоса фронта.

        Ядро дёргает его часто (на каждом сегменте whisper и на каждом шаге
        pyannote); троттлит подписчик, не мы.
        """
        self._engine.set_progress(fn)

    # -- прогрев -------------------------------------------------------------

    def warmup(self) -> None:
        """Загрузить обе модели заранее — на старте сервера, не в запросе.

        Первая загрузка whisper+pyannote занимает десятки секунд (а при пустом
        кэше HF — ещё и скачивание). Без прогрева это время платит первый
        пользователь.
        """
        self._engine._load_whisper()
        if self._engine.hf_token:
            self._engine._load_diarizer()

    # -- основной проход -----------------------------------------------------

    def transcribe(
        self,
        media_path: str,
        num_speakers: int | None = DEFAULT_NUM_SPEAKERS,
    ) -> SttResult:
        if not os.path.isfile(media_path):
            raise FileNotFoundError(media_path)
        if not is_supported(media_path):
            raise ValueError(f"Формат не підтримується: {os.path.splitext(media_path)[1]}")
        if not check_ffmpeg():
            raise RuntimeError("ffmpeg не знайдено в PATH — розпізнавання неможливе.")

        timing: dict[str, float] = {"ffmpeg": 0.0, "stt": 0.0, "diarization": 0.0}

        with tempfile.TemporaryDirectory(prefix="stt_") as tmp:
            wav = os.path.join(tmp, "audio.wav")

            self._engine._tick("Готую аудіо", None, None)
            t0 = time.perf_counter()
            self._engine.extract_audio(media_path, wav)
            timing["ffmpeg"] = time.perf_counter() - t0
            duration = _wav_duration(wav)

            t0 = time.perf_counter()
            # Пословные таймкоды нужны, чтобы разрезать реплики по сменам
            # говорящего — см. split_by_speaker().
            segments, language = self._engine.transcribe(wav, word_timestamps=True)
            timing["stt"] = time.perf_counter() - t0

            if not segments:
                return SttResult([], language or (self.language or DEFAULT_LANGUAGE), duration, timing)

            # Диаризация — самый долгий этап, но без неё нет speaker_id,
            # а по контракту он нужен в каждом Evidence.
            t0 = time.perf_counter()
            diar = self._engine.diarize_audio(wav, num_speakers=num_speakers)
            timing["diarization"] = time.perf_counter() - t0

            if diar and len({d.speaker for d in diar}) > 1:
                # Сначала режем по сменам говорящего, потом метку каждому
                # куску ставит штатный assign_speakers ядра.
                segments = split_by_speaker(segments, diar)
                self._engine.assign_speakers(segments, diar)
            else:
                # Один голос на всю запись: диаризация ничего не даёт,
                # но поле speaker пустым оставлять нельзя.
                for seg in segments:
                    seg.speaker = "SPEAKER_00"

        segments.sort(key=lambda s: s.start)
        utterances = [
            Utterance(
                index=i,
                start=round(float(seg.start), 2),
                end=round(float(seg.end), 2),
                speaker=seg.speaker or "SPEAKER_00",
                text=seg.text.strip(),
            )
            for i, seg in enumerate(segments)
        ]

        if not duration and utterances:
            duration = utterances[-1].end

        self._engine._tick("Розшифровка готова", duration, duration)
        return SttResult(
            utterances=utterances,
            language=language or (self.language or DEFAULT_LANGUAGE),
            duration_sec=round(duration, 2),
            timing_sec={k: round(v, 3) for k, v in timing.items()},
        )
