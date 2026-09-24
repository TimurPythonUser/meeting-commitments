"""Извлечение обязательств из транскрипта: промпт, разбор, ретрай.

Схему ответа держит Pydantic (schemas.ExtractionResult), а не API: строгой
json_schema у DeepSeek на этом эндпоинте нет. Поэтому цикл такой:
запрос -> json.loads -> Pydantic -> при провале ретрай с текстом ошибки.

Здесь модель ещё может соврать. Гарантию даёт не этот файл, а verify.py,
который проверяет каждую цитату по транскрипту.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .llm.deepseek import DeepSeekClient, LlmResponse
from .recording import RecordingDate, sanity_check_deadlines
from .schemas import ExtractionResult
from .stt.base import Utterance
from .vendor.transcriber import format_timestamp

# Сколько раз перезапрашиваем при невалидном ответе. Первая попытка сверх
# этого числа. Каждый ретрай стоит денег и попадает в метрики.
MAX_RETRIES = 2


# ── Промпт ───────────────────────────────────────────────────────────────────
# Промпт на английском — модель на нём точнее держит формат и перечисления.
# Выход при этом украинский: язык ответа задан правилом внутри промпта.
# Цитаты из этого правила исключены — они копируются из транскрипта как есть,
# иначе verify.py их не найдёт и выбросит пункт.
# Слово «json» ниже присутствует обязательно: без него DeepSeek отклоняет
# запрос в режиме response_format={"type": "json_object"}.

SYSTEM_PROMPT = """\
You analyse recordings of work calls. The input is a transcript with \
timestamps and speaker labels. Your job is to extract the REAL COMMITMENTS, \
the open questions, and whatever stayed unclear. Answer with exactly one \
json object: no markdown, no text before or after.

This is NOT a meeting summary. The value of the answer is the accuracy of \
the FINAL state of the agreements as of the end of the recording.

OUTPUT LANGUAGE: Ukrainian. Every value you write yourself — title, \
owner_missing_reason, deadline.note, question, reason — must be in Ukrainian, \
whatever language is spoken in the recording.
THE ONE EXCEPTION IS QUOTES. Every evidence.text is copied from the \
transcript character for character, in the original language. Never \
translate, correct or tidy a quote. Code checks each quote against the \
transcript, and a quote that cannot be found there discards the whole item.

CORE RULES

1. WRITE OUT EVERY CANDIDATE TASK — including the ones that were cancelled \
   and the ones nobody agreed to. Do not drop them: each carries its full \
   trajectory through the recording in the lifecycle field \
   (proposed -> accepted -> deadline corrected -> cancelled).

2. STATUS IS DECIDED BY THE LAST EVENT IN THE RECORDING, not the first:
   - "agreed" — clear agreement was voiced and never revoked later;
   - "proposed_not_accepted" — it was proposed but never agreed to (the \
     other person changed the subject, stayed silent, or the proposer \
     dropped it themselves);
   - "cancelled" — it was agreed and then called off.
   Agreed and then cancelled -> "cancelled". Argued about first and agreed \
   at the end -> "agreed".
   AGREEMENT MUST BE UNAMBIGUOUS. A hedge is a refusal, not consent: \
   "I'll take a look but no promises", "I'll glance at it", "I'll try", \
   "maybe", "if I have time" all give "proposed_not_accepted". If the call \
   broke off and the other person never confirmed — also \
   "proposed_not_accepted".
   NOTE: status and owner are DIFFERENT things. A task both sides agreed is \
   needed is "agreed" even when no one was named to do it and owner = null. \
   A missing owner does NOT downgrade the status to "proposed_not_accepted"; \
   that status means the task itself was never agreed to.

