"""Коллектор (Модуль 1 → Модуль 2): сбор расписаний, склейка команд, снимки кэфов.

Порядок работы (запускается планировщиком в 06:00 и 14:00 по Москве):

  1. ensure_sports_exist() — гарантирует строки в `sports` (football, hockey, ...).
  2. collect_day_schedule() — забирает события на дату у ВСЕХ доступных источников
     (букмекеры дают расписание + тир турнира, Liquipedia/BallDontLie — киберспорт/NBA),
     склеивает команды через TeamMatcher и делает upsert в `matches`.
  3. refresh_todays_odds() — дёргает кэфы по матчам дня и пишет снимки в `odds`
     (append-only; повторные одинаковые цены не дублируются).

Любая ошибка источника изолирована: лог + `data_quality="weak"` у матча,
остальные источники продолжают работать (см. ТЗ «грациозная деградация»).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import get_sport_map, session_scope
from app.db.models import DataQuality, Match as MatchRow, MatchStatus, Sport
from app.sources.apibasketball import ApiBasketballProvider, BallDontLieProvider
from app.sources.apifootball import ApiFootballProvider
from app.sources.apitennis import ApiTennisProvider
from app.sources.base import Match, OddsProvider, SourceError, StatsProvider
from app.sources.betboom import BetBoomOddsProvider
from app.sources.liquipedia import LiquipediaProvider
from app.sources.mma_rss import MmaRssProvider
from app.sources.moneypuck import MoneyPuckProvider
from app.sources.nhl import NhlProvider
from app.sources.odds_aggregator import OddsAggregator
from app.sources.opendota import OpenDotaProvider
from app.sources.team_matching import TeamMatcher
from app.sources.theoddsapi import TheOddsApiProvider
from app.sources.understat import UnderstatProvider, league_code_for
from app.sources.winline import WinlineOddsProvider

# Матчи, к которым применяем обновление кэфов по расписанию
ODDS_REFRESH_HORIZON_HOURS = 30
# Насколько близко по времени должны совпадать события разных источников, чтобы склеиться
MATCH_MERGE_WINDOW_HOURS = 6
# Сколько матчей дня максимум обновляем за один проход (защита free-tier лимитов)
MAX_ODDS_REFRESH_PER_RUN = 80


@dataclass
class Providers:
    """Контейнер всех источников: собирается один раз на процесс (см. main.py)."""

    odds: list[OddsProvider] = field(default_factory=list)
    aggregator: OddsAggregator = field(default_factory=OddsAggregator)
    football: StatsProvider | None = None
    understat: UnderstatProvider | None = None
    nhl: NhlProvider | None = None
    moneypuck: MoneyPuckProvider | None = None
    basketball: StatsProvider | None = None
    balldontlie: BallDontLieProvider | None = None
    tennis: ApiTennisProvider | None = None
    fight_news: MmaRssProvider | None = None
    opendota: OpenDotaProvider | None = None
    liquipedia: LiquipediaProvider | None = None

    def stats_for(self, sport: str) -> list[StatsProvider]:
        """Статистические источники sport'а в порядке приоритета."""
        mapping: dict[str, list[StatsProvider | None]] = {
            "football": [self.football, self.understat, self.fight_news],
            "hockey": [self.nhl, self.moneypuck],
            "basketball": [self.basketball, self.balldontlie],
            "tennis": [self.tennis],
            "mma": [self.fight_news],
            "boxing": [self.fight_news],
            "dota2": [self.opendota, self.liquipedia],
            "cs2": [self.liquipedia],
        }
        return [source for source in mapping.get(sport, []) if source is not None]

    def available_odds(self) -> list[OddsProvider]:
        return [provider for provider in self.odds if provider.available]

    def describe(self) -> dict[str, Any]:
        """Сводка «что включено» — печатается в лог при старте и в /health."""
        return {
            "odds": {provider.provider_name: provider.available for provider in self.odds},
            "stats": {
                "api-football": bool(self.football and self.football.available),
                "understat": bool(self.understat and self.understat.available),
                "nhl": bool(self.nhl and self.nhl.available),
                "moneypuck": bool(self.moneypuck and self.moneypuck.available),
                "api-basketball": bool(self.basketball and self.basketball.available),
                "balldontlie": bool(self.balldontlie and self.balldontlie.available),
                "api-tennis": bool(self.tennis and self.tennis.available),
                "mma-rss": bool(self.fight_news and self.fight_news.available),
                "opendota": bool(self.opendota and self.opendota.available),
                "liquipedia": bool(self.liquipedia and self.liquipedia.available),
            },
        }


