"""Единый клиент OpenRouter (OpenAI-совместимый) для всех уровней LLM-каскада.

Замечание по структуре: в ТЗ дерево проекта не содержит отдельного файла под
LLM-клиент, но он нужен всем трём уровням (screener/analyzer/judge). Держим его
в app/llm_client.py, чтобы избежать циклических импортов между pipeline-модулями.

Что важно и реализовано:
  * base_url = https://openrouter.ai/api/v1 (OpenAI SDK);
  * ТАЙМАУТ на каждый вызов (llm_timeout_sec, для арбитра — JUDGE_TIMEOUT_SEC);
  * семафор на settings.llm_concurrency одновременных LLM-запросов;
  * сетевые ретраи (tenacity, экспоненциальный бэкофф) и ЛОГИЧЕСКИЙ ретрай
    «невалидный JSON/схема» (llm_max_retries, по умолчанию 1) с повторной просьбой
    вернуть корректный JSON;
  * строгий JSON-режим включается только если модель его поддерживает: при ошибке
    провайдера флаг снимается и запрос повторяется (это видно в логе);
  * статистика по токенам/ошибкам (для диагностики в scripts и в логах).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, TypeVar

from loguru import logger
from openai import APIError, APIStatusError, APITimeoutError, AsyncOpenAI, BadRequestError
from pydantic import BaseModel, ValidationError

from app.config import settings

TModel = TypeVar("TModel", bound=BaseModel)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)
_NO_JSON_MODE: set[str] = set()  # модели, у которых response_format=json_object не поддержан

llm_semaphore = asyncio.Semaphore(settings.llm_concurrency)


class LLMError(Exception):
    """Любая ошибка LLM-уровня (сеть, таймаут, невалидный ответ)."""


class LLMTimeout(LLMError):
    """Таймаут (важен для арбитра: при таймауте решение принимается без него)."""


def extract_json(text: str) -> dict[str, Any]:
    """Достаёт JSON-объект из ответа модели (в т.ч. из ```json ...``` ограждения)."""
    if not text:
        raise LLMError("пустой ответ модели")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK_RE.search(cleaned)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            raise LLMError(f"не удалось разобрать JSON: {exc}") from exc
    raise LLMError("в ответе модели нет JSON-объекта")


# Примерная стоимость за 1 миллион токенов (prompt_cost, completion_cost) в USD на OpenRouter
MODEL_PRICING_PER_M: dict[str, tuple[float, float]] = {
    "anthropic/claude-3.5-sonnet": (3.00, 15.00),
    "anthropic/claude-3-haiku": (0.25, 1.25),
    "deepseek/deepseek-chat": (0.14, 0.28),
    "deepseek/deepseek-r1": (0.55, 2.19),
    "qwen/qwen-2.5-72b": (0.35, 0.40),
    "meta-llama/llama-3.1-70b": (0.35, 0.40),
    "meta-llama/llama-3.1-8b": (0.06, 0.06),
    "google/gemini-flash": (0.075, 0.30),
}


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Оценка расхода в USD по известным тарифам OpenRouter."""
    pricing = None
    model_lower = model.lower()
    for key, val in MODEL_PRICING_PER_M.items():
        if key in model_lower:
            pricing = val
            break
    if pricing is None:
        pricing = (0.50, 1.50)  # разумный дефолт
    prompt_rate, completion_rate = pricing
    return (prompt_tokens / 1_000_000.0) * prompt_rate + (completion_tokens / 1_000_000.0) * completion_rate


