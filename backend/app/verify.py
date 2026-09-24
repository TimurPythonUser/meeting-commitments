"""Валидатор цитат — защита от галлюцинаций. Детерминированный Python, без LLM.

Модель обязана возвращать дословные цитаты. Здесь код проверяет, что цитата
действительно есть в транскрипте, и пересчитывает таймкоды из найденной
позиции — координатам, которые придумала модель, веры нет.

Порядок проверки одной цитаты:
    1. нормализация обеих сторон (нижний регистр, ґ->г и ё->е, без
       пунктуации и апострофов, схлопнутые пробелы);
    2. поиск подстроки — точное совпадение, score 1.0;
    3. промах -> difflib.SequenceMatcher по скользящему окну транскрипта,
       окно выравнено по словам; лучший score сравнивается с порогом 0.85;
    4. нашли -> start/end/speaker_id берутся из перекрывающихся Utterance,
       выставляется timestamps_recomputed;
    5. не нашли -> quote_verified = false.

Обязательство без подтверждённой primary_evidence ФИЗИЧЕСКИ не попадает в
активный список: verify_result() уносит его в unverified_commitments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .schemas import (
    Clarification,
    Commitment,
    Evidence,
    ExtractionResult,
    OpenQuestion,
    Participant,
    Verification,
)
from .stt.base import Utterance

# Порог нечёткого совпадения. Ниже — считаем, что цитаты в записи нет.
# 0.85 прощает разницу в окончании и пропущенное слово, но не пересказ.
MATCH_THRESHOLD = 0.85

# Цитаты короче этого (в символах после нормализации) не проверяем нечётко:
# на «да» и «ага» скользящее окно даёт ложные срабатывания где угодно.
MIN_FUZZY_LEN = 12

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Нижний регистр, склейка похожих букв, без пунктуации, схлопнутые пробелы.

    ґ->г и ё->е применяются к ОБЕИМ сторонам сравнения, поэтому совпадений
    они могут только добавить: whisper и LLM нередко пишут одно и то же слово
    разными буквами. Апострофы в украинских словах съедает _PUNCT_RE — тоже
    симметрично с обеих сторон, так что на поиск это не влияет.
    """
    lowered = (
        text.lower()
        .replace("\u0491", "\u0433")
        .replace("\u0451", "\u0435")
        .replace("-", " ")
    )
    return _SPACE_RE.sub(" ", _PUNCT_RE.sub(" ", lowered)).strip()


# ── Индекс транскрипта ───────────────────────────────────────────────────────

@dataclass
class _Span:
    """Кусок нормализованного текста, принадлежащий одной реплике."""

    char_start: int
    char_end: int
    utt: Utterance


@dataclass
class QuoteMatch:
    start: float
    end: float
    speaker_id: str | None
    score: float