def build_providers() -> Providers:
    """Создаёт все провайдеры. Недоступные (нет ключа/URL) просто не будут вызваны."""
    winline = WinlineOddsProvider()
    betboom = BetBoomOddsProvider()
    theoddsapi = TheOddsApiProvider()
    odds: list[OddsProvider] = [winline, betboom]
    if settings.the_odds_api_enabled:
        odds.append(theoddsapi)
    else:
        logger.info(
            "collector: TheOddsApi выключен (THE_ODDS_API_ENABLED=false). "
            "Это осознанно: free-tier 500 запросов/мес — включайте только на время сверок."
        )

    providers = Providers(
        odds=odds,
        aggregator=OddsAggregator(providers=odds),
        football=ApiFootballProvider(),
        understat=UnderstatProvider(),
        nhl=NhlProvider(),
        moneypuck=MoneyPuckProvider(),
        basketball=ApiBasketballProvider(),
        balldontlie=BallDontLieProvider(),
        tennis=ApiTennisProvider(),
        fight_news=MmaRssProvider(),
        opendota=OpenDotaProvider(),
        liquipedia=LiquipediaProvider(),
    )
    logger.info("collector: конфигурация источников — {}", providers.describe())
    return providers


# Единый набор провайдеров на процесс (ТЗ: ОДИН долгоживущий процесс).
# Держит HTTP-соединения и TTL-кэши тёплыми — важно для лимитов бесплатных API.
_providers_singleton: Providers | None = None


def get_providers() -> Providers:
    """Возвращает общий контейнер провайдеров, создавая его при первом обращении."""
    global _providers_singleton
    if _providers_singleton is None:
        _providers_singleton = build_providers()
    return _providers_singleton


async def shutdown_providers() -> None:
    """Закрывает общий контейнер (graceful shutdown в main.py)."""
    global _providers_singleton
    if _providers_singleton is not None:
        await close_providers(_providers_singleton)
        _providers_singleton = None


async def close_providers(providers: Providers) -> None:
    """Закрывает HTTP-клиенты всех провайдеров (вызывается при graceful shutdown)."""
    closed = 0
    for provider in [
        *providers.odds,
        providers.football,
        providers.understat,
        providers.nhl,
        providers.moneypuck,
        providers.basketball,
        providers.balldontlie,
        providers.tennis,
        providers.fight_news,
        providers.opendota,
        providers.liquipedia,
    ]:
        if provider is None:
            continue
        try:
            await provider.aclose()
            closed += 1
        except Exception as exc:
            logger.debug("collector: ошибка закрытия {} ({})", getattr(provider, "provider_name", provider), exc)
    logger.info("collector: закрыто HTTP-клиентов источников: {}", closed)


# --------------------------------------------------------------------------- #
# Упсёрт матчей
# --------------------------------------------------------------------------- #
async def upsert_match(
    session: AsyncSession,
    match: Match,
    sport_map: dict[str, int],
    matchers: dict[int, TeamMatcher],
) -> MatchRow | None:
    """Создаёт/обновляет строку `matches` (склейка команд + external_refs по источникам).

    Возвращает None, если матч не удалось привязать к спорту/командам.
    """
    sport_id = sport_map.get(match.sport)
    if not sport_id:
        logger.warning("collector: неизвестный спорт '{}' у события {}", match.sport, match.ext_id[:80])
        return None

    matcher = matchers.get(sport_id) or TeamMatcher(session, sport_id)
    matchers[sport_id] = matcher

    home_id = await matcher.get_or_create_team_id(match.home_team, match.source)
    away_id = await matcher.get_or_create_team_id(match.away_team, match.source)
    if not home_id or not away_id or home_id == away_id:
        logger.warning(
            "collector: не удалось склеить команды '{}' / '{}' ({})",
            match.home_team, match.away_team, match.source,
        )
        return None

    ext_id = f"{match.source}:{match.ext_id}"[:120]
    row = (
        await session.execute(select(MatchRow).where(MatchRow.sport_id == sport_id, MatchRow.ext_id == ext_id))
    ).scalar_one_or_none()

    if row is None:
        # Пробуем найти тот же матч от другого источника (склейка расписаний).
        window_start = match.starts_at - timedelta(hours=MATCH_MERGE_WINDOW_HOURS)
        window_end = match.starts_at + timedelta(hours=MATCH_MERGE_WINDOW_HOURS)
        row = (
            await session.execute(
                select(MatchRow).where(
                    MatchRow.sport_id == sport_id,
                    MatchRow.home_team_id == home_id,
                    MatchRow.away_team_id == away_id,
                    MatchRow.starts_at >= window_start,
                    MatchRow.starts_at <= window_end,
                )
            )
        ).scalars().first()

    if row is None:
        row = MatchRow(
            sport_id=sport_id,
            ext_id=ext_id,
            league=match.league[:160],
            league_tier=match.league_tier,
            home_team_id=home_id,
            away_team_id=away_id,
            starts_at=match.starts_at,
            status=match.status or MatchStatus.SCHEDULED,
            source=match.source,
            external_refs={match.source: match.ext_id},
            data_quality=DataQuality.OK,
        )
        session.add(row)
        await session.flush()
        logger.debug("collector: новый матч {} vs {} ({}, {})", match.home_team, match.away_team, match.league, match.source)
        return row

    # Обновляем существующий
    refs = dict(row.external_refs or {})
    refs[match.source] = match.ext_id
    row.external_refs = refs
    if match.league and match.league != "unknown":
        row.league = match.league[:160]
    if match.league_tier and not row.league_tier:
        row.league_tier = match.league_tier
    if match.starts_at and not row.starts_at:
        row.starts_at = match.starts_at
    if match.is_live or match.status == MatchStatus.LIVE:
        row.status = MatchStatus.LIVE
    if match.home_score is not None and match.away_score is not None:
        row.result_home = match.home_score
        row.result_away = match.away_score
    await session.flush()
    return row


