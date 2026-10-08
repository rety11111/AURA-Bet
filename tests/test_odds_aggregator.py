"""Юнит-тесты агрегатора кэфов: медиана implied, лучшая цена, suspicious, снимки."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import settings
from app.sources.base import Odd
from app.sources.odds_aggregator import (
    OddsAggregator,
    _is_complete,
    aggregate_outcomes,
    group_by_source_market,
    normalize_margin,
)
from app.db.models import Match, Sport, Team


def odd(source: str, selection: str, price: float, market: str = "1x2", line: float | None = None) -> Odd:
    return Odd(market=market, selection=selection, price=price, source=source, line=line)


def test_normalize_margin_removes_overround():
    odds = [odd("winline", "home", 2.0), odd("winline", "draw", 3.5), odd("winline", "away", 4.0)]
    normalized = normalize_margin(odds)
    assert sum(normalized.values()) == pytest.approx(1.0)
    assert normalized["home"] == pytest.approx(0.5 / 1.035714, abs=1e-6)
    assert normalize_margin([]) == {}


def test_group_by_source_market_splits_lines():
    odds = [
        odd("winline", "over", 1.9, "totals", 2.5),
        odd("winline", "under", 1.9, "totals", 2.5),
        odd("betboom", "over", 2.0, "totals", 3.5),
    ]
    grouped = group_by_source_market(odds)
    assert ("winline", "totals", 2.5) in grouped
    assert ("betboom", "totals", 3.5) in grouped
    assert ("winline", "totals", 3.5) not in grouped


def test_is_complete_two_way_and_three_way():
    assert _is_complete("1x2", {"home", "draw", "away"})
    assert _is_complete("1x2", {"home", "away"})       # баскетбол/теннис
    assert not _is_complete("1x2", {"home"})
    assert _is_complete("totals", {"over", "under"})
    assert not _is_complete("totals", {"over"})


def test_aggregate_median_implied_and_best_price():
    odds = [
        odd("winline", "home", 2.00), odd("winline", "draw", 3.50), odd("winline", "away", 4.00),
        odd("betboom", "home", 2.10), odd("betboom", "draw", 3.40), odd("betboom", "away", 3.90),
    ]
    outcomes = {outcome.selection: outcome for outcome in aggregate_outcomes(odds)}
    home = outcomes["home"]
    # Лучшая цена — у betboom (2.10), implied — медиана по двум источникам
    assert home.best_price == pytest.approx(2.10)
    assert home.best_source == "betboom"
    assert home.sources == 2
    assert not home.suspicious
    expected_median = (normalize_margin([odds[0], odds[1], odds[2]])["home"] +
                       normalize_margin([odds[3], odds[4], odds[5]])["home"]) / 2
    assert home.implied == pytest.approx(expected_median, abs=1e-9)


def test_aggregate_detects_suspicious_divergence():
    odds = [
        # Источник A: явный фаворит
        odd("winline", "home", 1.20), odd("winline", "draw", 6.00), odd("winline", "away", 12.0),
        # Источник B: почти равный матч — расхождение implied > 10%
        odd("betboom", "home", 2.00), odd("betboom", "draw", 3.40), odd("betboom", "away", 4.00),
    ]
    outcomes = {outcome.selection: outcome for outcome in aggregate_outcomes(odds)}
    assert outcomes["home"].suspicious
    assert "расходятся" in (outcomes["home"].suspicious_reason or "")
    assert outcomes["home"].suspicious_reason is not None
    assert settings.max_suspicious_divergence == pytest.approx(0.10)


def test_incomplete_market_is_not_aggregated():
    odds = [odd("winline", "home", 2.0)]  # нет draw/away → маржу не убираем
    assert aggregate_outcomes(odds) == []


@pytest.mark.asyncio
async def test_store_snapshots_dedupes_unchanged_prices(session):
    sport = Sport(code="football", name="Футбол")
    session.add(sport)
    await session.flush()
    home = Team(sport_id=sport.id, canonical_name="A")
    away = Team(sport_id=sport.id, canonical_name="B")
    session.add_all([home, away])
    await session.flush()
    match = Match(
        sport_id=sport.id,
        ext_id="winline:1",
        league="English Premier League",
        home_team_id=home.id,
        away_team_id=away.id,
        starts_at=datetime.now(timezone.utc),
    )
    session.add(match)
    await session.flush()

    aggregator = OddsAggregator(providers=[])
    odds = [odd("winline", "home", 2.0), odd("winline", "away", 3.2)]
    written_first = await aggregator.store_snapshots(session, match.id, odds)
    written_second = await aggregator.store_snapshots(session, match.id, odds)
    assert written_first == 2
    assert written_second == 0  # цены не изменились → история не пухнет

    changed = [odd("winline", "home", 1.95), odd("winline", "away", 3.2)]
    assert await aggregator.store_snapshots(session, match.id, changed) == 1

    aggregated = await aggregator.aggregated_from_db(session, match.id)
    by_selection = {outcome.selection: outcome for outcome in aggregated}
    assert by_selection["home"].best_price == pytest.approx(1.95)
