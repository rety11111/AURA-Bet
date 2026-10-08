"""BetBoomOddsProvider — fallback №1 по коэффициентам (прематч + лайв).

Парсинг полностью переиспользует общий JSON-парсер букмекера из sources/winline.py
(класс JsonBookmakerOddsProvider): та же логика, другие URL из окружения.

⚠️ URL BetBoom берутся ТОЛЬКО из BETBOOM_API_BASE / BETBOOM_LIVE_API_BASE
(как получить — SETUP.md, раздел «Как достать реальные URL Winline и BetBoom»).
Если переменные пусты, провайдер недоступен, и агрегатор работает без него.
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.sources.winline import JsonBookmakerOddsProvider


class BetBoomOddsProvider(JsonBookmakerOddsProvider):
    provider_name = "betboom"

    def __init__(self, base_url: str | None = None, live_base_url: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.base_url = (base_url if base_url is not None else settings.betboom_api_base).rstrip("/")
        self.live_base_url = (
            live_base_url if live_base_url is not None else settings.betboom_live_api_base
        ).rstrip("/")

    @property
    def _extra_params_raw(self) -> str:
        return settings.betboom_prematch_params

    @property
    def _live_params_raw(self) -> str:
        return settings.betboom_live_params