3. OWNER = null UNLESS SOMEONE WAS EXPLICITLY MADE RESPONSIBLE. Do not infer \
   an owner from who proposed the task, who agreed it was needed, or who \
   talked about it most. "Yes, we should do that" or "agreed" does NOT make \
   someone the assignee. There is an owner only when a person clearly took \
   the task ("I'll do it", "I'll take it", "that's on me") or it was handed \
   to them directly and they accepted ("can you take it?" — "I will"). \
   When owner = null, explain why in owner_missing_reason. If someone did \
   take the task but was never named out loud, still fill in owner: leave \
   name null and set speaker_id — the person is identified by voice, just \
   nameless.

4. DEADLINE:
   - deadline.raw_text is ALWAYS verbatim, as spoken, but only the deadline \
     phrase itself, without the words around it: from "let's say Monday \
     instead" raw_text takes "Monday";
   - the user prompt states RECORDING DATE — the day the call was recorded, \
     taken from the audio file, not from the speech. It is your anchor. \
     Count every relative deadline from it: "tomorrow" is the anchor plus \
     one day, "by Thursday" is the next Thursday on or after the anchor, \
     "next week" is the Monday of the following week. Put the result in \
     resolved_date (ISO YYYY-MM-DD) and set resolution_status = "resolved". \
     In note say what you counted from, for example: "Пораховано від дати \
     запису 2026-09-21 (понеділок)";
   - a deadline can never fall BEFORE the anchor. If your arithmetic gives \
     an earlier date, you made a mistake — recount;
   - if the user prompt says the recording date is UNKNOWN, then \
     resolved_date = null, resolution_status = "relative_unresolved", and \
     note explains that there is nothing to count from. NEVER invent an \
     anchor of your own and never guess the year;
   - a deadline with no point in time at all ("before the release", "when \
     we finish the previous task") stays "relative_unresolved" even when \
     the anchor is known: there is no arithmetic that turns it into a date. \
     Say so in note;
   - no deadline at all -> deadline = null or resolution_status = "none";
   - deadlines are recorded for cancelled and non-accepted tasks too: if it \
     was spoken, it stays part of the history;
   - if the deadline was changed during the call, raw_text holds the LAST \
     version and the previous one stays in lifecycle as deadline_corrected;
   - if two mutually exclusive deadlines were named and no choice was made, \
     keep BOTH in raw_text as spoken and describe the contradiction in note. \
     Picking one of them is forbidden.

5. QUOTES ARE VERBATIM ONLY. Every evidence.text is an exact fragment of the \
   transcript, copied character for character in its original language. Do \
   not paraphrase, do not clean up the speech, do not merge two turns into \
   one quote, do not shorten with ellipses, do not translate. The quote must \
   support exactly what it is attached to: primary_evidence is the main turn \
   about the task, lifecycle evidence is the turn for that event, owner \
   evidence is where the person took the task, deadline evidence is where \
   the deadline was named.

6. WHEN DATA IS MISSING, WRITE clarifications_needed — DO NOT GUESS. Better \
   an honest refusal than a tidy invention.

   THIS IS NOT LEFT TO YOUR JUDGEMENT. Take EVERY commitment you wrote out \
   and run it through the checklist below. Each condition that fires is a \
   SEPARATE clarifications_needed entry with its own quote. Do not merge two \
   different gaps into one wording and do not decide which matters more: if \
   both the owner and the deadline are unclear for one task, that is TWO \
   entries.

   Checklist (run in order, for every commitment):
   a) owner = null -> clarification "assignee undefined" plus why;
   b) two or more incompatible deadlines were voiced and no choice was made \
      -> clarification "deadline contradictory", listing every variant;
   c) resolution_status = "relative_unresolved" -> clarification that the \
      anchor cannot be derived from the recording (one such entry per \
      recording is enough, no need to repeat it per task);
   d) status = "proposed_not_accepted" but there was no explicit refusal \
      either -> clarification "the fate of the task is undecided";
   e) the call broke off before the topic was closed -> clarification about \
      the interruption.

   THE SAME THING MAY APPEAR IN BOTH PLACES. clarifications_needed and \
   open_questions are not mutually exclusive buckets. If you already put a \
   contradictory deadline into open_questions, it MUST STILL be a separate \
   entry in clarifications_needed. Choosing one bucket over the other is an \
   error.