class TranscriptIndex:
    """Нормализованный транскрипт одной строкой + карта обратно в реплики.

    Строится один раз на задачу: цитат в ответе модели десятки, а реплик
    в трёхминутной записи под сотню — пересобирать индекс на каждую цитату
    расточительно.
    """

    def __init__(self, utterances: list[Utterance]) -> None:
        self.utterances = utterances
        self._spans: list[_Span] = []
        parts: list[str] = []
        cursor = 0
        for utt in utterances:
            norm = normalize(utt.text)
            if not norm:
                continue
            if parts:
                cursor += 1                      # пробел-разделитель между репликами
            self._spans.append(_Span(cursor, cursor + len(norm), utt))
            parts.append(norm)
            cursor += len(norm)
        self.haystack = " ".join(parts)

        # Границы слов — по ним двигаем окно при нечётком поиске.
        self._words: list[tuple[int, int]] = [
            (m.start(), m.end()) for m in re.finditer(r"\S+", self.haystack)
        ]

    # -- поиск ---------------------------------------------------------------

    def find(self, quote: str) -> QuoteMatch | None:
        """Найти цитату. None — в транскрипте такого не говорили."""
        needle = normalize(quote)
        if not needle or not self.haystack:
            return None

        pos = self.haystack.find(needle)
        if pos != -1:
            return self._span_to_match(pos, pos + len(needle), 1.0)

        if len(needle) < MIN_FUZZY_LEN:
            # Слишком короткая цитата: точного совпадения нет, а нечёткое
            # на такой длине ничего не доказывает.
            return None

        found = self._fuzzy(needle)
        if found is None:
            return None
        start_char, end_char, score = found
        if score < MATCH_THRESHOLD:
            return None
        return self._span_to_match(start_char, end_char, score)

    def _fuzzy(self, needle: str) -> tuple[int, int, float] | None:
        """Лучшее окно транскрипта по SequenceMatcher. Окна режутся по словам."""
        if not self._words:
            return None

        matcher = SequenceMatcher(autojunk=False)
        matcher.set_seq2(needle)

        target = len(needle)
        best: tuple[int, int, float] | None = None

        for i, (w_start, _) in enumerate(self._words):
            # Набираем слова, пока окно не перерастёт цитату. Чуть длиннее —
            # нормально: лишний хвост слабее штрафует ratio, чем обрезанное слово.
            j = i
            while j < len(self._words) and self._words[j][1] - w_start < target:
                j += 1
            j = min(j, len(self._words) - 1)

            # Проверяем окно и его соседей по длине — цитата может быть
            # на слово короче или длиннее подобранного куска.
            for k in (j - 1, j, j + 1):
                if k < i or k >= len(self._words):
                    continue
                w_end = self._words[k][1]
                window = self.haystack[w_start:w_end]
                matcher.set_seq1(window)
                # Дешёвые верхние оценки: отсекают заведомо чужие окна
                # до полного прохода по диагонали.
                if matcher.real_quick_ratio() < MATCH_THRESHOLD:
                    continue
                if matcher.quick_ratio() < MATCH_THRESHOLD:
                    continue
                score = matcher.ratio()
                if best is None or score > best[2]:
                    best = (w_start, w_end, score)

        return best

    def _span_to_match(
        self, start_char: int, end_char: int, score: float
    ) -> QuoteMatch:
        """Позиция в нормализованном тексте -> таймкоды перекрытых реплик."""
        hits = [
            s for s in self._spans
            if s.char_start < end_char and s.char_end > start_char
        ]
        if not hits:
            # Индекс и позиция разошлись — такого быть не должно,
            # но молча отдавать нули хуже, чем взять всю запись.
            first, last = self._spans[0], self._spans[-1]
            hits = [first, last]

        start = min(h.utt.start for h in hits)
        end = max(h.utt.end for h in hits)

        # Спикер — тот, чьей речи в найденном куске больше всего.
        def covered(span: _Span) -> int:
            return min(span.char_end, end_char) - max(span.char_start, start_char)

        speaker = max(hits, key=covered).utt.speaker
        return QuoteMatch(
            start=round(start, 2),
            end=round(end, 2),
            speaker_id=speaker,
            score=round(score, 4),
        )


# ── Проверка одной цитаты ────────────────────────────────────────────────────

def verify_evidence(
    evidence: Evidence | None, index: TranscriptIndex
) -> tuple[Evidence | None, float, bool]:
    """Проверить цитату и проставить ей реальные таймкоды.

    Возвращает (evidence | None, score, timestamps_recomputed).
    None — цитаты в транскрипте нет, ссылаться не на что.
    """
    if evidence is None or not (evidence.text or "").strip():
        return None, 0.0, False

    match = index.find(evidence.text)
    if match is None:
        return None, 0.0, False

    recomputed = (
        abs(evidence.start - match.start) > 0.05 or abs(evidence.end - match.end) > 0.05
    )
    fixed = evidence.model_copy(
        update={
            "start": match.start,
            "end": match.end,
            # speaker_id модели тоже не доверяем — берём из диаризации.
            "speaker_id": match.speaker_id,
        }
    )
    return fixed, match.score, recomputed


