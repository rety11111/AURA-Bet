"""Лайв-воркер киберспорта (Модуль 7) — OpenDota + Winline (без PandaScore).

Поллинг каждые 45 секунд, только tier-1 матчи в статусе live.

DOTA 2 (данные: OpenDota /api/live, кэфы: Winline live)
  Окно 1 — сразу после завершения драфта: полный список пиков → LLM-анализ
           (синергия пиков, контрпики, стенд-ины из Liquipedia) против лайв-кэфа.
  Окно 2 — игровые минуты 8–12: разница networth/XP/CS (если OpenDota их отдаёт),
           ранние смерти, кто давит → сравнение с лайв-кэфом.
  После 20-й минуты сигналы ЗАПРЕЩЕНЫ — линия уже всё отражает.

CS2 (данные: Liquipedia — карты/составы, Elo по парам команда+карта — наша таблица;
     кэфы и счёт серии/раундов: Winline live)
  Окно 1 — после определения карты: Elo обеих команд на этой карте + H2H → сравнение с кэфом.
  Окно 2 — раунды 6–10: счёт по раундам + АНАЛИЗ ДВИЖЕНИЯ КЭФА (live/odds_movement.py).

Общий принцип: value берём там, где наши игровые данные опережают реакцию линии.
Если линия уже «догнала» реальность — сигнала нет. Плюс жёсткое правило:
не сигналить, если кэф фаворита < LIVE_MIN_FAVOURITE_ODDS (1.35) — там нет value.

Честно про данные: OpenDota гарантированно даёт матч, время игры и героев игроков;
networth/XP/CS присутствуют не всегда (см. sources/opendota.py). Когда их нет,
воркер не выдумывает числа, а строит контекст по тому, что есть, и помечает это
в live_info["data_notes"].
"""

from __future__ import annotations


from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import get_sport_map
from app.db.models import Match, MatchStatus, Signal, SignalStatus
from app.live import odds_movement
from app.pipeline.analyzer import analyze_live_candidate
from app.pipeline.collector import Providers, get_providers, upsert_match
from app.sources.base import EsportsLiveMatch, Match as SourceMatch
from app.sources.team_matching import TeamMatcher
from app.stats_models.elo import get_elo

# Кэш «что уже обрабатывали» — чтобы не гонять LLM каждые 45 секунд по одному и тому же окну.
_processed_windows: dict[str, datetime] = {}


def _window_key(match_ext_id: str, window: str) -> str:
    return f"{match_ext_id}:{window}"


def _already_done(match_ext_id: str, window: str, cooldown_minutes: int = 30) -> bool:
    last = _processed_windows.get(_window_key(match_ext_id, window))
    if last is None:
        return False
    return (datetime.now(timezone.utc) - last) < timedelta(minutes=cooldown_minutes)


def _mark_done(match_ext_id: str, window: str) -> None:
    _processed_windows[_window_key(match_ext_id, window)] = datetime.now(timezone.utc)


async def live_tick(session: AsyncSession, providers: Providers | None = None) -> dict[str, Any]:
    """Один цикл воркера (вызывается планировщиком каждые 45 секунд)."""
    providers = providers or get_providers()
    stats: dict[str, Any] = {"dota2": 0, "cs2": 0, "signals": 0, "skipped": 0}

    if not settings.live_enabled:
        return stats

    if "dota2" in settings.live_esports_sports:
        try:
            stats["dota2"] = await _process_dota(session, providers, stats)
        except Exception as exc:  # noqa: BLE001 — воркер не должен падать
            logger.exception("live_worker: ошибка обработки Dota 2: {}", exc)
    if "cs2" in settings.live_esports_sports:
        try:
            stats["cs2"] = await _process_cs2(session, providers, stats)
        except Exception as exc:  # noqa: BLE001
            logger.exception("live_worker: ошибка обработки CS2: {}", exc)
    return stats


# --------------------------------------------------------------------------- #
# Dota 2
# --------------------------------------------------------------------------- #
async def _process_dota(session: AsyncSession, providers: Providers, stats: dict[str, Any]) -> int:
    live_matches = await providers.opendota.get_live_matches("dota2")
    if not live_matches:
        return 0
    tier1 = [match for match in live_matches if await _is_tier1(providers, "dota2", match.league)]
    logger.debug("live_worker: Dota 2 — {} лайв-матчей, tier-1: {}", len(live_matches), len(tier1))
    processed = 0
    for live in tier1:
        processed += 1
        await _handle_dota_match(session, providers, live, stats)
        await session.commit()
    return processed