class OpenRouterClient:

    """Тонкая обёртка над OpenAI SDK с ретраями, семафором и валидацией pydantic."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else settings.openrouter_api_key
        self.base_url = base_url or settings.openrouter_base_url
        self._client: AsyncOpenAI | None = None
        self.stats: dict[str, Any] = {
            "requests": 0,
            "failures": 0,
            "invalid_json": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "by_model": {},
        }

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.api_key or "missing-key",
                base_url=self.base_url,
                default_headers={
                    "HTTP-Referer": settings.openrouter_referer,
                    "X-Title": settings.openrouter_title,
                },
                # SDK сам повторяет только транспортные сбои/429/5xx (llm_http_retries),
                # а «невалидный JSON» мы ретраим своим циклом выше — так честнее по логам.
                max_retries=settings.llm_http_retries,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ------------------------------------------------------------------ call
    async def complete_json(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        timeout: float | None = None,
        json_mode: bool | None = None,
        schema_hint: str | None = None,
        retries: int | None = None,
        label: str = "llm",
        match_id: int | str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Запрос к модели с гарантией «верни JSON». Возвращает (parsed, raw_text)."""
        if not self.available:
            raise LLMError("OPENROUTER_API_KEY не задан (см. SETUP.md)")
        timeout = timeout or settings.llm_timeout_sec
        retries = settings.llm_max_retries if retries is None else retries
        use_json_mode = (settings.llm_use_json_mode if json_mode is None else json_mode) and model not in _NO_JSON_MODE
        attempts = retries + 1
        last_error: Exception | None = None
        log = logger.bind(match_id=match_id, model=model, level=label)

        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                async with llm_semaphore:
                    response = await self._request(
                        model=model,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        timeout=timeout,
                        json_mode=use_json_mode,
                    )
            except BadRequestError as exc:
                # Скорее всего, модель не поддерживает response_format=json_object.
                if use_json_mode:
                    _NO_JSON_MODE.add(model)
                    logger.warning(
                        "{}: модель {} не приняла response_format=json_object ({}) — повтор без него",
                        label, model, exc,
                    )
                    use_json_mode = False
                    continue
                self.stats["failures"] += 1
                raise LLMError(f"{label}: BadRequest от {model}: {exc}") from exc
            except APITimeoutError as exc:
                self.stats["failures"] += 1
                last_error = LLMTimeout(f"{label}: таймаут {timeout}s на модели {model}")
                logger.warning("{}: таймаут модели {} (попытка {}/{})", label, model, attempt, attempts)
                if attempt < attempts:
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                raise last_error from exc
            except (APIStatusError, APIError) as exc:
                self.stats["failures"] += 1
                last_error = LLMError(f"{label}: ошибка API {model}: {exc}")
                logger.warning("{}: ошибка API {} (попытка {}/{})", label, exc, attempt, attempts)
                if attempt < attempts:
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                raise last_error from exc

            latency = time.monotonic() - started
            usage = getattr(response, "usage", None)
            self._track_usage(model, usage)
            raw = (response.choices[0].message.content or "").strip() if response.choices else ""
            log.info(
                "{}: {} ← {:.1f}s, tokens={}/{}",
                label, model, latency,
                getattr(usage, "prompt_tokens", "?"), getattr(usage, "completion_tokens", "?"),
            )

            try:
                parsed = extract_json(raw)
                return parsed, raw
            except LLMError as exc:
                self.stats["invalid_json"] += 1
                last_error = exc
                log.warning("{}: невалидный JSON (попытка {}/{}): {}", label, attempt, attempts, str(exc)[:160])
                if attempt < attempts:
                    # Просим модель исправиться: тот же смысл + требование чистого JSON.
                    user_prompt = (
                        f"{user_prompt}\n\nВАЖНО: предыдущий ответ был невалидным JSON ({exc}). "
                        f"Верни ТОЛЬКО валидный JSON-объект без пояснений и без markdown."
                        + (f"\nСхема: {schema_hint}" if schema_hint else "")
                    )
                    continue
        raise last_error or LLMError(f"{label}: не удалось получить валидный ответ")

    async def _request(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        timeout: float,
        json_mode: bool,
    ):
        self.stats["requests"] += 1
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "timeout": timeout,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return await self.client.chat.completions.create(**kwargs)

    def _track_usage(self, model: str, usage: Any) -> None:
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        self.stats["prompt_tokens"] += prompt_tokens
        self.stats["completion_tokens"] += completion_tokens
        per_model = self.stats["by_model"].setdefault(model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        per_model["calls"] += 1
        per_model["prompt_tokens"] += prompt_tokens
        per_model["completion_tokens"] += completion_tokens

    # -------------------------------------------------------------- validate
    async def complete_model(
        self,
        model_cls: type[TModel],
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        timeout: float | None = None,
        label: str = "llm",
        match_id: int | str | None = None,
        repair: bool = True,
    ) -> tuple[TModel, str]:
        """Запрос + pydantic-валидация. При невалидной схеме — 1 повтор (repair)."""
        parsed, raw = await self.complete_json(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            label=label,
            match_id=match_id,
        )
        try:
            return model_cls.model_validate(parsed), raw
        except ValidationError as exc:
            if not repair:
                raise LLMError(f"{label}: ответ не прошёл валидацию схемы: {exc}") from exc
            logger.warning("{}: ответ не прошёл валидацию схемы ({}) — просим исправить", label, str(exc)[:200])
            schema = json.dumps(model_cls.model_json_schema(), ensure_ascii=False)[:1800]
            parsed_retry, raw_retry = await self.complete_json(
                model=model,
                system_prompt=system_prompt,
                user_prompt=(
                    f"{user_prompt}\n\nТвой предыдущий JSON не соответствует схеме. "
                    f"Ошибки валидации: {str(exc)[:600]}\nВерни JSON строго по схеме:\n{schema}"
                ),
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
                label=f"{label}:repair",
                match_id=match_id,
                retries=0,
            )
            try:
                return model_cls.model_validate(parsed_retry), raw_retry
            except ValidationError as exc2:
                raise LLMError(f"{label}: повторный ответ тоже невалиден: {exc2}") from exc2

    def usage_summary(self) -> dict[str, Any]:
        total_cost = 0.0
        by_model_summary: dict[str, Any] = {}
        for model_name, info in self.stats["by_model"].items():
            cost = estimate_cost_usd(model_name, info.get("prompt_tokens", 0), info.get("completion_tokens", 0))
            total_cost += cost
            by_model_summary[model_name] = {
                **info,
                "estimated_cost_usd": round(cost, 4),
            }
        return {
            "requests": self.stats["requests"],
            "failures": self.stats["failures"],
            "invalid_json": self.stats["invalid_json"],
            "prompt_tokens": self.stats["prompt_tokens"],
            "completion_tokens": self.stats["completion_tokens"],
            "total_tokens": self.stats["prompt_tokens"] + self.stats["completion_tokens"],
            "estimated_cost_usd": round(total_cost, 4),
            "by_model": by_model_summary,
        }


def format_llm_stats_text(client: OpenRouterClient | None = None) -> str:
    """Форматирует красивый отчёт по использованию LLM для админки в Telegram."""
    cli = client or get_llm_client()
    summary = cli.usage_summary()

    lines = [
        "💰 <b>Мониторинг расходов и токенов LLM</b>\n",
        f"• <b>Всего запросов:</b> {summary['requests']} (ошибок: {summary['failures']}, испр. JSON: {summary['invalid_json']})",
        f"• <b>Токены:</b> {summary['total_tokens']:,} (вход: {summary['prompt_tokens']:,} | выход: {summary['completion_tokens']:,})",
        f"• <b>Оценка стоимости:</b> ~${summary['estimated_cost_usd']:.4f} USD\n",
    ]

    by_model = summary.get("by_model", {})
    if by_model:
        lines.append("<b>Расход по моделям:</b>")
        for m_name, m_info in by_model.items():
            lines.append(
                f"▫️ <code>{m_name}</code>:\n"
                f"   {m_info['calls']} выз. | {m_info['prompt_tokens'] + m_info['completion_tokens']:,} tok "
                f"(~${m_info['estimated_cost_usd']:.4f})"
            )
    else:
        lines.append("<i>Запросов к LLM в этой сессии ещё не было.</i>")

    lines.append(
        f"\n⚙️ <b>Текущие модели:</b>\n"
        f"• Скринер: <code>{settings.screener_model}</code>\n"
        f"• Аналитик: <code>{settings.analyzer_model}</code>\n"
        f"• Арбитр: <code>{settings.judge_model if settings.judge_enabled else 'выключен'}</code>"
    )
    return "\n".join(lines)


_client: OpenRouterClient | None = None


def get_llm_client() -> OpenRouterClient:
    """Единый клиент на процесс (переиспользует HTTP-соединения)."""
    global _client
    if _client is None:
        _client = OpenRouterClient()
    return _client


async def close_llm_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
