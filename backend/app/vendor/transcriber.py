r"""
ПЕРЕИСПОЛЬЗОВАННЫЙ КОМПОНЕНТ — не писался для этого проекта.

Источник:  C:\Users\timur\VSCodeProjects\TestClaudeCode\voice-recognition-bot\transcriber.py
           (телеграм-бот транскрипции, собственная разработка)
Скопирован: 2026-09-22
Автор оригинала: тот же, что и у этого проекта.

Что взято без изменений:
    is_supported(), is_video(), format_timestamp(), check_ffmpeg(), _overlap(),
    dataclass DiarSegment,
    Transcriber.__init__/_resolve_device/set_logger/set_progress/_tick,
    Transcriber._load_whisper(), _load_diarizer(), _load_waveform(),
    Transcriber.extract_audio(), diarize_annotations(), diarize_audio(),
    _to_diar_segments(), assign_speakers().

Что взято с правкой: dataclass Segment и Transcriber.transcribe() — см. ниже.

Что ВЫБРОШЕНО при копировании (этому проекту не нужно):
    * split_speakers() и весь блок нарезки аудио по голосам:
      _read_pcm(), _pad_timeline(), _vad_speech_mask(), _speaker_timeline(),
      _assemble(), _encode(), dataclass SpeakerTrack, tracks_to_markdown();
    * фильтр поддакиваний: BACKCHANNEL_WORDS, _normalize_words(),
      _load_bc_whisper(), _is_backchannel(), _drop_backchannels(),
      SPLIT_DROP_BACKCHANNELS и прочие SPLIT_*-настройки;
      здесь поддакивания НУЖНЫ — «да, беру» это согласие, а не мусор;
    * Markdown-вывод to_markdown()/_label_map() и полный проход process():
      слой stt/local_whisper.py собирает list[Utterance] сам;
    * CLI (_build_parser/main) и импорты argparse/sys/tempfile/re/field;
    * bot.py не копировался вообще.

Правки тела (обе помечены по месту комментарием «ИЗМЕНЕНО/ДОБАВЛЕНО»):
    * Segment получил поле words — пословные таймкоды;
    * transcribe() получил параметр word_timestamps=False.
      Без него faster-whisper отдаёт куски по 30 секунд, внутри которых
      говорят оба собеседника: один speaker_id на такой кусок делает
      таймкод цитаты бесполезным, а он здесь — половина ценности.
      По умолчанию выключен, то есть поведение оригинала сохранено.
Остальная логика не трогалась, чтобы отличия от рабочего оригинала были видны.
"""

from __future__ import annotations

import os
import subprocess
import warnings
from dataclasses import dataclass, field

# Мы не используем torchcodec (аудио грузим в память), поэтому глушим его
# шумные предупреждения о ненайденных ffmpeg-DLL и предупреждение о симлинках.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
warnings.filterwarnings("ignore", message=".*torchcodec.*")

# ── Константы ────────────────────────────────────────────────────────────────