async def _handle_dota_match(
    session: AsyncSession, providers: Providers, live: EsportsLiveMatch, stats: dict[str, Any]
) -> None:
    minute = live.game_minute or 0
    if minute > settings.live_dota_max_minute:
        return  # после 20-й минуты сигналы запрещены

    match_row = await _ensure_match(session, providers, live, sport="dota2")
    if match_row is None:
        return

    window: str | None = None
    if (
        settings.live_dota_window1_enabled
        and live.is_draft_complete
        and minute <= 3
        and not _already_done(live.ext_id, "w1")
    ):
        window = "w1"
    elif (
        settings.live_dota_window2_from_min <= minute <= settings.live_dota_window2_to_min
        and not _already_done(live.ext_id, "w2")
    ):
        window = "w2"

    if window is None:
        stats["skipped"] += 1
        return

    # Лайв-кэфы: обновляем снимки и берём агрегат.
    outcomes = await providers.aggregator.refresh_match(session, match_row, live=True)
    if not outcomes:
        logger.debug("live_worker: нет лайв-кэфов по match_id={} (dota2)", match_row.id)
        return

    favourite = _favourite_odds(outcomes)
    if favourite is not None and favourite < settings.live_min_favourite_odds:
        logger.debug("live_worker: match_id={} — кэф фаворита {:.2f} < {} — сигналы запрещены", match_row.id, favourite, settings.live_min_favourite_odds)
        stats["skipped"] += 1
        return

    movement = await _movement_for_match(session, match_row.id)
    leading = _leading_selection(live)
    context = {
        "stage": live.stage or f"минута {minute}",
        "minute": minute,
        "draft": {
            "radiant": live.draft_a,
            "dire": live.draft_b,
            "is_complete": live.is_draft_complete,
        },
        "kills_or_score": live.extra.get("score"),
        "net_worth": {
            "radiant": live.net_worth_a,
            "dire": live.net_worth_b,
            "available": bool(live.extra.get("networth_available")),
        },
        "xp": {"radiant": live.xp_a, "dire": live.xp_b},
        "players": {
            "radiant": [player.model_dump() for player in live.players_a],
            "dire": [player.model_dump() for player in live.players_b],
        },
        "teams": {"a": live.team_a, "b": live.team_b},
        "odds_movement": movement,
        "window": window,
        "data_notes": (
            "networth/XP/CS доступны" if live.extra.get("networth_available")
            else "OpenDota не отдаёт networth/XP для этого матча — оценка по драфту, времени и кэфам"
        ),
    }
    # Оценка «кто давит» для сравнения с линией (правило (а) из ТЗ).
    verdict, reason = odds_movement.assess_movement(
        next((m for m in [MovementProxy(item) for item in movement] if m.selection == leading), None),
        signal_selection=leading or "",
        leading_team_selection=leading,
        score_margin_abs=live.extra.get("score_margin"),
    )
    if verdict == odds_movement.MARKET_MOVES:
        await odds_movement.flag_market_moves(session, match_row, reason)
        stats["skipped"] += 1
        return

    logger.info(
        "live_worker: Dota 2 match_id={} окно {} (минута {}, лидер {}) — запускаем анализ",
        match_row.id, window, minute, leading or "н/д",
    )
    report = await analyze_live_candidate(session, match_row, context, providers=providers)
    stats["signals"] += report.signals
    _mark_done(live.ext_id, window)