7. open_questions — questions raised during the call that were still \
   unanswered by the end of the recording. They are not tasks: they have no \
   assignee and no deadline. A promise to go and sort such a question out \
   ("I'll find out separately", "I'll check and post in the chat", "I'll ask") \
   is NOT a separate commitment. The question itself goes into \
   open_questions; do not turn it into a task.

8. participants — one entry per speaker_id in the transcript. Fill name only \
   when the name was spoken in the call (the person introduced themselves or \
   was addressed by name), with a supporting quote in name_evidence. Names \
   are never guessed — otherwise name = null.

9. ONE MATTER, ONE ENTRY. Do not split the discussion of a single task into \
   several records. "Look into the scope of the migration" and "do the \
   migration" are one task, not two; the scouting step belongs in lifecycle, \
   not in a commitment of its own.

RESPONSE FORMAT (json, all fields required; a missing value is null).
Field names and the values of status, resolution_status and event are in \
English exactly as listed. Everything you write yourself is in Ukrainian; \
quotes stay in the transcript's language.

{
  "participants": [
    {"speaker_id": "SPEAKER_00", "name": "Анна" | null,
     "name_evidence": {"text": "verbatim quote", "speaker_id": "SPEAKER_00"} | null}
  ],
  "commitments": [
    {
      "id": "c1",
      "title": "коротке формулювання задачі одним рядком (українською)",
      "status": "agreed" | "proposed_not_accepted" | "cancelled",
      "owner": {"name": "Ігор", "speaker_id": "SPEAKER_01",
                "evidence": {"text": "verbatim quote", "speaker_id": "SPEAKER_01"}} | null,
      "owner_missing_reason": "чому власника немає (українською)" | null,
      "deadline": {
        "raw_text": "verbatim deadline phrase from the transcript" | null,
        "resolved_date": "2026-09-28" | null,
        "resolution_status": "resolved" | "relative_unresolved" | "none",
        "note": "чого забракло для точної дати (українською)" | null,
        "evidence": {"text": "verbatim quote", "speaker_id": "SPEAKER_00"} | null
      } | null,
      "lifecycle": [
        {"event": "proposed" | "accepted" | "rejected" | "owner_assigned" |
                  "deadline_set" | "deadline_corrected" | "cancelled",
         "evidence": {"text": "verbatim quote", "speaker_id": "SPEAKER_00"}}
      ],
      "primary_evidence": {"text": "verbatim quote", "speaker_id": "SPEAKER_01"}
    }
  ],
  "open_questions": [
    {"id": "q1", "question": "формулювання питання (українською)",
     "evidence": {"text": "verbatim quote", "speaker_id": "SPEAKER_00"}}
  ],
  "clarifications_needed": [
    {"reason": "що саме лишилося незрозумілим і чому (українською)",
     "evidence": {"text": "verbatim quote", "speaker_id": "SPEAKER_00"} | null}
  ]
}

Take the values of status, resolution_status and event ONLY from the lists \
above, verbatim, in Latin script. Do not put timestamps into evidence — code \
fills them in from the position of the quote it finds. Number the ids in \
sequence: c1, c2, … and q1, q2, …\
"""

USER_PROMPT_TEMPLATE = """\
Transcript of a work call. Each line is: [turn number] timestamp speaker: text.

RECORDING DATE: {anchor}
This is the anchor for every relative deadline. It comes from the audio file,
not from the speech, so it is a fact — use it, do not argue with it.

--- TRANSCRIPT START ---
{transcript}
--- TRANSCRIPT END ---

Analyse the recording by the rules and return a json object in the format \
described. Quotes verbatim from the transcript; everything you write \
yourself in Ukrainian.\
"""

RETRY_SUFFIX = """\

The previous answer failed validation: {error}

