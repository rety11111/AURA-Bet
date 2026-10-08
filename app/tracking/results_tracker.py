"""Трекер результатов (Модуль 9): считает исходы разосланных сигналов и ROI.

Работает ежечасно (см. app/scheduler.py):

  1. Находит матчи со статусом live/scheduled, которые начались 2+ часа назад.
  2. Тянет финальный счёт из бесплатных источников (по спорту):
       football  → API-Football (/fixtures?id=...), фолбэк — уже сохранённый счёт букмекера;
       hockey    → NHL /score/<дата> (по аббревиатурам команд);
       basketball→ API-Basketball /games?date=...
       dota2     → OpenDota /matches/<id> (radiant_win);
       tennis/mma/boxing → бесплатного источника результатов нет.
  3. Рассчитывает исход по каждому сигналу: won / lost / void
     (void — «пуш»: тотал/фора ровно попали в линию, или матч отменён/не доигран).
  4. Пишет статус в signals и пополняет статистику (её читают /stats, дневная сводка и
     модуль обучения — learning/calibration.py).

ВАЖНО: трекер НИКОГДА не отключается (по ТЗ он кормит обучающий контур). Если счёт
найти не удалось — сигнал остаётся открытым, а в лог пишется warning с match_id.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import (
    DataQuality,
    Market,
    Match as MatchRow,
    MatchStatus,
    Selection,
    Signal,
    SignalStatus,
    Sport,
    Team,
)
from app.sources.base import H2HRecord, OddsProvider, SourceError

# Насколько позже старта считаем матч завершённым (футбол ~2ч, хоккей/баскетбол ~3ч, киберспорт ~4ч)
SETTLE_DELAY_HOURS = {"football": 2, "hockey": 3, "basketball": 3, "tennis": 4, "mma": 5, "boxing": 5, "dota2": 5, "cs2": 5}
DEFAULT_SETTLE_DELAY_HOURS = 4
# Если счёт не найден за это время — пишем warning (но матч оставляем открытым)
RESULT_SEARCH_WINDOW_DAYS = 7


def _sport_delay(sport_code: str) -> int:
    return SETTLE_DELAY_HOURS.get(sport_code, DEFAULT_SETTLE_DELAY_HOURS)


async def fetch_result(session: AsyncSession, match: MatchRow, providers: Any, sport_code: str) -> tuple[int, int] | None:
    """Пытается получить финальный счёт матча. None — не удалось (не выдумываем!)."""
    if match.result_home is not None and match.result_away is not None:
        return int(match.result_home), int(match.result_away)

    try:
        if sport_code == "football" and providers.football is not None:
            fixture_id = (match.external_refs or {}).get("api-football") or (match.external_refs or {}).get("apifootball")
            if fixture_id and providers.football.available:
                payload = await providers.football.get_json("/fixtures", params={"id": str(fixture_id)})
                rows = payload.get("response", []) if isinstance(payload, dict) else []
                for row in rows:
                    goals = (row.get("goals") or {})
                    home, away = goals.get("home"), goals.get("away")
                    if home is not None and away is not None:
                        return int(home), int(away)

        elif sport_code == "hockey" and providers.nhl is not None:
            home_name, away_name = await _team_names(session, match)
            games = await providers.nhl.get_todays_games(match.starts_at.date())
            for game in games:
                state = str(game.get("gameState") or "").upper()
                if not state.startswith("FINAL"):
                    continue
                home = ((game.get("homeTeam") or {}).get("abbrev") or {})
                away = ((game.get("awayTeam") or {}).get("abbrev") or {})
                home = home.get("default") if isinstance(home, dict) else home
                away = away.get("default") if isinstance(away, dict) else away
                if await _abbrev_matches(providers.nhl, home_name, home) and await _abbrev_matches(providers.nhl, away_name, away):
                    home_score = game.get("homeTeam", {}).get("score")
                    away_score = game.get("awayTeam", {}).get("score")
                    if home_score is not None and away_score is not None:
                        return int(home_score), int(away_score)

        elif sport_code == "basketball" and providers.basketball is not None and providers.basketball.available:
            home_name, away_name = await _team_names(session, match)
            games = await providers.basketball.get_games(match.starts_at.date())
            for game in games:
                teams = game.get("teams") or {}
                home = str((teams.get("home") or {}).get("name") or "")
                away = str((teams.get("away") or {}).get("name") or "")
                if not (_same_team(home, home_name) and _same_team(away, away_name)):
                    continue
                status_short = str((game.get("status") or {}).get("short") or "").upper()
                if status_short not in ("FT", "AOT", "AP", "AET"):
                    continue
                scores = game.get("scores") or {}
                home_points = (scores.get("home") or {}).get("total")
                away_points = (scores.get("away") or {}).get("total")
                if home_points is not None and away_points is not None:
                    return int(home_points), int(away_points)

        elif sport_code == "dota2" and providers.opendota is not None:
            match_id = (match.external_refs or {}).get("opendota")
            if match_id:
                payload = await providers.opendota.get_json(f"/matches/{match_id}")
                radiant_win = payload.get("radiant_win") if isinstance(payload, dict) else None
                if radiant_win is not None:
                    home_name, _away_name = await _team_names(session, match)
                    radiant_name = str((payload.get("radiant_team") or {}).get("name") or "")
                    if radiant_name and _same_team(radiant_name, home_name):
                        return (1, 0) if radiant_win else (0, 1)
                    return (0, 1) if radiant_win else (1, 0)

        elif sport_code in ("tennis", "mma", "boxing"):
            logger.debug(
                "tracker: match_id={} ({}) — бесплатного источника результатов нет, "
                "сигнал останется открытым (вручную закройте его через БД/аналитику)",
                match.id, sport_code,
            )
            return None
    except SourceError as exc:
        logger.warning("tracker: источник результата для match_id={} вернул ошибку ({})", match.id, str(exc)[:200])
    except Exception as exc:
        logger.warning("tracker: неожиданная ошибка получения результата match_id={} ({})", match.id, str(exc)[:200])
    return None


async def _team_names(session: AsyncSession, match: MatchRow) -> tuple[str, str]:
    rows = (
        await session.execute(select(Team.id, Team.canonical_name).where(Team.id.in_([match.home_team_id, match.away_team_id])))
    ).all()
    names = {team_id: name for team_id, name in rows}
    return names.get(match.home_team_id, ""), names.get(match.away_team_id, "")


def _same_team(a: str, b: str) -> bool:
    from app.sources.team_matching import similarity

    return similarity(a, b) >= 80


async def _abbrev_matches(nhl: Any, team_name: str, abbrev: Any) -> bool:
    if not abbrev:
        return False
    resolved = await nhl.resolve_team(team_name)
    return bool(resolved and str(resolved).upper() == str(abbrev).upper())


# --------------------------------------------------------------------------- #
# Расчёт исхода сигнала
# --------------------------------------------------------------------------- #
def settle_signal(signal: Signal, home_score: int, away_score: int) -> str:
    """won / lost / void для конкретного сигнала (чистая функция — покрыта тестами)."""
    line = signal.line
    market = signal.market
    selection = signal.selection

    if market == Market.ONE_X_TWO:
        if selection == Selection.HOME:
            return SignalStatus.WON if home_score > away_score else SignalStatus.LOST
        if selection == Selection.AWAY:
            return SignalStatus.WON if away_score > home_score else SignalStatus.LOST
        if selection == Selection.DRAW:
            return SignalStatus.WON if home_score == away_score else SignalStatus.LOST
        return SignalStatus.VOID

    if market == Market.DOUBLE_CHANCE:
        if selection == Selection.DC_1X:
            return SignalStatus.WON if home_score >= away_score else SignalStatus.LOST
        if selection == Selection.DC_X2:
            return SignalStatus.WON if away_score >= home_score else SignalStatus.LOST
        if selection == Selection.DC_12:
            return SignalStatus.WON if home_score != away_score else SignalStatus.LOST
        return SignalStatus.VOID

    if market == Market.TOTALS:
        if line is None:
            return SignalStatus.VOID
        total = home_score + away_score
        if abs(total - line) < 1e-9:
            return SignalStatus.VOID  # ровно в линию (целый тотал) — возврат
        if selection == Selection.OVER:
            return SignalStatus.WON if total > line else SignalStatus.LOST
        if selection == Selection.UNDER:
            return SignalStatus.WON if total < line else SignalStatus.LOST
        return SignalStatus.VOID

    if market == Market.HANDICAP:
        if line is None:
            return SignalStatus.VOID
        margin = home_score - away_score
        if selection == Selection.HOME_HANDICAP:
            adjusted = margin + line
            if abs(adjusted) < 1e-9:
                return SignalStatus.VOID
            return SignalStatus.WON if adjusted > 0 else SignalStatus.LOST
        if selection == Selection.AWAY_HANDICAP:
            adjusted = -margin + line
            if abs(adjusted) < 1e-9:
                return SignalStatus.VOID
            return SignalStatus.WON if adjusted > 0 else SignalStatus.LOST
        return SignalStatus.VOID

    return SignalStatus.VOID


# --------------------------------------------------------------------------- #
# Основной проход
# --------------------------------------------------------------------------- #
async def settle_finished_matches(session: AsyncSession, providers: Any) -> dict[str, int]:
    """Закрывает сигналы по всем матчам, которые уже должны быть завершены."""
    now = datetime.now(timezone.utc)
    candidates = (
        (
            await session.execute(
                select(MatchRow)
                .where(
                    MatchRow.status.in_((MatchStatus.SCHEDULED, MatchStatus.LIVE)),
                    MatchRow.starts_at >= now - timedelta(days=RESULT_SEARCH_WINDOW_DAYS),
                    # RESULTS_SETTLE_AFTER_HOURS — сколько ждать после старта, прежде чем
                    # вообще смотреть на матч (дальше ещё проверяется длительность по виду спорта).
                    MatchRow.starts_at <= now - timedelta(hours=settings.results_settle_after_hours),
                )
                .order_by(MatchRow.starts_at.asc())
            )
        )
        .scalars()
        .all()
    )
    if not candidates:
        logger.debug("tracker: нет матчей, готовых к вычислению результата")
        return {"checked": 0, "settled": 0, "unresolved": 0}

    sport_codes = dict((await session.execute(select(Sport.id, Sport.code))).all())
    counters = {"checked": 0, "settled": 0, "unresolved": 0, "void": 0}

    for match in candidates:
        sport_code = sport_codes.get(match.sport_id, "unknown")
        delay = _sport_delay(sport_code)
        if match.starts_at > now - timedelta(hours=delay):
            continue  # ещё рано — матч может идти

        signals = (
            (await session.execute(select(Signal).where(Signal.match_id == match.id, Signal.status.in_(
                (SignalStatus.CONFIRMED, SignalStatus.SENT, SignalStatus.CANDIDATE)
            ))))
            .scalars()
            .all()
        )
        if not signals:
            if match.status == MatchStatus.SCHEDULED:
                match.status = MatchStatus.FINISHED
            continue

        counters["checked"] += 1
        result = await fetch_result(session, match, providers, sport_code)
        if result is None:
            counters["unresolved"] += 1
            logger.warning(
                "tracker: match_id={} ({}), сигналов {} — результат не найден, они остаются открытыми",
                match.id, sport_code, len(signals),
            )
            continue

        home_score, away_score = result
        match.result_home, match.result_away = home_score, away_score
        match.status = MatchStatus.FINISHED
        # Лайв-прогнозы, которые так и не разослали, аннулируем: матч закончился.
        for signal in signals:
            new_status = settle_signal(signal, home_score, away_score)
            if signal.status == SignalStatus.CANDIDATE and new_status in (SignalStatus.WON, SignalStatus.LOST, SignalStatus.VOID):
                new_status = SignalStatus.REJECTED  # не рассылали → в статистику не идёт
            signal.status = new_status
            signal.settled_at = now
            if new_status == SignalStatus.VOID:
                counters["void"] += 1
            counters["settled"] += 1
            logger.info(
                "tracker: signal_id={} match_id={} {} {} @ {} → {} (счёт {}-{})",
                signal.id, match.id, signal.market, signal.selection, signal.odds, new_status, home_score, away_score,
            )

    await session.commit()
    logger.info("tracker: {}", counters)
    return counters


# --------------------------------------------------------------------------- #
# Статистика (ROI)
# --------------------------------------------------------------------------- #
async def performance_stats(session: AsyncSession, days: int = 30, sport_code: str | None = None) -> dict[str, Any]:
    """ROI/винрейт за период. ROI = (Σ stake×(odds−1) на выигранных − Σ stake на проигранных) / Σ всех stake."""
    edge = datetime.now(timezone.utc) - timedelta(days=days)
    query = select(Signal).where(Signal.status.in_((SignalStatus.WON, SignalStatus.LOST, SignalStatus.VOID)), Signal.settled_at >= edge)
    if sport_code:
        query = query.join(MatchRow, MatchRow.id == Signal.match_id).join(Sport, Sport.id == MatchRow.sport_id).where(Sport.code == sport_code)
    signals = (await session.execute(query)).scalars().all()

    won = [signal for signal in signals if signal.status == SignalStatus.WON]
    lost = [signal for signal in signals if signal.status == SignalStatus.LOST]
    void = [signal for signal in signals if signal.status == SignalStatus.VOID]

    staked = sum(float(signal.stake_pct or 0.0) for signal in signals if signal.status != SignalStatus.VOID)
    profit = sum(
        float(signal.stake_pct or 0.0) * (float(signal.odds or 0.0) - 1.0) for signal in won
    ) - sum(float(signal.stake_pct or 0.0) for signal in lost)
    return {
        "signals": len(signals),
        "won": len(won),
        "lost": len(lost),
        "void": len(void),
        "open": await _count_open(session, sport_code),
        "win_rate": round(len(won) / (len(won) + len(lost)), 4) if (won or lost) else 0.0,
        "stake_sum": round(staked, 2),
        "profit_units": round(profit, 2),
        "roi": round(profit / staked, 4) if staked else 0.0,
        "avg_odds": round(sum(float(signal.odds or 0.0) for signal in signals) / len(signals), 3) if signals else 0.0,
        "avg_score": round(sum(float(signal.confidence_score or 0.0) for signal in signals) / len(signals), 1) if signals else 0.0,
        "avg_edge": round(sum(float(signal.edge or 0.0) for signal in signals) / len(signals), 4) if signals else 0.0,
        "period_days": days,
        "sport": sport_code or "all",
    }


async def _count_open(session: AsyncSession, sport_code: str | None = None) -> int:
    query = select(func.count(Signal.id)).where(Signal.status.in_((SignalStatus.CONFIRMED, SignalStatus.SENT)))
    if sport_code:
        query = query.join(MatchRow, MatchRow.id == Signal.match_id).join(Sport, Sport.id == MatchRow.sport_id).where(Sport.code == sport_code)
    return int((await session.execute(query)).scalar() or 0)


async def performance_by_sport(session: AsyncSession, days: int = 30) -> dict[str, dict[str, Any]]:
    """ROI/винрейт в разрезе видов спорта — используется в /stats и дневной сводке."""
    sports = (await session.execute(select(Sport.code))).scalars().all()
    return {sport: await performance_stats(session, days=days, sport_code=sport) for sport in sports}


async def performance_by_market(session: AsyncSession, days: int = 30) -> list[dict[str, Any]]:
    """Разрез по рынкам (для модуля обучения и дневной сводки)."""
    edge = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        await session.execute(
            select(Signal.market, Signal.status, Signal.odds, Signal.stake_pct).where(
                Signal.status.in_((SignalStatus.WON, SignalStatus.LOST)), Signal.settled_at >= edge
            )
        )
    ).all()
    grouped: dict[str, dict[str, Any]] = {}
    for market, status, odds, stake in rows:
        bucket = grouped.setdefault(market, {"market": market, "won": 0, "lost": 0, "profit_units": 0.0, "stake_sum": 0.0})
        stake = float(stake or 0.0)
        if status == SignalStatus.WON:
            bucket["won"] += 1
            bucket["profit_units"] += stake * (float(odds or 0.0) - 1.0)
        else:
            bucket["lost"] += 1
            bucket["profit_units"] -= stake
        bucket["stake_sum"] += stake
    for bucket in grouped.values():
        total = bucket["won"] + bucket["lost"]
        bucket["win_rate"] = round(bucket["won"] / total, 4) if total else 0.0
        bucket["roi"] = round(bucket["profit_units"] / bucket["stake_sum"], 4) if bucket["stake_sum"] else 0.0
        bucket["profit_units"] = round(bucket["profit_units"], 2)
        bucket["stake_sum"] = round(bucket["stake_sum"], 2)
    return sorted(grouped.values(), key=lambda bucket: bucket["roi"], reverse=True)