# --------------------------------------------------------------------------- #
# Сбор расписания
# --------------------------------------------------------------------------- #
async def collect_day_schedule(session: AsyncSession, day: date | None = None, providers: Providers | None = None) -> dict[str, int]:
    """Собирает матчи дня со всех источников. Возвращает счётчики по источникам."""
    providers = providers or get_providers()
    day = day or datetime.now(settings.tzinfo).date()
    sport_map = await get_sport_map(session)
    matchers: dict[int, TeamMatcher] = {}
    stats: dict[str, int] = {}

    # ---------------------------------------------------------- букмекеры
    for provider in providers.available_odds():
        created = 0
        for sport in ("football", "hockey", "basketball", "tennis", "mma", "boxing", "dota2", "cs2"):
            try:
                matches = await provider.get_upcoming(sport, day)
            except SourceError as exc:
                logger.warning("collector: {} не отдал расписание по {} ({})", provider.provider_name, sport, str(exc)[:200])
                continue
            except Exception as exc:
                logger.warning("collector: {} упал на {} ({})", provider.provider_name, sport, str(exc)[:200])
                continue
            for match in matches:
                if await upsert_match(session, match, sport_map, matchers):
                    created += 1
        stats[provider.provider_name] = created
        logger.info("collector: {} → {} событий на {}", provider.provider_name, created, day.isoformat())

    # -------------------------------------------------- Liquipedia (киберспорт)
    if providers.liquipedia and providers.liquipedia.available:
        try:
            created = 0
            for sport in ("dota2", "cs2"):
                for match in await providers.liquipedia.get_upcoming_matches(sport, days=2):
                    if not match.league_tier:
                        # Tier нужен скринеру (только S-Tier/Tier 1 для киберспорта).
                        match.league_tier = await providers.liquipedia.get_tournament_tier(match.league)
                    if await upsert_match(session, match, sport_map, matchers):
                        created += 1
            stats["liquipedia"] = created
        except Exception as exc:
            logger.warning("collector: liquipedia недоступна ({})", str(exc)[:200])

    # ------------------------------------------------- BallDontLie (NBA, фолбэк)
    if providers.balldontlie and providers.balldontlie.available:
        try:
            matches = await providers.balldontlie.get_upcoming(day)
            created = 0
            for match in matches:
                if await upsert_match(session, match, sport_map, matchers):
                    created += 1
            stats["balldontlie"] = created
        except Exception as exc:
            logger.warning("collector: balldontlie недоступен ({})", str(exc)[:200])

    # ------------------------------------------------------------ анализ лиг
    await ensure_football_leagues_enabled(session, matchers, sport_map)

    await session.commit()
    logger.info("collector: расписание на {} собрано — {}", day.isoformat(), stats)
    return stats