This is an internal note about format. Do not mention it or the error itself \
anywhere in the content of your answer — not in clarifications_needed, not \
anywhere else. The analysis of the recording does not change, only its shape \
does. Return the corrected json object in full, in the same format. Json only.\
"""


def render_transcript(utterances: list[Utterance]) -> str:
    """Транскрипт в том виде, в каком его читает модель."""
    return "\n".join(
        f"[{u.index}] {format_timestamp(u.start)}–{format_timestamp(u.end)} "
        f"{u.speaker}: {u.text}"
        for u in utterances
    )


# ── Разбор ответа ────────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^`{3}(?:json)?\s*|\s*`{3}$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    """Снять обёртку в тройные кавычки — модель иногда её дорисовывает."""
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    # Дополнительно вырезаем внешний объект, если вокруг остался мусор.
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


def parse_extraction(raw_text: str) -> ExtractionResult:
    """Текст ответа -> ExtractionResult. Бросает исключение, если не вышло."""
    if not raw_text or not raw_text.strip():
        raise ValueError("модель вернула пустой ответ")
    data = json.loads(_strip_fences(raw_text))
    if not isinstance(data, dict):
        raise ValueError("ожидался json-объект, пришёл " + type(data).__name__)
    return ExtractionResult.model_validate(data)


# ── Результат прохода ────────────────────────────────────────────────────────

@dataclass
class ExtractionRun:
    result: ExtractionResult
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    # Сколько раз пришлось перезапросить. 0 — приняли с первой попытки.
    retries: int = 0
    model: str = ""
    # Ошибки отклонённых попыток — в лог и в отчёт о прогоне.
    attempt_errors: list[str] = field(default_factory=list)
    # Сколько дат снял sanity-контроль: модель посчитала, код не согласился.
    dates_dropped: int = 0


def _render_anchor(anchor: "RecordingDate | None") -> str:
    """Точка отсчёта в том виде, в каком её читает модель.

    День недели пишем словом: без него модель стабильно мажет на «до
    четверга» — вычислять день недели из даты она не умеет.
    """
    if anchor is None or not anchor.date:
        return "UNKNOWN — nothing to count relative deadlines from."
    weekday = anchor.weekday or "unknown weekday"
    return f"{anchor.date} ({weekday})"


def extract_commitments(
    utterances: list[Utterance],
    client: DeepSeekClient | None = None,
    max_retries: int = MAX_RETRIES,
    anchor: "RecordingDate | None" = None,
) -> ExtractionRun:
    """Транскрипт -> обязательства. Токены битых попыток тоже считаются.

    anchor — дата записи из app/recording.py. Есть она — относительные сроки
    раскрываются в конкретные даты; нет — остаются текстом.
    """
    client = client or DeepSeekClient()
    user_prompt = USER_PROMPT_TEMPLATE.format(
        transcript=render_transcript(utterances),
        anchor=_render_anchor(anchor),
    )

    run = ExtractionRun(result=ExtractionResult(), model=client.model)
    last_error: str | None = None

    for attempt in range(max_retries + 1):
        prompt = user_prompt
        if last_error:
            prompt += RETRY_SUFFIX.format(error=last_error)

        resp: LlmResponse = client.complete_json(SYSTEM_PROMPT, prompt)
        run.input_tokens += resp.input_tokens
        run.output_tokens += resp.output_tokens
        run.cached_input_tokens += resp.cached_input_tokens
        run.reasoning_tokens += resp.reasoning_tokens

        try:
            run.result = parse_extraction(resp.text)
            # Арифметику модели проверяет код: дата раньше самой записи —
            # заведомая ошибка, такую снимаем и возвращаем строк текстом.
            if anchor is not None:
                run.dates_dropped = sanity_check_deadlines(run.result, anchor)
            run.retries = attempt
            return run
        except Exception as exc:                       # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"[:800]
            run.attempt_errors.append(last_error)
            print(f"[extract] попытка {attempt + 1} отклонена: {last_error}", flush=True)

    run.retries = max_retries
    raise RuntimeError(
        f"Модель не повернула валідний json за {max_retries + 1} спроб. "
        f"Остання помилка: {last_error}"
    )
