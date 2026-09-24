"""Метрики прогона: тайминги по этапам, токены, ретраи, себестоимость.

Цены DeepSeek на 2026-09-22:
    вход  $0.30 / 1M  (cache miss)
    вход  $0.006 / 1M (cache hit)
    выход $1.20 / 1M
Биллинг peak / off-peak, off-peak вдвое дешевле. Считаем по peak — это
верхняя оценка, и время замера фиксируется в assumptions.

STT крутится локально, поэтому его переменная стоимость $0. Хостинг
(железо под faster-whisper и pyannote) в cost_usd не входит и оговаривается
отдельной строкой — иначе цифра выглядела бы нечестно дёшево.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .schemas import CostUsd, LlmMetrics, Metrics, TimingSec

# ── Прайс-лист, $ за 1M токенов ──────────────────────────────────────────────

PRICE_INPUT_MISS = 0.30
PRICE_INPUT_HIT = 0.006
PRICE_OUTPUT = 1.20

# Скидка вне часа пик. Фиксируем как константу, чтобы в отчёте было видно,
# откуда взялась половина цены, если замер делали ночью.
OFF_PEAK_MULTIPLIER = 0.5

PRICES_DATED = "2026-09-22"

# ── Хостинг ──────────────────────────────────────────────────────────────────
# Цена по публичному прайсу Hetzner, сверено 10.09.2026:
# CX33 $9.99/мес + Primary IPv4 $0.60/мес, без НДС.
#
# Конфигурация выбрана не с потолка: faster-whisper и pyannote держат в
# памяти гигабайты, на инстансе поменьше прогрев моделей не проходит.
HOSTING_NAME = "Hetzner CX33 — 4 vCPU / 8 ГБ / 40 ГБ"
HOSTING_USD_PER_MONTH = 10.59
HOURS_PER_MONTH = 24 * 30
HOSTING_USD_PER_HOUR = HOSTING_USD_PER_MONTH / HOURS_PER_MONTH

# Допущение по объёму: столько записей сервис обрабатывает за месяц.
# Без него аренду не разнести по прогонам — инстанс тарифицируется
# временем, а не числом запросов.
RUNS_PER_MONTH = 300


def llm_cost_usd(
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    off_peak: bool = False,
) -> float:
    """Стоимость запросов к DeepSeek в долларах.

    cached_input_tokens — часть входа, попавшая в кэш: она тарифицируется
    по $0.006/1M, а не по $0.30/1M, и вычитается из cache-miss части.
    """
    cached = max(0, min(cached_input_tokens, input_tokens))
    miss = input_tokens - cached
    cost = (
        miss * PRICE_INPUT_MISS
        + cached * PRICE_INPUT_HIT
        + output_tokens * PRICE_OUTPUT
    ) / 1_000_000
    if off_peak:
        cost *= OFF_PEAK_MULTIPLIER
    return cost


@dataclass
class MetricsCollector:
    """Собирает метрики по ходу прогона одной задачи.

    Тайминги пишутся через stage(); что не измерено — остаётся нулём,
    а не выдумывается.
    """

    timing: dict[str, float] = field(default_factory=dict)
    model: str = "deepseek-flash"
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    retries: int = 0
    off_peak: bool = False
    audio_duration_sec: float = 0.0
    stt_model: str = "small"
    extra_assumptions: list[str] = field(default_factory=list)

    _t_start: float = field(default_factory=time.perf_counter)
    _measured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # -- тайминги ------------------------------------------------------------

    @contextmanager
    def stage(self, name: str):
        """Замерить этап: with metrics.stage("llm"): ..."""
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.timing[name] = round(self.timing.get(name, 0.0) + time.perf_counter() - t0, 3)

    def add_timing(self, values: dict[str, float]) -> None:
        """Влить тайминги, померенные внутри другого слоя (например, STT)."""
        for key, value in values.items():
            self.timing[key] = round(self.timing.get(key, 0.0) + float(value), 3)

    # -- токены --------------------------------------------------------------

    def add_llm_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        reasoning_tokens: int = 0,
        retries: int = 0,
        model: str | None = None,
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cached_input_tokens += cached_input_tokens
        self.reasoning_tokens += reasoning_tokens
        # Ретраи — не сумма, а счётчик попыток сверх первой на этой задаче.
        self.retries += retries
        if model:
            self.model = model

    # -- сборка --------------------------------------------------------------

    @property
    def total_sec(self) -> float:
        return round(time.perf_counter() - self._t_start, 3)

    def cost(self) -> CostUsd:
        llm = llm_cost_usd(
            self.input_tokens,
            self.output_tokens,
            self.cached_input_tokens,
            off_peak=self.off_peak,
        )
        total = llm  # STT локальный, переменная стоимость нулевая
        minutes = self.audio_duration_sec / 60.0
        per_minute = total / minutes if minutes > 0 else 0.0

        # Аренда считается по амортизации, а не по занятости инстанса:
        # сервер тикает круглосуточно независимо от того, шлёт кто-то
        # запросы или нет. По занятости вышло бы в разы меньше — и это
        # была бы цифра для сервера, загруженного на 100%, то есть неправда.
        hosting_per_run = HOSTING_USD_PER_MONTH / RUNS_PER_MONTH
        total_with_hosting = total + hosting_per_run
        return CostUsd(
            stt=0.0,
            llm=round(llm, 6),
            total=round(total, 6),
            per_audio_minute=round(per_minute, 6),
            hosting_name=HOSTING_NAME,
            hosting_per_month=HOSTING_USD_PER_MONTH,
            hosting_per_run=round(hosting_per_run, 6),
            runs_per_month=RUNS_PER_MONTH,
            total_with_hosting=round(total_with_hosting, 6),
            per_audio_minute_with_hosting=round(
                total_with_hosting / minutes if minutes > 0 else 0.0, 6
            ),
        )

    def assumptions(self) -> list[str]:
        tariff = "off-peak" if self.off_peak else "peak"
        stamp = self._measured_at.strftime("%Y-%m-%d %H:%M UTC")
        items = [
            f"STT локальний (faster-whisper «{self.stt_model}» + pyannote) — "
            "змінна вартість $0. Це НЕ означає «безкоштовно»: "
            "див. рядок хостингу",
            f"Хостинг: {HOSTING_NAME}, ${HOSTING_USD_PER_MONTH:.2f}/міс "
            "(публічний прайс Hetzner, звірено 10.09.2026, без ПДВ)",
            f"Хостинг на прогін: ${HOSTING_USD_PER_MONTH:.2f}/міс ÷ "
            f"{RUNS_PER_MONTH} записів = ${HOSTING_USD_PER_MONTH / RUNS_PER_MONTH:.4f}. "
            "Рахуємо за амортизацією, а не за часом зайнятості інстансу: платять "
            "за місяць оренди, а не за секунди завантаження процесора.",
            "Безкоштовні кредити провайдера не враховуються: вони закінчуються, "
            "а прайс лишається.",
            f"{self.model}: ${PRICE_INPUT_MISS:.2f}/1M вхід, "
            f"${PRICE_OUTPUT:.2f}/1M вихід, тариф {tariff}, замір {stamp}",
            f"ціни зафіксовані на {PRICES_DATED}; off-peak удвічі дешевший за peak",
        ]
        if self.cached_input_tokens:
            items.append(
                f"{self.cached_input_tokens} вхідних токенів потрапили в кеш і "
                f"пораховані по ${PRICE_INPUT_HIT}/1M"
            )
        if self.reasoning_tokens:
            items.append(
                f"{self.reasoning_tokens} із {self.output_tokens} вихідних токенів — "
                "міркування моделі, оплачується за ціною виходу"
            )
        if self.retries:
            items.append(
                f"ретраїв через невалідний json: {self.retries}; "
                "їхні токени включені у вартість"
            )
        return items + self.extra_assumptions

    def build(self) -> Metrics:
        timing = TimingSec(
            ffmpeg=self.timing.get("ffmpeg", 0.0),
            stt=self.timing.get("stt", 0.0),
            diarization=self.timing.get("diarization", 0.0),
            llm=self.timing.get("llm", 0.0),
            validation=self.timing.get("validation", 0.0),
            total=self.total_sec,
        )
        return Metrics(
            timing_sec=timing,
            llm=LlmMetrics(
                model=self.model,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                retries=self.retries,
            ),
            cost_usd=self.cost(),
            assumptions=self.assumptions(),
        )
