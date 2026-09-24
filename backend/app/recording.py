"""Дата записи: точка отсчёта, от которой раскрываются «завтра» и «до четверга».

Без неё относительный срок остаётся текстом, и в карточке висит бесполезное
«дату не названо». А дата почти всегда известна — просто не из речи, а из
файла: телефоны, Zoom, Teams и диктофоны пишут в контейнер creation_time,
и он переживает копирование.

Цепочка источников, от надёжного к слабому:

    file_metadata     creation_time из контейнера (.m4a/.mp4/.mov/.mkv).
                      Настоящая дата записи: позавчерашний файл так и
                      определится позавчерашним.
    client_file_date  дата файла на диске, её присылает браузер
                      (file.lastModified). Обычно верна, но копирование
                      файла её сбивает.
    manual            дату указал человек. Главнее всего остального.
    unknown           ничего не нашлось. Даты НЕТ — и мы так и говорим.
                      Подставлять день загрузки нельзя: для старой записи
                      это враньё, а «завтра» от выдуманной точки отсчёта
                      указало бы на случайный день.

Важно: дату считает код из метаданных, а не модель из воздуха. Защита от
галлюцинаций на месте — LLM получает готовую точку отсчёта и только
прибавляет к ней «завтра», а sanity_check_deadlines() проверяет результат.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from dataclasses import dataclass
from typing import Literal

from .schemas import ExtractionResult
from .vendor.transcriber import _ffmpeg_bin

DateSource = Literal["file_metadata", "client_file_date", "manual", "unknown"]

# Теги, в которых разные программы держат дату записи. Порядок — по доверию.
_DATE_TAGS = (
    "creation_time",                      # mp4/m4a/mov/mkv — самый частый
    "com.apple.quicktime.creationdate",   # iPhone
    "date",                               # часть кодировщиков
    "ICRD",                               # INFO-чанк в wav
    "originiation_date",
    "origination_date",                   # BWF-wav с диктофонов
)

# Дальше этого в прошлое дата записи уехать не может — значит, тег мусорный.
_MIN_PLAUSIBLE_YEAR = 2000


def _ffprobe_bin() -> str:
    """ffprobe лежит рядом с ffmpeg, отдельной переменной для него не заводим."""
    ffmpeg = _ffmpeg_bin()
    if ffmpeg.lower().endswith("ffmpeg.exe"):
        return ffmpeg[: -len("ffmpeg.exe")] + "ffprobe.exe"
    if ffmpeg.lower().endswith("ffmpeg"):
        return ffmpeg[: -len("ffmpeg")] + "ffprobe"
    return "ffprobe"


@dataclass
class RecordingDate:
    """Точка отсчёта и честный рассказ о том, откуда она взялась."""

    date: str | None                 # ISO YYYY-MM-DD
    source: DateSource
    # Человекочитаемое пояснение для карточки — его показывает фронт.
    detail: str

    @property
    def weekday(self) -> str | None:
        """День недели словом — без него модель путается в «до четверга»."""
        parsed = parse_iso_date(self.date)
        return parsed.strftime("%A") if parsed else None


def parse_iso_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _normalize_tag(raw: str) -> str | None:
    """Значение тега -> YYYY-MM-DD, если оно вообще похоже на дату."""
    text = (raw or "").strip()
    if not text:
        return None
    # creation_time приходит как 2026-09-21T14:32:00.000000Z, ICRD — как
    # 2026-09-21, apple — с таймзоной вида +03:00. Первых десяти символов
    # хватает во всех трёх случаях.
    parsed = parse_iso_date(text.replace("/", "-"))
    if parsed is None or parsed.year < _MIN_PLAUSIBLE_YEAR:
        return None
    # Дата записи из будущего — сбитые часы на устройстве, верить нельзя.
    if parsed > dt.date.today():
        return None
    return parsed.isoformat()


def probe_file_date(path: str) -> str | None:
    """Дата записи из метаданных файла. None — тегов нет или они мусорные."""
    cmd = [
        _ffprobe_bin(), "-v", "error",
        "-show_entries", "format_tags:stream_tags",
        "-of", "json", path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

    blocks = [(data.get("format") or {}).get("tags") or {}]
    blocks += [(s.get("tags") or {}) for s in (data.get("streams") or [])]

    for tag in _DATE_TAGS:
        for block in blocks:
            for key, value in block.items():
                if key.lower() == tag.lower():
                    normalized = _normalize_tag(str(value))
                    if normalized:
                        return normalized
    return None


def resolve_recording_date(
    audio_path: str,
    manual_date: str | None = None,
    client_file_date: str | None = None,
) -> RecordingDate:
    """Определить точку отсчёта по всей цепочке источников."""
    manual = _normalize_tag(manual_date or "")
    if manual:
        return RecordingDate(
            date=manual,
            source="manual",
            detail=f"Дату запису вказано вручну: {manual}",
        )

    from_file = probe_file_date(audio_path)
    if from_file:
        return RecordingDate(
            date=from_file,
            source="file_metadata",
            detail=f"Дату запису взято з метаданих файлу: {from_file}",
        )

    from_client = _normalize_tag(client_file_date or "")
    if from_client:
        return RecordingDate(
            date=from_client,
            source="client_file_date",
            detail=f"Дату запису взято з дати файлу на диску: {from_client}",
        )

    # Дату не нашли — значит, её нет. День загрузки подставлять нельзя:
    # относительные сроки посчитались бы от выдуманной точки отсчёта, и
    # «завтра» указало бы на день, к разговору отношения не имеющий.
    return RecordingDate(
        date=None,
        source="unknown",
        detail=(
            "Дати запису не знайдено: у файлі її немає, у розмові не називали, "
            "вручну не вказали. Відносні строки лишаються текстом, як прозвучали."
        ),
    )


def sanity_check_deadlines(result: ExtractionResult, anchor: RecordingDate) -> int:
    """Снять даты, которые не сходятся с точкой отсчёта.

    Арифметику делает модель, а модель ошибается: то год не тот подставит,
    то посчитает «до четверга» назад. Срок раньше самой записи — заведомая
    ошибка, такую дату лучше снять и честно оставить текст, чем показать
    правдоподобное враньё.

    Возвращает число снятых дат.
    """
    base = parse_iso_date(anchor.date)
    if base is None:
        return 0

    dropped = 0
    for item in result.commitments:
        deadline = item.deadline
        if deadline is None or not deadline.resolved_date:
            continue
        parsed = parse_iso_date(deadline.resolved_date)
        if parsed is None or parsed < base:
            was = deadline.resolved_date
            deadline.resolved_date = None
            deadline.resolution_status = "relative_unresolved"
            deadline.note = (
                f"Модель порахувала дату {was}, але вона не сходиться з датою "
                f"запису {anchor.date} — знято, лишаємо строк текстом."
            )
            dropped += 1
    return dropped
