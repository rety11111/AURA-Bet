"""Ур.0 — детерминированный скринер (без LLM, бесплатно).

Пропускает матч дальше только если выполнены ВСЕ условия ТЗ:
  1. лига/турнир в whitelist (конфиг);
  2. есть хотя бы один кэф в [ODDS_MIN, ODDS_MAX];
  3. статистика доступна (минимум MIN_RECENT_MATCHES последних матчей), иначе
     матч помечается data_quality="weak" и не блокируется жёстко — weak просто не даёт
     сигнал на выходе (см. value_engine.select_signals);
  4. матч не анализировался в этом проходе (проверяется вызывающим кодом — таблица analyses);
  5. линия не suspicious.

Модуль не ходит в сеть и не знает про БД — ему на вход подаётся ScreenInput.
Так его легко тестировать (tests/test_screener.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.config import settings
from app.db.models import DataQuality
from app.sources.odds_aggregator import AggregatedOutcome


@dataclass
class ScreenInput:
    sport: str
    league: str
    league_tier: str | None = None
    outcomes: list[AggregatedOutcome] = field(default_factory=list)
    home_matches_played: int = 0
    away_matches_played: int = 0
    has_stats: bool = False
    already_analyzed: bool = False
    starts_at: datetime | None = None
    is_esports: bool = False


@dataclass
class ScreenResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    data_quality: str = DataQuality.OK
    odds_in_range: int = 0
    checked: list[str] = field(default_factory=list)

    def reject(self, reason: str) -> ScreenResult:
        self.reasons.append(reason)
        self.passed = False
        return self


def league_allowed(sport: str, league: str, league_tier: str | None = None, is_esports: bool = False) -> bool:
    """Проверка whitelist. Для киберспорта — дополнительно tier-1 (S-Tier/Tier 1)."""
    if not settings.whitelist_enabled:
        return True
    keywords = settings.whitelist_for(sport)
    if not keywords:
        return False
    low = (league or "").lower()
    matched = any(keyword in low for keyword in keywords)
    if not matched:
        return False
    if is_esports and settings.esports_tier1_only:
        tier = (league_tier or "").lower()
        allowed = settings.allowed_esports_tiers
        # Если tier известен — проверяем; если неизвестен, допускаем (whitelist уже отсёк лигу).
        if tier and not any(allowed_tier in tier for allowed_tier in allowed):
            return False
        if not tier and "major" not in low and "the international" not in low:
            return False
    return True


def odds_in_range_count(outcomes: list[AggregatedOutcome]) -> int:
    return sum(1 for outcome in outcomes if settings.odds_min <= outcome.best_price <= settings.odds_max)


def stats_available(screen_input: ScreenInput) -> bool:
    if screen_input.has_stats:
        return True
    return (
        min(screen_input.home_matches_played, screen_input.away_matches_played) >= settings.min_recent_matches
    )


def screen(screen_input: ScreenInput) -> ScreenResult:
    """Основная функция уровня 0."""
    result = ScreenResult(passed=True)

    if screen_input.already_analyzed:
        return result.reject("матч уже анализировался в этом проходе")

    if not league_allowed(
        screen_input.sport, screen_input.league, screen_input.league_tier, screen_input.is_esports
    ):
        return result.reject(f"лига вне whitelist: {screen_input.league} ({screen_input.sport})")

    in_range = odds_in_range_count(screen_input.outcomes)
    result.odds_in_range = in_range
    if in_range == 0:
        return result.reject(
            f"нет кэфов в диапазоне [{settings.odds_min}, {settings.odds_max}]"
        )

    suspicious = [outcome for outcome in screen_input.outcomes if outcome.suspicious]
    if suspicious:
        result.checked.append(f"suspicious-линий: {len(suspicious)}")
        if len(suspicious) == len(screen_input.outcomes):
            return result.reject("все линии помечены suspicious (расхождение источников > 10%)")

    if not stats_available(screen_input):
        result.data_quality = DataQuality.WEAK
        result.checked.append("статистики меньше минимума → data_quality=weak")
    if not any(outcome.sources >= 2 for outcome in screen_input.outcomes):
        result.checked.append("подтверждение цены только из одного источника")

    result.reasons.append(
        f"прошёл: кэфов в диапазоне {in_range}, источников {max((o.sources for o in screen_input.outcomes), default=0)}"
    )
    return result


def summary(screen_input: ScreenInput) -> dict[str, Any]:
    """Краткая сводка для промпта скринера (Ур.1)."""
    best_prices = [
        {
            "market": outcome.market,
            "selection": outcome.selection,
            "line": outcome.line,
            "price": outcome.best_price,
            "implied": round(outcome.implied, 4),
            "sources": outcome.sources,
        }
        for outcome in sorted(screen_input.outcomes, key=lambda o: -o.best_price)[:14]
    ]
    return {
        "sport": screen_input.sport,
        "league": screen_input.league,
        "league_tier": screen_input.league_tier,
        "stats_matches": {"home": screen_input.home_matches_played, "away": screen_input.away_matches_played},
        "has_stats": screen_input.has_stats,
        "markets": best_prices,
    }
