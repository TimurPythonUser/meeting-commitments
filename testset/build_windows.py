"""
Збирає evidence_windows.json із таймингів генератора.

Навіщо: вікна таймкодів раніше були вписані руками і застарівали після кожної
перегенерації аудіо — TTS ніколи не читає той самий текст байт у байт однаково.
Тут вони виводяться з .timings.json за фразами-маркерами, тож після будь-якої
перегенерації достатньо прогнати цей скрипт.

Маркер — шматок репліки, який однозначно її знаходить. Якщо маркер перестав
збігатися (сценарій переписали), скрипт падає з поясненням, а не мовчки видає
зсунуте вікно.

Запуск:
    ../.venv/Scripts/python.exe build_windows.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "backend" / "tests" / "fixtures"
OUT = HERE / "evidence_windows.json"

TOLERANCE_SEC = 1.5

# Для кожного випадку: що шукаємо і за якими фразами.
# Значення — список маркерів; вікно розтягується від початку першої знайденої
# репліки до кінця останньої.
MARKERS = {
    "meeting_v1": {
        "participants": {
            "Оксана": ["Я Оксана, веду проєкт"],
            "Ігор": ["Ігор, фронтенд"],
        },
        "commitments": {
            "Зверстати екран оплати за макетами з фігми": {
                "accepted": ["Візьму на себе"],
                "deadline_set": ["до п'ятниці закрию", "значить п'ятниця"],
                "deadline_corrected": ["Давай краще понеділок", "хай буде понеділок"],
                "note": "Вікно deadline_corrected зобов'язане потрапити в репліки, де понеділок замінює п'ятницю. Якщо таймкод дедлайну вказує на deadline_set — система взяла СКАСОВАНИЙ строк, це помилка.",
            },
            "Переписати авторизацію на нову бібліотеку": {
                "proposed": ["перепишемо авторизацію"],
                "rejected": ["ще розсилка висить", "просто до слова"],
                "note": "Відмова не прозвучала прямим словом: Оксана пішла в іншу тему, Ігор згорнув сам. Будь-яке з цих двох вікон зараховується.",
            },
            "Підготувати тексти для березневої розсилки": {
                "accepted": ["обіцяла підготувати тексти"],
                "cancelled": ["знімаю з себе"],
                "note": "Репліка Ігоря про перенесення розсилки теж допустима як контекст скасування.",
            },
            "Оновити скріншоти в сторі": {
                "accepted": ["Скріншоти в сторі застаріли", "Там штук вісім"],
                "note": "Власника в цьому вікні немає ні в кого — на цьому й перевіряється, що система не приписала задачу Ігорю за його «так, треба».",
            },
            "Докрутити аналітику: проставити події на новому екрані": {
                "proposed": ["аналітику треба докрутити"],
                "accepted": ["наступного тижня зроблю"],
                "deadline_set": ["Тільки не зараз, наступного тижня", "наступного тижня зроблю"],
            },
        },
        "open_questions": {
            "Хто затверджує бюджет на рекламу": ["бюджет на рекламу", "з'ясую окремо"],
        },
        "negative": [
            {
                "markers": ["до п'ятниці закрию", "значить п'ятниця"],
                "field": "deadline evidence для екрана оплати",
                "why_wrong": "Це п'ятниця — скасований строк. Фінальний дедлайн звучить пізніше.",
            },
            {
                "markers": ["Там штук вісім"],
                "field": "owner evidence для скріншотів у сторі",
                "why_wrong": "Репліка Ігоря «так, треба» — згода з потрібністю задачі, а не прийняття відповідальності.",
            },
            {
                "markers": ["Може, Сергій?"],
                "field": "owner evidence для питання про бюджет",
                "why_wrong": "Припущення, яке Оксана одразу не підтвердила. Призначати Сергія не можна.",
            },
        ],
    },

    "meeting_v2": {
        "participants": {
            "Оксана": ["Я Оксана, веду проєкт"],
            "Ігор": ["Ігор, фронтенд"],
        },
        "commitments": {
            "Зверстати екран оплати за макетами з фігми": {
                "accepted": ["Візьму на себе"],
                "deadline_set": ["до п'ятниці закрию", "значить п'ятниця"],
                "deadline_corrected": ["Давай краще понеділок", "хай буде понеділок"],
            },
            "Переписати авторизацію на нову бібліотеку": {
                "proposed": ["перепишемо авторизацію"],
                "accepted": ["Бери на себе, строки потім", "Зрозумів, беру"],
                "note": "ЄДИНА ВІДМІННІСТЬ ВІД v1. У v1 це місце містить відмову, тут — згоду. Власник Ігор береться звідси.",
            },
            "Підготувати тексти для березневої розсилки": {
                "accepted": ["обіцяла підготувати тексти"],
                "cancelled": ["знімаю з себе"],
            },
            "Оновити скріншоти в сторі": {
                "accepted": ["Скріншоти в сторі застаріли", "Там штук вісім"],
            },
            "Докрутити аналітику: проставити події на новому екрані": {
                "proposed": ["аналітику треба докрутити"],
                "accepted": ["наступного тижня зроблю"],
                "deadline_set": ["Тільки не зараз, наступного тижня", "наступного тижня зроблю"],
            },
        },
        "open_questions": {
            "Хто затверджує бюджет на рекламу": ["бюджет на рекламу", "з'ясую окремо"],
        },
        "negative": [
            {
                "markers": ["до п'ятниці закрию", "значить п'ятниця"],
                "field": "deadline evidence для екрана оплати",
                "why_wrong": "Це п'ятниця — скасований строк.",
            },
        ],
    },

    "meeting_v3_ambiguous": {
        "participants": {
            "Оксана": ["Оксана на зв'язку"],
            "Ігор": ["Ігор, привіт"],
        },
        "commitments": {
            "Міграція бази даних": {
                "proposed": ["щодо міграції бази"],
                "note": "Лише пропозиція. Вікна accepted тут немає й бути не може — згоди в записі не звучить.",
            },
        },
        "open_questions": {},
        "clarifications": {
            "Виконавця не визначено": ["подивлюся, але не обіцяю", "Олену попрошу"],
            "Строк суперечливий": ["до кінця наступного"],
        },
        "negative": [
            {
                "markers": ["подивлюся, але не обіцяю"],
                "field": "owner evidence для міграції бази",
                "why_wrong": "«Подивлюся, але не обіцяю» — не згода. Власником Ігоря ставити не можна.",
            },
        ],
    },
}


def norm(s: str) -> str:
    """Порівнюємо без пунктуації й регістру: апострофи в TTS-тексті різні."""
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


def find(reps: list[dict], marker: str, case: str) -> dict:
    target = norm(marker)
    for r in reps:
        if target in norm(r["text"]):
            return r
    raise SystemExit(
        f"ПОМИЛКА [{case}]: маркер «{marker}» не знайдено в репліках.\n"
        f"Схоже, сценарій переписали. Онови MARKERS у build_windows.py."
    )


def window(reps: list[dict], markers: list[str], case: str) -> list[float]:
    found = [find(reps, m, case) for m in markers]
    return [
        round(min(r["start"] for r in found), 2),
        round(max(r["end"] for r in found), 2),
    ]


def main() -> int:
    out: dict = {
        "purpose": "Очікувані часові вікна підтверджувальних цитат. Дозволяє перевіряти не лише склад зобов'язань, а й те, що таймкод вказує на правильне місце запису — інакше кнопка «прослухати» програє не той фрагмент, а перевіряльник цього не помітить.",
        "derived_from": "backend/tests/fixtures/*.timings.json — точні межі реплік від генератора TTS. НЕ з виводу продукту.",
        "generated_by": "testset/build_windows.py — перезапускати після кожної перегенерації аудіо",
        "language": "uk",
        "how_to_use": "Для кожного зобов'язання взяти primary_evidence (або evidence потрібної події lifecycle) і перевірити, що [start, end] перетинається із вказаним вікном. Точного збігу не потрібно: STT ріже сегменти по-своєму. Достатньо перетину.",
        "tolerance_sec": TOLERANCE_SEC,
    }

    for case, spec in MARKERS.items():
        timings_path = FIXTURES / f"{case}.timings.json"
        if not timings_path.is_file():
            print(f"Пропускаю {case}: немає {timings_path.name}", file=sys.stderr)
            continue
        timings = json.loads(timings_path.read_text(encoding="utf-8"))
        reps = timings["replicas"]

        section: dict = {"duration_sec": timings["duration_sec"]}

        section["participants"] = {
            name: window(reps, ms, case) for name, ms in spec["participants"].items()
        }

        commitments: dict = {}
        for title, events in spec["commitments"].items():
            entry: dict = {}
            for event, value in events.items():
                if event == "note":
                    entry["note"] = value
                else:
                    entry[event] = window(reps, value, case)
            commitments[title] = entry
        section["commitments"] = commitments

        if spec.get("open_questions"):
            section["open_questions"] = {
                q: window(reps, ms, case) for q, ms in spec["open_questions"].items()
            }
        else:
            section["open_questions"] = {}

        if spec.get("clarifications"):
            section["clarifications_needed"] = {
                c: window(reps, ms, case) for c, ms in spec["clarifications"].items()
            }

        if spec.get("negative"):
            section["negative_checks"] = [
                {
                    "window": window(reps, n["markers"], case),
                    "field": n["field"],
                    "why_wrong": n["why_wrong"],
                }
                for n in spec["negative"]
            ]

        out[case] = section
        print(f"{case}: {timings['duration_sec']:.1f} с, зобов'язань {len(commitments)}")

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nЗаписано: {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