# ── Проверка обязательства ───────────────────────────────────────────────────

def verify_commitment(commitment: Commitment, index: TranscriptIndex) -> Commitment:
    """Проверить все цитаты пункта и заполнить блок verification.

    Судьбу пункта решает primary_evidence. Побочные цитаты (владелец, срок,
    события жизненного цикла) при провале просто снимаются: непроверяемая
    ссылка на запись хуже её отсутствия — по ней нельзя перемотать плеер.
    """
    item = commitment.model_copy(deep=True)

    primary, score, recomputed = verify_evidence(item.primary_evidence, index)

    if primary is None:
        # Главной цитаты нет или она не подтвердилась. Прежде чем хоронить
        # пункт, пробуем поднять в главные любую подтверждённую цитату
        # жизненного цикла — но только если primary не было вовсе.
        if item.primary_evidence is None:
            for step in item.lifecycle:
                candidate, cand_score, cand_recomputed = verify_evidence(
                    step.evidence, index
                )
                if candidate is not None:
                    primary, score, recomputed = candidate, cand_score, cand_recomputed
                    break

    item.primary_evidence = primary
    item.verification = Verification(
        quote_verified=primary is not None,
        match_score=score,
        timestamps_recomputed=bool(primary is not None and recomputed),
    )

    if item.owner is not None:
        item.owner.evidence, _, _ = verify_evidence(item.owner.evidence, index)
    if item.deadline is not None:
        item.deadline.evidence, _, _ = verify_evidence(item.deadline.evidence, index)
    for step in item.lifecycle:
        step.evidence, _, _ = verify_evidence(step.evidence, index)

    return item


def _verify_participant(p: Participant, index: TranscriptIndex) -> Participant:
    """Имя без подтверждения в записи — выдумка, поэтому снимается вместе с ним."""
    item = p.model_copy(deep=True)
    if item.name is None:
        item.name_evidence = None
        return item
    evidence, _, _ = verify_evidence(item.name_evidence, index)
    if evidence is None:
        item.name = None
        item.name_evidence = None
    else:
        item.name_evidence = evidence
    return item


def _verify_question(q: OpenQuestion, index: TranscriptIndex) -> OpenQuestion:
    item = q.model_copy(deep=True)
    item.evidence, _, _ = verify_evidence(item.evidence, index)
    return item


def _verify_clarification(c: Clarification, index: TranscriptIndex) -> Clarification:
    item = c.model_copy(deep=True)
    item.evidence, _, _ = verify_evidence(item.evidence, index)
    return item


# ── Проверка всего ответа ────────────────────────────────────────────────────

@dataclass
class VerifiedExtraction:
    """Результат проверки: активные пункты отделены от неподтверждённых."""

    participants: list[Participant]
    commitments: list[Commitment]
    unverified_commitments: list[Commitment]
    open_questions: list[OpenQuestion]
    clarifications_needed: list[Clarification]

    @property
    def dropped(self) -> int:
        return len(self.unverified_commitments)


def verify_result(
    extraction: ExtractionResult, utterances: list[Utterance]
) -> VerifiedExtraction:
    """Сверить весь ответ модели с транскриптом.

    Пункты с неподтверждённой цитатой уходят в unverified_commitments и в
    активный список не попадают ни при каких условиях.
    """
    index = TranscriptIndex(utterances)

    active: list[Commitment] = []
    rejected: list[Commitment] = []
    for commitment in extraction.commitments:
        checked = verify_commitment(commitment, index)
        (active if checked.verification.quote_verified else rejected).append(checked)

    return VerifiedExtraction(
        participants=[_verify_participant(p, index) for p in extraction.participants],
        commitments=active,
        unverified_commitments=rejected,
        open_questions=[_verify_question(q, index) for q in extraction.open_questions],
        clarifications_needed=[
            _verify_clarification(c, index) for c in extraction.clarifications_needed
        ],
    )
