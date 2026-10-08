"""Ур.1 — дешёвая LLM-отсечка (SCREENER_MODEL, напр. deepseek/deepseek-v4-flash).

Вход: краткая сводка матча (команды, лига, кэфы с implied, наличие статистики).
Выход: {"verdict": "pass"|"skip", "reason": "..."} — pydantic-валидация, 1 ретрай.

Поведение при ошибках: если LLM недоступна/ответ невалиден после ретрая — матч
ПРОПУСКАЕТСЯ дальше (fail-open) с пометкой в логе. Так деградация LLM не «съедает»
поток матчей; расходы ограничены тем, что дорогой аналитик всё равно работает
только на прошедших скринер.
"""

from __future__ import annotations

from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field

from app.config import settings
from app.llm_client import LLMError, get_llm_client
from app.pipeline.llm_prompts import SCREENER_SYSTEM, build_screener_user_prompt


class ScreenerVerdict(BaseModel):
    verdict: Literal["pass", "skip"]
    reason: str = Field(default="", max_length=500)


async def llm_screen(summary: dict[str, Any], match_id: int | str | None = None) -> ScreenerVerdict:
    """Быстрый отсев матча дешёвой моделью."""
    client = get_llm_client()
    if not client.available:
        logger.warning("llm_screener: OPENROUTER_API_KEY не задан — пропускаем уровень 1 (fail-open)")
        return ScreenerVerdict(verdict="pass", reason="LLM недоступна, уровень 1 пропущен")
    try:
        verdict, _raw = await client.complete_model(
            ScreenerVerdict,
            model=settings.screener_model,
            system_prompt=SCREENER_SYSTEM,
            user_prompt=build_screener_user_prompt(summary),
            max_tokens=settings.llm_max_tokens_screener,
            temperature=settings.llm_temperature_screener,
            timeout=settings.llm_screener_timeout_sec,
            label="screener",
            match_id=match_id,
        )
        logger.info("llm_screener: match_id={} → {} ({})", match_id, verdict.verdict, verdict.reason[:120])
        return verdict
    except LLMError as exc:
        logger.warning("llm_screener: match_id={} — ошибка LLM: {} — fail-open (pass)", match_id, exc)
        return ScreenerVerdict(verdict="pass", reason=f"ошибка LLM, пропускаем дальше: {str(exc)[:120]}")


async def screen_many(summaries: list[tuple[int, dict[str, Any]]]) -> dict[int, ScreenerVerdict]:
    """Пакетный прогон уровня 1 (последовательно — внутри клиента есть семафор)."""
    import asyncio

    results: dict[int, ScreenerVerdict] = {}
    tasks = [llm_screen(summary, match_id) for match_id, summary in summaries]
    for (match_id, _), verdict in zip(summaries, await asyncio.gather(*tasks, return_exceptions=False)):
        results[match_id] = verdict
    passed = sum(1 for v in results.values() if v.verdict == "pass")
    logger.info("llm_screener: прошли {} из {} матчей", passed, len(results))
    return results