AUDIO_EXT = {".mp3", ".wav", ".ogg", ".oga", ".m4a", ".flac", ".aac", ".opus", ".wma"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv"}
SUPPORTED_EXT = AUDIO_EXT | VIDEO_EXT

DEFAULT_MODEL = "small"   # золотая середина скорость/качество на CPU
DEFAULT_BEAM_SIZE = 5     # для русского хватает и 3 (быстрее)
DIARIZATION_MODEL = os.environ.get(
    "DIARIZATION_MODEL", "pyannote/speaker-diarization-community-1"
)

# Человекочитаемые названия шагов pyannote — их видно в прогресс-баре фронта.
# ИЗМЕНЕНО при копировании: подписи переведены на украинский, это текст
# интерфейса. Ключи — имена шагов pyannote, их трогать нельзя.
_DIAR_STEPS = {
    "segmentation": "Шукаю мовлення (сегментація)",
    "embeddings": "Рахую відбитки голосів",
    "speaker_counting": "Рахую мовців",
    "discrete_diarization": "Збираю розмітку",
    "clustering": "Групую за голосами",
}


# ── Структуры данных ─────────────────────────────────────────────────────────

@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str = ""
    # ДОБАВЛЕНО при копировании: пословные таймкоды, если их запросили.
    # Список (start, end, word). В оригинале поля не было — боту хватало
    # сегментов целиком, а здесь по ним режутся реплики разных спикеров.
    words: list[tuple[float, float, str]] = field(default_factory=list)


@dataclass
class DiarSegment:
    start: float
    end: float
    speaker: str


# ── Вспомогательное ──────────────────────────────────────────────────────────

def is_supported(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXT


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXT


def format_timestamp(seconds: float) -> str:
    """Секунды -> HH:MM:SS."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """Длина пересечения двух временных интервалов (0, если не пересекаются)."""
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _ffmpeg_bin() -> str:
    return os.environ.get("FFMPEG_BINARY", "ffmpeg")


def check_ffmpeg() -> bool:
    """Проверить, что ffmpeg доступен в PATH."""
    try:
        subprocess.run(
            [_ffmpeg_bin(), "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False



# ── Основной класс ───────────────────────────────────────────────────────────

class Transcriber:
    """
    Держит загруженные модели в памяти и переиспользует их между файлами.
    Модели грузятся лениво — при первом вызове .process().
    """

    def __init__(
        self,
        model_size: str = DEFAULT_MODEL,
        language: str | None = None,
        hf_token: str | None = None,
        device: str | None = None,
        diarize: bool = True,
        batch_size: int = 8,
        cpu_threads: int = 0,
        beam_size: int = DEFAULT_BEAM_SIZE,
    ) -> None:
        self.model_size = model_size
        self.language = language
        self.hf_token = hf_token or os.environ.get("HF_TOKEN")
        self.diarize = diarize
        self.beam_size = beam_size
        # batch_size>1 включает BatchedInferencePipeline (кратно быстрее на CPU).
        self.batch_size = batch_size
        # 0 -> использовать все ядра CPU.
        self.cpu_threads = cpu_threads or (os.cpu_count() or 4)

        self.device, self.compute_type = self._resolve_device(device)

        self._whisper = None      # WhisperModel или BatchedInferencePipeline
        self._batched = False
        self._diarizer = None     # pyannote.audio.Pipeline
        self._log = print
        self._progress = lambda stage, done=None, total=None: None

    # -- настройка устройства ------------------------------------------------

    @staticmethod
    def _resolve_device(device: str | None) -> tuple[str, str]:
        """Определить устройство и тип вычислений (CPU int8 / GPU float16)."""
        if device is None:
            try:
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:
                device = "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        return device, compute_type

    def set_logger(self, fn) -> None:
        """Подменить функцию логирования (бот шлёт прогресс в чат)."""
        self._log = fn

    def set_progress(self, fn) -> None:
        """Колбэк прогресса: fn(stage: str, done: float|None, total: float|None).

        Вызывается часто — троттлить обязан вызывающий, не мы.
        """
        self._progress = fn

    def _tick(self, stage: str, done=None, total=None) -> None:
        try:
            self._progress(stage, done, total)
        except Exception:            # прогресс не должен ронять обработку
            pass

    # -- ленивая загрузка моделей -------------------------------------------

    def _load_whisper(self):
        if self._whisper is None:
            from faster_whisper import WhisperModel
            self._log(
                f"Загружаю модель whisper «{self.model_size}» "
                f"({self.device}, потоков: {self.cpu_threads})…"
            )
            model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
            )
            if self.batch_size and self.batch_size > 1:
                try:
                    from faster_whisper import BatchedInferencePipeline
                    self._whisper = BatchedInferencePipeline(model=model)
                    self._batched = True
                except ImportError:
                    self._whisper = model      # старая версия faster-whisper
            else:
                self._whisper = model
        return self._whisper

    def _load_diarizer(self):
        if self._diarizer is None:
            if not self.hf_token:
                raise RuntimeError(
                    "HF_TOKEN не задано — діаризація недоступна. "
                    "Вкажіть токен у змінній оточення HF_TOKEN."
                )
            import torch
            from pyannote.audio import Pipeline
            self._log("Загружаю модель диаризации pyannote…")
            # pyannote.audio 4.x использует token=, 3.x — use_auth_token=.
            try:
                pipeline = Pipeline.from_pretrained(
                    DIARIZATION_MODEL, token=self.hf_token
                )
            except TypeError:
                pipeline = Pipeline.from_pretrained(
                    DIARIZATION_MODEL, use_auth_token=self.hf_token
                )
            if pipeline is None:
                raise RuntimeError(
                    "Не вдалося завантажити pyannote. Перевірте, що HF-токен вірний "
                    "і що прийнято умови моделі pyannote/speaker-diarization-3.1 "
                    "на huggingface.co."
                )
            pipeline.to(torch.device(self.device))
            self._diarizer = pipeline
        return self._diarizer

    # -- шаги пайплайна ------------------------------------------------------

    def extract_audio(self, input_path: str, out_wav: str) -> None:
        """Привести любой вход к wav 16kHz mono через ffmpeg."""
        cmd = [
            _ffmpeg_bin(), "-y",
            "-i", input_path,
            "-vn",                # без видео
            "-ac", "1",           # моно
            "-ar", "16000",       # 16 kHz
            "-f", "wav",
            out_wav,
        ]
        result = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg не зміг обробити файл:\n{result.stderr[-800:]}")

    def transcribe(
        self, wav_path: str, word_timestamps: bool = False
    ) -> tuple[list[Segment], str]:
        """faster-whisper -> список текстовых сегментов + определённый язык.

        ИЗМЕНЕНО при копировании: добавлен параметр word_timestamps. В боте
        сегмент целиком уходил в Markdown, а здесь по границам слов режутся
        реплики разных спикеров — без этого сегмент на 30 секунд с двумя
        голосами получает один speaker_id, и таймкод цитаты врёт на полминуты.
        """
        model = self._load_whisper()
        kwargs = dict(
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=True,     # отсекает тишину
        )
        if word_timestamps:
            kwargs["word_timestamps"] = True
        if self._batched:
            kwargs["batch_size"] = self.batch_size
        segments_iter, info = model.transcribe(wav_path, **kwargs)
        # info.duration — длительность аудио; по ней и считаем процент:
        # segments_iter ленивый, seg.end показывает, докуда дошли.
        total = getattr(info, "duration", 0.0) or 0.0
        segments: list[Segment] = []
        for seg in segments_iter:
            text = seg.text.strip()
            if text:
                words = [
                    (w.start, w.end, w.word)
                    for w in (getattr(seg, "words", None) or [])
                    if w.start is not None and w.end is not None
                ]
                segments.append(
                    Segment(start=seg.start, end=seg.end, text=text, words=words)
                )
            self._tick("Розпізнаю мовлення", seg.end, total)
        self._tick("Розпізнаю мовлення", total, total)
        return segments, info.language

    def diarize_annotations(
        self,
        wav_path: str,
        num_speakers: int | None = None,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> tuple:
        """pyannote -> (полная аннотация, аннотация без перекрытий).

        Полная нужна для нарезки по голосам: только по ней видно, где двое
        говорят одновременно. Версия без перекрытий точнее ложится на текст,
        её используем для транскрипции.
        """
        pipeline = self._load_diarizer()
        kwargs = {}
        if num_speakers is not None:
            kwargs["num_speakers"] = num_speakers
        else:
            if min_speakers is not None:
                kwargs["min_speakers"] = min_speakers
            if max_speakers is not None:
                kwargs["max_speakers"] = max_speakers

        # pyannote зовёт hook на каждом шаге и внутри долгих шагов —
        # прокидываем его в наш прогресс как есть.
        def hook(step_name, step_artifact, file=None, total=None, completed=None):
            self._tick(_DIAR_STEPS.get(step_name, step_name), completed, total)

        kwargs["hook"] = hook

        # Передаём аудио уже загруженным в память — так pyannote не обращается
        # к torchcodec (которому нужна shared-сборка ffmpeg с DLL). Наш wav —
        # это 16kHz mono PCM, читаем его напрямую.
        result = pipeline(self._load_waveform(wav_path), **kwargs)

        # pyannote 4.x возвращает DiarizeOutput, 3.x — сразу Annotation.
        full = getattr(result, "speaker_diarization", None) or result
        exclusive = getattr(result, "exclusive_speaker_diarization", None) or full
        return full, exclusive

    def diarize_audio(
        self,
        wav_path: str,
        num_speakers: int | None = None,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> list[DiarSegment]:
        """pyannote -> список сегментов говорящих (без перекрытий)."""
        _, exclusive = self.diarize_annotations(
            wav_path,
            num_speakers=num_speakers,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
        )
        return self._to_diar_segments(exclusive)

    @staticmethod
    def _to_diar_segments(annotation) -> list[DiarSegment]:
        diar: list[DiarSegment] = []
        for turn, _, speaker in annotation.itertracks(yield_label=True):
            diar.append(DiarSegment(start=turn.start, end=turn.end, speaker=speaker))
        diar.sort(key=lambda d: d.start)
        return diar

    @staticmethod
    def _load_waveform(wav_path: str):
        """Прочитать PCM-wav в тензор (channels, time) float32 в [-1, 1]."""
        import numpy as np
        import torch
        from scipy.io import wavfile

        sample_rate, data = wavfile.read(wav_path)
        if data.dtype == np.int16:
            audio = data.astype(np.float32) / 32768.0
        elif data.dtype == np.int32:
            audio = data.astype(np.float32) / 2147483648.0
        elif data.dtype == np.uint8:
            audio = (data.astype(np.float32) - 128.0) / 128.0
        else:  # уже float
            audio = data.astype(np.float32)

        if audio.ndim == 1:
            audio = audio[np.newaxis, :]      # (1, time)
        else:
            audio = audio.T                    # (channels, time)

        return {"waveform": torch.from_numpy(audio), "sample_rate": sample_rate}

    # -- слияние текста и спикеров -------------------------------------------

    @staticmethod
    def assign_speakers(segments: list[Segment], diar: list[DiarSegment]) -> None:
        """Каждому текстовому сегменту назначить спикера по макс. перекрытию."""
        for seg in segments:
            best_speaker, best_overlap = None, 0.0
            for d in diar:
                ov = _overlap(seg.start, seg.end, d.start, d.end)
                if ov > best_overlap:
                    best_overlap, best_speaker = ov, d.speaker
            if best_speaker is None:
                # нет пересечения — берём ближайший по времени сегмент
                if diar:
                    best_speaker = min(
                        diar,
                        key=lambda d: min(
                            abs(d.start - seg.start), abs(d.end - seg.end)
                        ),
                    ).speaker
                else:
                    best_speaker = "SPEAKER_00"
            seg.speaker = best_speaker
