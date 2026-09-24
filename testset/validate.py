"""
Перевірка внутрішньої узгодженості тест-сету.

Не чіпає продукт і не вимагає ключів — лише звіряє між собою те, що лежить на
диску: сценарії, аудіо, тайминги, еталони та вікна таймкодів.

Ловить саме ті помилки, які інакше спливуть пізно й тихо:
  * еталон посилається на зобов'язання, якого немає у вікнах таймкодів
    (або навпаки) — отже, один із файлів правили, а другий забули;
  * аудіо перегенерували, а таймкоди у вікнах лишилися від старої версії;
  * запис виліз за ліміт завдання у 3 хвилини;
  * expected_counts розійшлися з фактичним складом commitments;
  * v1 і v2 відрізняються не одним зобов'язанням, а кількома;
  * у таймингах лишилася стара мова.

Запуск:
    ../.venv/Scripts/python.exe validate.py
Код повернення 0 — усе сходиться, 1 — є розбіжності.
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "backend" / "tests" / "fixtures"
MANUAL_AUDIO = HERE / "manual" / "audio"

MAX_DURATION_SEC = 180.0
NEAR_LIMIT_SEC = 170.0   # привід попередити заздалегідь
LANGUAGE = "uk"

CASES = ["v1", "v2", "v3_ambiguous"]
AUDIO_NAME = {c: f"meeting_{c}.wav" for c in CASES}
EXPECTED_NAME = {"v1": "expected_v1.json", "v2": "expected_v2.json",
                 "v3_ambiguous": "expected_v3.json"}
WINDOWS_KEY = {c: f"meeting_{c}" for c in CASES}

problems: list[str] = []
warnings: list[str] = []


def fail(msg: str) -> None:
    problems.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def load_json(path: Path) -> dict | None:
    if not path.is_file():
        fail(f"немає файлу: {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"побитий JSON {path.name}: {exc}")
        return None


def wav_duration(path: Path) -> float | None:
    if not path.is_file():
        fail(f"немає аудіо: {path}")
        return None
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def check_manual() -> None:
    """Набір для ручної перевірки: лише наявність і базова цілісність."""
    scripts = sorted((HERE / "manual").glob("manual_*.md"))
    if not scripts:
        return
    print("\n=== набір для ручної перевірки ===")
    for script in scripts:
        wav = MANUAL_AUDIO / f"{script.stem}.wav"
        if not wav.is_file():
            warn(f"{script.stem}: аудіо ще не згенеровано ({wav.name})")
            continue
        timings = load_json(wav.with_suffix(".timings.json"))
        if timings is None:
            continue
        real = wav_duration(wav)
        speakers = {r["speaker"] for r in timings["replicas"]}
        print(
            f"{script.stem:12} {real:>5.1f} с  реплік={len(timings['replicas']):>2}  "
            f"speed={timings.get('speed')}  {sorted(speakers)}"
        )
        if len(speakers) != 2:
            fail(f"{script.stem}: спікерів {len(speakers)}, очікувалося 2")


def main() -> int:
    windows = load_json(HERE / "evidence_windows.json")
    if windows is None:
        return 1

    titles_by_case: dict[str, set[str]] = {}

    for case in CASES:
        print(f"\n=== {case} ===")

        audio_path = FIXTURES / AUDIO_NAME[case]
        timings = load_json(audio_path.with_suffix(".timings.json"))
        expected = load_json(HERE / EXPECTED_NAME[case])
        win = windows.get(WINDOWS_KEY[case])

        if win is None:
            fail(f"{case}: немає секції {WINDOWS_KEY[case]} в evidence_windows.json")
        if timings is None or expected is None:
            continue

        # 0. Мова — щоб після переходу на українську ніде не лишилося старої
        for name, obj in (("timings", timings), ("еталон", expected)):
            lang = obj.get("language")
            if lang and lang != LANGUAGE:
                fail(f"{case}: у {name} мова «{lang}», очікується «{LANGUAGE}»")

        # 1. Тривалість: реальна, з таймингів і заявлена у вікнах
        real = wav_duration(audio_path)
        if real is None:
            continue
        print(f"тривалість: {real:.1f} с")

        if real > MAX_DURATION_SEC:
            fail(f"{case}: {real:.1f} с — за лімітом завдання ({MAX_DURATION_SEC:.0f} с)")
        elif real > NEAR_LIMIT_SEC:
            warn(f"{case}: {real:.1f} с — до ліміту {MAX_DURATION_SEC - real:.1f} с, запас малий")

        if abs(real - timings["duration_sec"]) > 0.5:
            fail(
                f"{case}: .wav триває {real:.1f} с, а в .timings.json записано "
                f"{timings['duration_sec']:.1f} с — аудіо перегенерували без таймингів"
            )

        if win and abs(real - win.get("duration_sec", -1)) > 1.0:
            fail(
                f"{case}: тривалість {real:.1f} с не сходиться з evidence_windows.json "
                f"({win.get('duration_sec')}) — вікна застаріли після перегенерації"
            )

        # 2. Репліки покривають запис і не вилазять за його кінець
        reps = timings["replicas"]
        print(f"реплік: {len(reps)}")
        if reps[-1]["end"] > real + 0.5:
            fail(f"{case}: остання репліка закінчується пізніше за сам файл")

        speakers = {r["speaker"] for r in reps}
        if len(speakers) != 2:
            fail(f"{case}: спікерів {len(speakers)}, очікувалося 2 — {speakers}")

        # Накладань бути не повинно: скоуп завдання їх виключає
        for a, b in zip(reps, reps[1:]):
            if b["start"] < a["end"] - 0.01:
                fail(f"{case}: репліки {a['index']} і {b['index']} накладаються")

        # 3. Склад еталона проти expected_counts
        commits = expected["commitments"]
        titles = {c["title"] for c in commits}
        titles_by_case[case] = titles
        print(f"зобов'язань в еталоні: {len(commits)}")

        counts = expected.get("expected_counts", {})
        for status in ("agreed", "proposed_not_accepted", "cancelled"):
            if status in counts:
                actual = sum(1 for c in commits if c["status"] == status)
                if actual != counts[status]:
                    fail(
                        f"{case}: expected_counts.{status}={counts[status]}, "
                        f"а в commitments таких {actual}"
                    )
        if "owner_is_null" in counts:
            actual = sum(1 for c in commits if c.get("owner_is_null"))
            if actual != counts["owner_is_null"]:
                fail(
                    f"{case}: expected_counts.owner_is_null={counts['owner_is_null']}, "
                    f"а в commitments таких {actual}"
                )
        # Пастка «задача без власника» перевіряється саме серед прийнятих:
        # у неприйнятої пропозиції власника немає й бути не повинно, і змішувати
        # ці два випадки в одному лічильнику — означає втратити саму пастку.
        if "owner_is_null_among_agreed" in counts:
            actual = sum(
                1 for c in commits if c.get("owner_is_null") and c["status"] == "agreed"
            )
            if actual != counts["owner_is_null_among_agreed"]:
                fail(
                    f"{case}: expected_counts.owner_is_null_among_agreed="
                    f"{counts['owner_is_null_among_agreed']}, а серед agreed таких {actual}"
                )
        if "open_questions" in counts:
            actual = len(expected.get("open_questions", []))
            if actual != counts["open_questions"]:
                fail(
                    f"{case}: expected_counts.open_questions={counts['open_questions']}, "
                    f"а в списку {actual}"
                )

        # owner і owner_is_null не повинні суперечити одне одному
        for c in commits:
            if c.get("owner_is_null") != (c.get("owner") is None):
                fail(f"{case}: «{c['title']}» — owner і owner_is_null суперечать одне одному")

        # 4. Еталон проти вікон таймкодів
        if win:
            win_titles = set(win.get("commitments", {}).keys())
            only_expected = titles - win_titles
            only_windows = win_titles - titles
            if only_expected:
                fail(f"{case}: є в еталоні, але немає вікон таймкодів: {sorted(only_expected)}")
            if only_windows:
                fail(f"{case}: є вікна таймкодів, але немає в еталоні: {sorted(only_windows)}")

            for title, events in win.get("commitments", {}).items():
                for event, value in events.items():
                    if event == "note" or not isinstance(value, list):
                        continue
                    start, end = value
                    if start < 0 or end > real + 0.5 or start >= end:
                        fail(f"{case}: вікно {title}/{event} = [{start}, {end}] поза записом")

    # 5. v1 і v2 зобов'язані відрізнятися рівно одним зобов'язанням
    if "v1" in titles_by_case and "v2" in titles_by_case:
        print("\n=== v1 проти v2 ===")
        if titles_by_case["v1"] != titles_by_case["v2"]:
            fail(
                "v1 і v2 містять різні набори зобов'язань — відрізнятися має "
                "тільки статус одного з них"
            )
        else:
            e1 = load_json(HERE / "expected_v1.json")
            e2 = load_json(HERE / "expected_v2.json")
            if e1 and e2:
                by1 = {c["title"]: c for c in e1["commitments"]}
                by2 = {c["title"]: c for c in e2["commitments"]}
                diff = [
                    t for t in by1
                    if (by1[t]["status"], by1[t].get("owner"), by1[t].get("deadline_resolution"))
                    != (by2[t]["status"], by2[t].get("owner"), by2[t].get("deadline_resolution"))
                ]
                print(f"відмінностей: {len(diff)} — {diff}")
                if len(diff) != 1:
                    fail(
                        f"v1 і v2 відрізняються в {len(diff)} зобов'язаннях, а мають рівно "
                        f"в одному: {diff}"
                    )

    check_manual()

    print("\n" + "=" * 60)
    for w in warnings:
        print(f"ПОПЕРЕДЖЕННЯ: {w}")
    if problems:
        for p in problems:
            print(f"ПОМИЛКА: {p}")
        print(f"\nРозбіжностей: {len(problems)}")
        return 1
    print("Тест-сет узгоджений." + (f" Попереджень: {len(warnings)}." if warnings else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
