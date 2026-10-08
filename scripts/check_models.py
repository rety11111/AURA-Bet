#!/usr/bin/env python
"""Проверка моделей OpenRouter, настроенных в BetSignals.

Что делает скрипт:
  1. Читает SCREENER_MODEL / ANALYZER_MODEL / JUDGE_MODEL из конфига (.env).
  2. Сверяет ID с живым каталогом OpenRouter (https://openrouter.ai/api/v1/models).
     Если ID не найден — печатает похожие варианты, чтобы было что подставить.
  3. Безопасно дергает каждую модель крошечным пробным запросом
     («верни JSON {ok:true}») — проверяет ключ, доступность и JSON-режим.
  4. Печатает расход токенов за пробу и итоговый вердикт.

Запуск:
    python scripts/check_models.py            # сверка + пробные запросы
    python scripts/check_models.py --no-probe # только сверка ID (без трат на LLM)

ВАЖНО: ID моделей и цены меняются часто. Скрипт нужен, чтобы вы могли сами
убедиться, что модель существует ПРЯМО СЕЙЧАС, а не верить комментарию в коде.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from rapidfuzz import fuzz, process

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.llm_client import LLMError, OpenRouterClient  # noqa: E402

PROBE_SYSTEM = "Ты — сервисный пинг. Отвечай только JSON, без пояснений и markdown."
PROBE_USER = 'Верни ровно такой JSON: {"ok": true, "model_working": true, "sum": 40}'


def configured_models() -> dict[str, str]:
    return {
        "SCREENER_MODEL": settings.screener_model,
        "ANALYZER_MODEL": settings.analyzer_model,
        "JUDGE_MODEL": settings.judge_model,
    }


def banner(text: str) -> None:
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


async def fetch_catalog() -> list[dict[str, Any]]:
    url = settings.openrouter_base_url.rstrip("/") + "/models"
    async with httpx.AsyncClient(timeout=settings.http_timeout_sec) as client:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    return payload.get("data", []) if isinstance(payload, dict) else []


def describe_model(entry: dict[str, Any]) -> str:
    pricing = entry.get("pricing") or {}
    try:
        prompt_cost = float(pricing.get("prompt", 0) or 0) * 1_000_000
        completion_cost = float(pricing.get("completion", 0) or 0) * 1_000_000
        price = f"${prompt_cost:.4f}/${completion_cost:.4f} за 1M in/out"
    except (TypeError, ValueError):
        price = "цена неизвестна"
    context = entry.get("context_length") or (entry.get("top_provider") or {}).get("context_length")
    modalities = (entry.get("architecture") or {}).get("input_modalities")
    return f"ctx={context}, {price}, модальности={modalities}"


def suggest_similar(model_id: str, all_ids: list[str]) -> list[str]:
    if not all_ids:
        return []
    matches = process.extract(model_id, all_ids, scorer=fuzz.WRatio, limit=5)
    return [match[0] for match in matches if match[1] >= 55] or [match[0] for match in matches[:3]]


async def check_ids(models: dict[str, str], catalog: list[dict[str, Any]]) -> bool | None:
    """True — все ID найдены, False — есть отсутствующие, None — каталог недоступен."""
    banner("ШАГ 1. Сверка ID моделей с каталогом OpenRouter")
    by_id = {entry["id"]: entry for entry in catalog if entry.get("id")}
    if not by_id:
        print("⚠️  Каталог моделей недоступен (нет интернета/прокси режет запрос) — сверку пропускаю.")
        print("   Это НЕ значит, что модели неверные: проверьте вручную на https://openrouter.ai/models")
        return None
    print(f"Каталог: {len(by_id)} моделей\n")

    all_ok = True
    for env_name, model_id in models.items():
        entry = by_id.get(model_id)
        if entry:
            params = entry.get("supported_parameters") or []
            structured = "structured_outputs" in params or "response_format" in params
            print(f"✅ {env_name} = {model_id}")
            print(f"   {describe_model(entry)}")
            print(f"   structured_outputs: {'да' if structured else 'НЕТ — работаем в текстовом JSON-режиме'}")
        else:
            all_ok = False
            print(f"❌ {env_name} = {model_id} — НЕ найден в каталоге!")
            print(f"   Похожие ID: {', '.join(suggest_similar(model_id, list(by_id)))}")
            print(f"   Замените в .env значение {env_name} и повторите.")
    return all_ok


async def probe_models(models: dict[str, str], catalog: list[dict[str, Any]]) -> bool:
    banner("ШАГ 2. Пробный запрос к каждой модели (крошечный JSON)")
    client = OpenRouterClient()
    if not client.available:
        print("❌ OPENROUTER_API_KEY пуст — впишите ключ в .env (см. SETUP.md, шаг про ключи).")
        return False

    by_id = {entry["id"]: entry for entry in catalog if entry.get("id")}
    all_ok = True
    try:
        for env_name, model_id in models.items():
            supported = (by_id.get(model_id) or {}).get("supported_parameters") or []
            json_mode = "response_format" in supported
            try:
                parsed, _raw = await client.complete_json(
                    model=model_id,
                    system_prompt=PROBE_SYSTEM,
                    user_prompt=PROBE_USER,
                    max_tokens=max(settings.llm_max_tokens_screener, 200),
                    temperature=0.0,
                    timeout=settings.llm_timeout_sec,
                    json_mode=json_mode,
                    label=f"check:{env_name}",
                )
                ok = bool(parsed.get("ok"))
                print(f"{'✅' if ok else '⚠️ '} {env_name} ({model_id}): {parsed}")
                all_ok = all_ok and ok
            except LLMError as exc:
                all_ok = False
                print(f"❌ {env_name} ({model_id}): {exc}")
        stats = client.usage_summary()
        print(
            f"\nТокены за пробу: in={stats['prompt_tokens']}, out={stats['completion_tokens']}, "
            f"запросов={stats['requests']}, ошибок={stats['failures']}"
        )
    finally:
        await client.aclose()
    return all_ok


async def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка моделей OpenRouter для BetSignals")
    parser.add_argument("--no-probe", action="store_true", help="только сверка ID, без вызовов моделей")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    models = configured_models()
    print("Модели в конфиге:")
    for env_name, model_id in models.items():
        print(f"  {env_name} = {model_id}")

    try:
        catalog = await fetch_catalog()
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        print(f"\n❌ Не удалось скачать каталог моделей: {exc}")
        print("   Проверьте интернет и OPENROUTER_BASE_URL. Продолжаю пробные запросы.")
        catalog = []

    ids_ok = await check_ids(models, catalog)
    probes_ok: bool | None = None
    if not args.no_probe:
        probes_ok = await probe_models(models, catalog)

    banner("ИТОГ")
    print("ID моделей найдены в каталоге: " + {True: "да", False: "НЕТ (см. подсказки выше)", None: "неизвестно (каталог недоступен)"}[ids_ok])
    print(
        "Пробные запросы прошли:       "
        + {True: "да", False: "нет", None: "не выполнялись (--no-probe)"}[probes_ok]
    )
    if ids_ok is False:
        print("\nПодсказка: актуальный список — https://openrouter.ai/models, там же видно цены.")
    # Недоступный каталог (нет интернета) не считаем провалом конфига.
    failed = ids_ok is False or probes_ok is False
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
