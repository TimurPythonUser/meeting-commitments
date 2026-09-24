"""
Генератор тестового аудіо зі сценарію-markdown.

Навіщо: завдання вимагає фіктивну розмову двох чітко розрізнюваних спікерів.
Синтез замість живого запису обрано з однієї причини — ми точно знаємо межі
кожної репліки. Це безкоштовний ground truth: за ним перевіряється, що
таймкоди цитат у виводі продукту вказують на правильне місце запису.

Конвеєр:
  1. розібрати сценарій: рядки «**Оксана:** текст» -> (speaker, text)
  2. озвучити кожну репліку своїм голосом (OpenAI gpt-4o-mini-tts, wav)
  3. склеїти в одну доріжку з паузами, рахуючи межі реплік
  4. записати .wav поруч із .timings.json

Склейка йде через numpy, без ffmpeg: усі репліки приходять від однієї моделі
з однаковим форматом, тож достатньо конкатенації.

Параметри аудіо задаються в самому сценарії рядком-коментарем:

    <!-- audio: voice_a=nova, voice_b=onyx, speed=1.0, pause=0.35, seed=1 -->

Усі ключі необов'язкові. voice_a — голос першого спікера за алфавітом імен,
voice_b — другого. Це дозволяє робити набір записів із різними параметрами,
не чіпаючи код.

Запуск:
    python make_audio.py                    # три основні сценарії
    python make_audio.py script_v1.md       # один
    python make_audio.py --manual           # набір для ручної перевірки
    python make_audio.py script_v1.md -o /шлях/meeting_v1.wav
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import sys
import wave
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

# ── Налаштування ─────────────────────────────────────────────────────────────

TTS_MODEL = os.environ.get("TTS_MODEL", "gpt-4o-mini-tts")

# Спікери основного тест-сету. Голоси навмисне контрастні: завдання вимагає
# двох ЧІТКО РОЗРІЗНЮВАНИХ спікерів, інакше діаризація склеїть їх в одного
# і тест перестане будь-що перевіряти.
DEFAULT_VOICES = {
    "Оксана": "nova",   # жіночий
    "Ігор": "onyx",     # чоловічий
}

DEFAULT_PAUSE_SAME = 0.30      # сек, репліки одного спікера підряд
DEFAULT_PAUSE_SWITCH = 0.45    # сек, зміна того, хто говорить
LEAD_IN = 0.25                 # тиша на початку: whisper ріже перший склад без неї

# Ліміт завдання. Перевищення — не помилка скрипта, але попередження має бути
# видно, інакше тест виїде за межі заявленого скоупу.
MAX_DURATION_SEC = 180.0

# Ціна gpt-4o-mini-tts станом на 2026-09-23. Потрібна для delivery notes:
# генерація тест-сету — теж витрата, хай і разова, що не входить у вартість
# операції продукту.
TTS_USD_PER_1M_INPUT_TOKENS = 0.60

SCRIPTS = ["script_v1.md", "script_v2.md", "script_v3_ambiguous.md"]
OUT_NAMES = {
    "script_v1.md": "meeting_v1.wav",
    "script_v2.md": "meeting_v2.wav",
    "script_v3_ambiguous.md": "meeting_v3_ambiguous.wav",
}

HERE = Path(__file__).resolve().parent
MANUAL_DIR = HERE / "manual"
DEFAULT_OUT_DIR = HERE.parent / "backend" / "tests" / "fixtures"
MANUAL_OUT_DIR = MANUAL_DIR / "audio"

# «**Ім'я:** текст». Імена не зашиті: беремо будь-яке слово з великої літери
# перед двокрапкою, а далі звіряємо зі списком спікерів сценарію. Так розмітка
# таблиць у шапці («| **A** | Тест-сет |») у діалог не потрапляє.
LINE_RE = re.compile(r"^\*\*([^*:]{2,30})\:\*\*\s*(.+?)\s*$")
META_RE = re.compile(r"<!--\s*audio:\s*(.+?)\s*-->", re.IGNORECASE)


@dataclass
class Replica:
    index: int
    speaker: str
    text: str
    start: float = 0.0
    end: float = 0.0


@dataclass
class AudioParams:
    voices: dict[str, str]
    speed: float = 1.0
    pause_same: float = DEFAULT_PAUSE_SAME
    pause_switch: float = DEFAULT_PAUSE_SWITCH
    instructions: str | None = None


# ── Розбір сценарію ──────────────────────────────────────────────────────────

def parse_meta(text: str) -> dict[str, str]:
    """Витягти рядок <!-- audio: ключ=значення, ... --> зі сценарію."""
    m = META_RE.search(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for chunk in m.group(1).split(","):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def dialogue_section(raw_text: str) -> str:
    """Відрізати шапку сценарію від самого діалогу.

    Шапка теж повна рядків «**Слово:** текст» — «**Призначення:**»,
    «**Учасники:**», «**Мова:**». Без цього відрізу вони потрапляють у
    діалог як репліки неіснуючих спікерів.

    Межа — заголовок «## Діалог», а якщо його немає, останній роздільник
    «---» у файлі.
    """
    m = re.search(r"^##\s+Діалог\s*$", raw_text, re.MULTILINE)
    if m:
        return raw_text[m.end():]

    lines = raw_text.splitlines()
    last_hr = max(
        (i for i, ln in enumerate(lines) if ln.strip() == "---"),
        default=-1,
    )
    return "\n".join(lines[last_hr + 1:]) if last_hr >= 0 else raw_text


def parse_script(path: Path) -> tuple[list[Replica], AudioParams]:
    """Розібрати сценарій: репліки + параметри озвучення."""
    raw_text = path.read_text(encoding="utf-8")
    meta = parse_meta(raw_text)

    replicas: list[Replica] = []
    for raw in dialogue_section(raw_text).splitlines():
        m = LINE_RE.match(raw.strip())
        if not m:
            continue
        name = m.group(1).strip()
        if len(name.split()) > 2:
            continue
        replicas.append(Replica(index=len(replicas), speaker=name, text=m.group(2)))

    if not replicas:
        raise ValueError(
            f"{path.name}: не знайдено жодної репліки. "
            "Очікується формат «**Оксана:** текст»."
        )

    names = sorted({r.speaker for r in replicas})
    if len(names) != 2:
        raise ValueError(f"{path.name}: спікерів {len(names)} ({names}), очікується 2.")

    # voice_a — перший спікер за алфавітом, voice_b — другий
    voices = {}
    for i, name in enumerate(names):
        key = "voice_a" if i == 0 else "voice_b"
        voices[name] = meta.get(key) or DEFAULT_VOICES.get(name) or ("nova" if i == 0 else "onyx")

    params = AudioParams(
        voices=voices,
        speed=float(meta.get("speed", 1.0)),
        pause_same=float(meta.get("pause", DEFAULT_PAUSE_SAME)),
        pause_switch=float(meta.get("pause_switch", meta.get("pause", DEFAULT_PAUSE_SAME))) + 0.15,
        instructions=meta.get("instructions"),
    )
    return replicas, params


# ── Синтез ───────────────────────────────────────────────────────────────────

def synth(client, text: str, voice: str, params: AudioParams) -> tuple[np.ndarray, int]:
    """Озвучити одну репліку. Повертає (моно float32 у [-1,1], sample_rate)."""
    kwargs = dict(model=TTS_MODEL, voice=voice, input=text, response_format="wav")
    if abs(params.speed - 1.0) > 1e-6:
        kwargs["speed"] = params.speed
    if params.instructions:
        kwargs["instructions"] = params.instructions

    response = client.audio.speech.create(**kwargs)
    with wave.open(io.BytesIO(response.read()), "rb") as w:
        sample_rate = w.getframerate()
        channels = w.getnchannels()
        frames = w.readframes(w.getnframes())

    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, sample_rate


# ── Склейка ──────────────────────────────────────────────────────────────────

def build(client, replicas: list[Replica], params: AudioParams) -> tuple[np.ndarray, int]:
    """Озвучити всі репліки та склеїти, проставивши кожній точні межі."""
    parts: list[np.ndarray] = []
    sample_rate: int | None = None
    cursor = 0.0
    prev_speaker: str | None = None

    for rep in replicas:
        pause = (
            LEAD_IN if prev_speaker is None
            else params.pause_switch if rep.speaker != prev_speaker
            else params.pause_same
        )

        print(f"  [{rep.index + 1:>2}/{len(replicas)}] {rep.speaker}: {rep.text[:50]}…")
        audio, sr = synth(client, rep.text, params.voices[rep.speaker], params)

        if sample_rate is None:
            sample_rate = sr
        elif sr != sample_rate:
            raise RuntimeError(
                f"Репліка {rep.index}: частота {sr} замість {sample_rate}. "
                "Склейка без ресемплінгу неможлива."
            )

        parts.append(np.zeros(int(pause * sample_rate), dtype=np.float32))
        cursor += pause

        rep.start = round(cursor, 3)
        parts.append(audio)
        cursor += len(audio) / sample_rate
        rep.end = round(cursor, 3)

        prev_speaker = rep.speaker

    return np.concatenate(parts), sample_rate


def write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())


# ── Один сценарій ────────────────────────────────────────────────────────────

def process(client, script_path: Path, out_path: Path) -> dict:
    print(f"\n=== {script_path.name} ===")
    replicas, params = parse_script(script_path)
    print(f"Реплік: {len(replicas)} | голоси: {params.voices} | швидкість: {params.speed}")

    audio, sample_rate = build(client, replicas, params)
    duration = len(audio) / sample_rate
    write_wav(out_path, audio, sample_rate)

    # Ground truth: межі реплік. За ними перевіряється, що таймкоди цитат
    # у виводі продукту потрапляють у правильну репліку.
    timings = {
        "audio": out_path.name,
        "source_script": script_path.name,
        "language": "uk",
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "tts_model": TTS_MODEL,
        "voices": params.voices,
        "speed": params.speed,
        "pause_same": params.pause_same,
        "pause_switch": params.pause_switch,
        "sample_rate": sample_rate,
        "duration_sec": round(duration, 3),
        "replicas": [asdict(r) for r in replicas],
    }
    timings_path = out_path.with_suffix(".timings.json")
    timings_path.write_text(
        json.dumps(timings, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    chars = sum(len(r.text) for r in replicas)
    # Грубо: ~4 символи на токен для кирилиці. Точність тут не потрібна — це
    # разова витрата на підготовку тест-сету, а не вартість операції продукту.
    est_cost = (chars / 4) / 1_000_000 * TTS_USD_PER_1M_INPUT_TOKENS

    print(f"Готово: {out_path}")
    print(f"  тривалість : {duration:.1f} с ({duration / 60:.2f} хв)")
    print(f"  тайминги   : {timings_path.name}")
    print(f"  символів   : {chars}, оцінка витрат TTS ≈ ${est_cost:.4f}")

    if duration > MAX_DURATION_SEC:
        print(
            f"  УВАГА: {duration:.1f} с — більше ліміту завдання "
            f"({MAX_DURATION_SEC:.0f} с). Скороти сценарій.",
            file=sys.stderr,
        )

    return {"duration": duration, "chars": chars, "cost": est_cost}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Генератор тестового аудіо зі сценарію.")
    parser.add_argument("script", nargs="?", default=None,
                        help="сценарій .md (за замовчуванням — три основні)")
    parser.add_argument("-o", "--output", default=None,
                        help="шлях до .wav (тільки разом із вказаним сценарієм)")
    parser.add_argument("--out-dir", default=None, help="куди складати")
    parser.add_argument("--manual", action="store_true",
                        help="згенерувати набір manual/ для ручної перевірки")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv(HERE.parent / ".env")
    except ImportError:
        pass

    if not os.environ.get("OPENAI_API_KEY"):
        print("Помилка: OPENAI_API_KEY не заданий.", file=sys.stderr)
        return 2

    try:
        from openai import OpenAI
    except ImportError:
        print("Помилка: немає пакета openai. Встанови: pip install openai", file=sys.stderr)
        return 2

    client = OpenAI()

    if args.manual:
        out_dir = Path(args.out_dir) if args.out_dir else MANUAL_OUT_DIR
        scripts = sorted(MANUAL_DIR.glob("manual_*.md"))
        if not scripts:
            print(f"Помилка: у {MANUAL_DIR} немає manual_*.md", file=sys.stderr)
            return 2
        jobs = [(s, out_dir / f"{s.stem}.wav") for s in scripts]
    elif args.script:
        script_path = Path(args.script) if os.path.isabs(args.script) else HERE / args.script
        out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_OUT_DIR
        out = (
            Path(args.output) if args.output
            else out_dir / OUT_NAMES.get(script_path.name, script_path.stem + ".wav")
        )
        jobs = [(script_path, out)]
    else:
        out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_OUT_DIR
        jobs = [(HERE / s, out_dir / OUT_NAMES[s]) for s in SCRIPTS]

    total_cost = 0.0
    for script_path, out_path in jobs:
        if not script_path.is_file():
            print(f"Пропускаю: немає файлу {script_path}", file=sys.stderr)
            continue
        try:
            total_cost += process(client, script_path, out_path)["cost"]
        except Exception as exc:
            print(f"Помилка на {script_path.name}: {exc}", file=sys.stderr)
            return 1

    print(f"\nУсього витрачено на генерацію ≈ ${total_cost:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