# --------------------------------------------------------------------------- #
# CS2
# --------------------------------------------------------------------------- #
async def _process_cs2(session: AsyncSession, providers: Providers, stats: dict[str, Any]) -> int:
    """CS2: лайв-события и кэфы берём из Winline; карта — из Liquipedia/данных события."""
    events: list[SourceMatch] = []
    for provider in providers.odds:
        if not getattr(provider, "available", False) or not getattr(provider, "live_available", False):
            continue
        try:
            events.extend(await provider.get_live_events("cs2"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("live_worker: {} не отдал лайв CS2: {}", provider.provider_name, exc)
    if not events:
        return 0

    processed = 0
    for event in events:
        if not await _is_tier1(providers, "cs2", event.league):
            continue
        processed += 1
        await _handle_cs2_event(session, providers, event, stats)
        await session.commit()
    logger.debug("live_worker: CS2 — обработано {} событий", processed)
    return processed


async def _handle_cs2_event(
    session: AsyncSession, providers: Providers, event: SourceMatch, stats: dict[str, Any]
) -> None:
    raw = event.extra.get("raw") or {}
    rounds_a = _int_from(raw, ("rounds1", "homeRoundScore", "score1", "homeScore", "mapScore1"))
    rounds_b = _int_from(raw, ("rounds2", "awayRoundScore", "score2", "awayScore", "mapScore2"))
    map_name = _str_from(raw, ("map", "mapName", "currentMap", "map_name"))
    stage = event.live_stage or _str_from(raw, ("stage", "period", "status")) or "лайв"

    match_row = await _ensure_match(
        session,
        providers,
        EsportsLiveMatch(
            ext_id=event.ext_id,
            sport="cs2",
            league=event.league,
            team_a=event.home_team,
            team_b=event.away_team,
            source=event.source,
            map_name=map_name,
            rounds_a=rounds_a,
            rounds_b=rounds_b,
            stage=stage,
            league_tier=event.league_tier,
        ),
        sport="cs2",
    )
    if match_row is None:
        return

    outcomes = await providers.aggregator.refresh_match(session, match_row, live=True)
    if not outcomes:
        return
    favourite = _favourite_odds(outcomes)
    if favourite is not None and favourite < settings.live_min_favourite_odds:
        stats["skipped"] += 1
        return

    total_rounds = (rounds_a or 0) + (rounds_b or 0)
    window: str | None = None
    if map_name and total_rounds == 0 and not _already_done(event.ext_id, "w1"):
        window = "w1"   # карта определена (вето прошло), раунды ещё не начались
    elif (
        settings.live_cs2_window2_from_round <= total_rounds <= settings.live_cs2_window2_to_round
        and not _already_done(event.ext_id, "w2")
    ):
        window = "w2"
    if window is None:
        stats["skipped"] += 1
        return

    elo_info: dict[str, Any] = {}
    sport_map = await get_sport_map(session)
    sport_id = sport_map.get("cs2")
    if sport_id and match_row.home_team_id and match_row.away_team_id:
        elo_home, matches_home = await get_elo(session, sport_id, match_row.home_team_id, map_name)
        elo_away, matches_away = await get_elo(session, sport_id, match_row.away_team_id, map_name)
        elo_info = {
            "map": map_name,
            "elo_home": elo_home,
            "elo_away": elo_away,
            "matches_home": matches_home,
            "matches_away": matches_away,
            "h2h_map_win_rate": await _map_h2h(session, providers, match_row, map_name),
        }

    movement = await _movement_for_match(session, match_row.id)
    score_margin = None
    if rounds_a is not None and rounds_b is not None:
        score_margin = rounds_a - rounds_b
    leading = _selection_for_score(match_row, score_margin)
    verdict, reason = odds_movement.assess_movement(
        MovementProxy(movement[0]) if movement else None,
        signal_selection=leading or "",
        leading_team_selection=leading,
        score_margin_abs=abs(score_margin) if score_margin is not None else None,
    )
    if verdict == odds_movement.MARKET_MOVES:
        await odds_movement.flag_market_moves(session, match_row, reason)
        stats["skipped"] += 1
        return

    context = {
        "stage": f"{stage} | карта {map_name or 'н/д'}, раунды {rounds_a or 0}:{rounds_b or 0}",
        "map": map_name,
        "rounds": {"a": rounds_a, "b": rounds_b},
        "series_score": event.home_score,
        "elo": elo_info,
        "odds_movement": movement,
        "window": window,
        "data_notes": (
            "раунды и карта из лайв-линии Winline; при отсутствии карты считаем Elo по общему рейтингу"
        ),
    }
    logger.info("live_worker: CS2 match_id={} окно {} (карта {}, раунды {}:{})", match_row.id, window, map_name, rounds_a, rounds_b)
    report = await analyze_live_candidate(session, match_row, context, providers=providers)
    stats["signals"] += report.signals
    _mark_done(event.ext_id, window)


# --------------------------------------------------------------------------- #
# Вспомогательные
# --------------------------------------------------------------------------- #
async def _ensure_match(
    session: AsyncSession, providers: Providers, live: EsportsLiveMatch, sport: str
) -> Match | None:
    """Находит/создаёт матч в БД по данным лайва."""
    sport_map = await get_sport_map(session)
    if sport not in sport_map:
        return None
    ext_id = f"{live.source}:{live.ext_id}"
    existing = (
        await session.execute(
            select(Match).where(Match.sport_id == sport_map[sport], Match.ext_id == ext_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.status = MatchStatus.LIVE
        existing.live_stage = live.stage
        await session.flush()
        return existing

    matchers: dict[int, TeamMatcher] = {}
    source_match = SourceMatch(
        ext_id=live.ext_id,
        sport=sport,
        league=live.league,
        home_team=live.team_a,
        away_team=live.team_b,
        starts_at=live.match_time or datetime.now(timezone.utc),
        source=live.source,
        league_tier=live.league_tier,
        status="live",
        live_stage=live.stage,
        is_live=True,
        home_score=live.series_score[0] if live.series_score else None,
        away_score=live.series_score[1] if live.series_score else None,
    )
    return await upsert_match(session, source_match, sport_map, matchers)


async def _is_tier1(providers: Providers, sport: str, league: str) -> bool:
    """Только tier-1 (S-Tier/Tier 1) — по Liquipedia, с кэшем внутри провайдера."""
    if not league or league == "unknown":
        return False
    low = league.lower()
    if any(keyword in low for keyword in ("the international", "major", "esl one", "esl pro league", "blast premier", "iem", "riyadh")):
        return True
    tier = await providers.liquipedia.get_tournament_tier(league)
    if tier is None:
        return False
    return any(allowed in tier.lower() for allowed in settings.allowed_esports_tiers)


def _favourite_odds(outcomes: list[Any]) -> float | None:
    prices = [outcome.best_price for outcome in outcomes if outcome.market == "1x2" and outcome.selection in ("home", "away")]
    if not prices:
        return None
    return min(prices)


def _leading_selection(live: EsportsLiveMatch) -> str | None:
    """Кто ведёт по данным (networth → XP → ничего)."""
    if live.net_worth_a is not None and live.net_worth_b is not None:
        if live.net_worth_a > live.net_worth_b * 1.05:
            return "home"
        if live.net_worth_b > live.net_worth_a * 1.05:
            return "away"
    return None


def _selection_for_score(match: Match, margin: int | None) -> str | None:
    if margin is None or abs(margin) < 3:
        return None
    return "home" if margin > 0 else "away"


def _int_from(raw: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
    return None


def _str_from(raw: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def _map_h2h(
    session: AsyncSession, providers: Providers, match_row: Match, map_name: str | None
) -> dict[str, Any] | None:
    """Win-rate обеих команд на карте (Liquipedia) — контекст для окна 1 в CS2."""
    if not map_name:
        return None
    home_name = await _team_name(session, match_row.home_team_id)
    away_name = await _team_name(session, match_row.away_team_id)
    if not home_name or not away_name:
        return None
    home_stats = await providers.liquipedia.get_map_stats(home_name, map_name)
    away_stats = await providers.liquipedia.get_map_stats(away_name, map_name)
    if home_stats is None and away_stats is None:
        return None
    return {
        "map": map_name,
        "home": home_stats.model_dump() if home_stats else None,
        "away": away_stats.model_dump() if away_stats else None,
    }


async def _team_name(session: AsyncSession, team_id: int | None) -> str | None:
    if not team_id:
        return None
    from app.db.models import Team

    return (await session.execute(select(Team.canonical_name).where(Team.id == team_id))).scalar_one_or_none()


async def _movement_for_match(session: AsyncSession, match_id: int) -> list[dict]:
    try:
        return await odds_movement.movement_report(session, match_id, window_minutes=settings.odds_movement_window_min)
    except Exception as exc:  # noqa: BLE001
        logger.debug("live_worker: не удалось посчитать движение кэфов для match_id={}: {}", match_id, exc)
        return []


class MovementProxy:
    """Адаптер словаря движения к объекту Movement (для assess_movement)."""

    def __init__(self, payload: dict[str, Any] | None) -> None:
        payload = payload or {}
        self.market = str(payload.get("market") or "")
        self.selection = str(payload.get("selection") or "")
        self.line = payload.get("line")
        self.first_price = float(payload.get("first_price") or 0.0)
        self.last_price = float(payload.get("last_price") or 0.0)
        self.first_at = None
        self.last_at = None
        self.window_minutes = float(payload.get("window_minutes") or 0.0)
        self.source = str(payload.get("source") or "")

    @property
    def delta(self) -> float:
        return self.last_price - self.first_price

    @property
    def delta_pct(self) -> float:
        return (self.delta / self.first_price) if self.first_price else 0.0

    @property
    def dropped(self) -> bool:
        return self.delta <= -settings.odds_movement_min_delta

    @property
    def rose(self) -> bool:
        return self.delta >= settings.odds_movement_min_delta


async def cancel_stale_live_signals(session: AsyncSession, hours: int = 6) -> int:
    """Закрывает лайв-сигналы, которые остались в статусе candidate (например, воркер упал)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (
        await session.execute(
            select(Signal).where(
                Signal.is_live.is_(True),
                Signal.status == SignalStatus.CANDIDATE,
                Signal.created_at <= cutoff,
            )
        )
    ).scalars().all()
    for signal in rows:
        signal.status = SignalStatus.REJECTED
        signal.judge_reason = "лайв-сигнал не был подтверждён (окно истекло)"
    if rows:
        await session.flush()
    return len(rows)