async def ensure_football_leagues_enabled(
    session: AsyncSession, matchers: dict[int, TeamMatcher], sport_map: dict[str, int]
) -> int:
    """Гарантирует, что у футбольных матчей из RPL/еврокубков команды склеены.

    Отдельная функция-хук: сюда удобно добавлять логику «включай Understat только
    для лиг, где он есть» (Understat не знает РПЛ-команды, script/check_sources
    покажет коды). Возвращает число матчей, для которых проверен матчинг.
    """
    football_id = sport_map.get("football")
    if not football_id:
        return 0
    rows = (
        await session.execute(
            select(MatchRow.id, MatchRow.league)
            .where(MatchRow.sport_id == football_id, MatchRow.home_team_id.is_not(None), MatchRow.away_team_id.is_not(None))
            .limit(200)
        )
    ).all()
    supported = sum(1 for _match_id, league in rows if league_code_for(league))
    logger.debug(
        "collector: футбольных матчей с командами — {}, из них с поддержкой Understat — {}",
        len(rows), supported,
    )
    return supported


# --------------------------------------------------------------------------- #
# Обновление кэфов
# --------------------------------------------------------------------------- #
async def refresh_todays_odds(session: AsyncSession, providers: Providers | None = None) -> int:
    """Обновляет снимки кэфов для матчей ближайших суток. Возвращает число обновлённых матчей."""
    providers = providers or get_providers()
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=ODDS_REFRESH_HORIZON_HOURS)
    rows = (
        (
            await session.execute(
                select(MatchRow)
                .where(
                    MatchRow.status.in_((MatchStatus.SCHEDULED, MatchStatus.LIVE)),
                    MatchRow.starts_at >= now - timedelta(hours=4),
                    MatchRow.starts_at <= horizon,
                )
                .order_by(MatchRow.starts_at.asc())
                .limit(MAX_ODDS_REFRESH_PER_RUN)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        logger.debug("collector: нет матчей в горизонте кэфов (±{}ч)", ODDS_REFRESH_HORIZON_HOURS)
        return 0

    updated = 0
    for row in rows:
        try:
            outcomes = await providers.aggregator.refresh_match(session, row)
        except Exception as exc:
            logger.warning("collector: кэфы матча {} не обновлены ({})", row.id, str(exc)[:200])
            row.data_quality = DataQuality.WEAK
            continue
        if outcomes:
            updated += 1
        else:
            logger.info(
                "collector: match_id={} — ни один источник не дал полного рынка кэфов "
                "(проверьте WINLINE_API_BASE/BETBOOM_API_BASE в .env)",
                row.id,
            )
    await session.commit()
    logger.info("collector: кэфы обновлены у {}/{} матчей", updated, len(rows))
    return updated


async def get_match_snapshot(session: AsyncSession, match_id: int) -> dict[str, Any] | None:
    """Краткая сводка по матчу для диагностики (scripts/check_sources.py, /health)."""
    row = (
        await session.execute(select(MatchRow).where(MatchRow.id == match_id))
    ).scalar_one_or_none()
    if row is None:
        return None
    sport = (await session.execute(select(Sport.code).where(Sport.id == row.sport_id))).scalar_one_or_none()
    return {
        "match_id": row.id,
        "sport": sport,
        "league": row.league,
        "status": row.status,
        "starts_at": row.starts_at.isoformat() if row.starts_at else None,
        "external_refs": row.external_refs,
        "data_quality": row.data_quality,
        "signals": None,
    }


async def collect_and_refresh(day: date | None = None) -> None:
    """Хелпер для планировщика: собрать расписание и сразу обновить кэфы в одной сессии."""
    providers = get_providers()
    try:
        async with session_scope() as session:
            await ensure_sports_exist()
            await collect_day_schedule(session, day, providers)
        async with session_scope() as session:
            await refresh_todays_odds(session, providers)
    finally:
        # В обычной работе (scheduler) провайдеры НЕ закрываются: контейнер общий.
        # Здесь закрываем только если функция вызвана как самостоятельный скрипт-хелпер.
        if providers is not get_providers():
            await close_providers(providers)


async def ensure_sports_exist() -> dict[str, int]:
    """Создаёт строки `sports` для всех кодов из SPORT_CODES (идемпотентно)."""
    from app.config import SPORT_CODES
    from app.db.database import ensure_sports

    await ensure_sports()
    async with session_scope() as session:
        sport_map = await get_sport_map(session)
    missing = [code for code in SPORT_CODES if code not in sport_map]
    if missing:
        logger.warning("collector: в таблице sports нет кодов {} (проверьте ensure_sports)", missing)
    return sport_map
