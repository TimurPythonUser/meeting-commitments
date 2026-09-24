"""Адаптер DeepSeek: единственная точка, где проект ходит в LLM.

SDK openai, направленный на api.deepseek.com. Возвращает сырой текст ответа
и расход токенов — разбор и валидация живут в extract.py, стоимость
считается в metrics.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Рабочий ID на 2026-09-22 — только он.
# deepseek-chat и deepseek-reasoner сняты 24.07.2026,
# deepseek-v4-flash снят 10.09.2026. Подставлять их обратно бессмысленно.
MODEL = "deepseek-flash"
BASE_URL = "https://api.deepseek.com"

DEFAULT_TIMEOUT = float(os.environ.get("DEEPSEEK_TIMEOUT", "180"))


@dataclass
class LlmResponse:
    """Ответ модели + всё, что нужно метрикам."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    # Часть входа, попавшая в кэш: тарифицируется по $0.006/1M вместо $0.30/1M.
    cached_input_tokens: int = 0
    # deepseek-flash рассуждает перед ответом. Эти токены ВХОДЯТ в
    # output_tokens и оплачиваются по цене выхода — считаем их отдельно,
    # чтобы в отчёте было видно, за что заплачено.
    reasoning_tokens: int = 0
    model: str = MODEL
    finish_reason: str | None = None


class DeepSeekClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = MODEL,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY не задано — витягнути зобовʼязання неможливо."
            )
        from openai import OpenAI

        self._client = OpenAI(api_key=key, base_url=base_url, timeout=timeout)
        self.model = model

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 32000,
    ) -> LlmResponse:
        """Запрос в JSON-режиме.

        Строгой json_schema на этом эндпоинте нет — только json_object,
        то есть гарантирован синтаксически валидный JSON, но не его форма.
        Поэтому форму проверяет Pydantic в extract.py, а при провале идёт ретрай.

        Требование API DeepSeek: слово «json» обязано встречаться в промпте,
        иначе запрос отклоняется. За это отвечает extract.py.

        ВНИМАНИЕ: deepseek-flash сначала рассуждает (reasoning_content) и эти
        токены списываются с max_tokens. Ставить лимит впритык к ожидаемому
        JSON нельзя — ответ просто не успеет начаться.
        """
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=temperature,
            max_tokens=max_tokens,
        )

        choice = resp.choices[0] if resp.choices else None
        text = (choice.message.content if choice and choice.message else "") or ""

        usage = getattr(resp, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None)
        out_details = getattr(usage, "completion_tokens_details", None)
        finish = getattr(choice, "finish_reason", None) if choice else None

        # Лимит съеден рассуждением, до ответа дело не дошло. Отдельная ошибка,
        # а не «пустой JSON»: ретрай тем же промптом не поможет, нужен запас.
        if not text.strip() and finish == "length":
            raise RuntimeError(
                f"Модель вперлася в max_tokens={max_tokens} на міркуванні "
                "й не повернула відповідь. Підніміть ліміт."
            )

        return LlmResponse(
            text=text,
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            cached_input_tokens=int(getattr(details, "cached_tokens", 0) or 0),
            reasoning_tokens=int(getattr(out_details, "reasoning_tokens", 0) or 0),
            model=self.model,
            finish_reason=finish,
        )
