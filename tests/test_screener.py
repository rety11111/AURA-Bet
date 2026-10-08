"""Юнит-тесты детерминированного скринера (Ур.0)."""

from __future__ import annotations

import pytest

from app.config import settings
from app.db.models import DataQuality
from app.pipeline.screener import (
    ScreenInput,
    league_allowed,
    odds_in_range_count,
    screen,
    stats_available,
    summary,
)
from app.sources.odds_aggregator import AggregatedOutcome


def outcome(price: float, selection: str = "home", market: str = "1x2", suspicious: bool = False) -> AggregatedOutcome:
    return AggregatedOutcome(
        market=market,
        selection=selection,
        line=None,
        best_price=price,
        best_source="winline",
        implied=1.0 / price,
        implied_by_source={"winline": 1.0 / price},
        sources=2,
        suspicious=suspicious,
        suspicious_reason="тест" if suspicious else None,
    )


def test_league_allowed_defaults():
    assert league_allowed("football", "English Premier League")
    assert league_allowed("football", "Лига чемпионов УЕФА")
    assert league_allowed("hockey", "NHL")
    assert league_allowed("basketball", "NBA")
    assert league_allowed("tennis", "ATP 1000 Miami")
    assert not league_allowed("football", "Товарищеские матчи")
    assert not league_allowed("basketball", "NBL Австралия")


def test_league_allowed_esports_tier1():
    assert league_allowed("cs2", "BLAST Premier World Final", league_tier="S-Tier", is_esports=True)
    assert not league_allowed("cs2", "BLAST Premier World Final", league_tier="Tier 3", is_esports=True)
    assert league_allowed("dota2", "The International 2026", league_tier=None, is_esports=True)
    assert not league_allowed("dota2", "The International 2026", league_tier="Tier 4", is_esports=True)


def test_odds_and_stats_helpers():
    assert odds_in_range_count([outcome(1.9), outcome(1.2), outcome(3.5)]) == 2
    assert stats_available(ScreenInput(sport="football", league="EPL", has_stats=True))
    assert stats_available(
        ScreenInput(sport="football", league="EPL", home_matches_played=5, away_matches_played=6)
    )
    assert not stats_available(
        ScreenInput(sport="football", league="EPL", home_matches_played=2, away_matches_played=6)
    )


def test_screen_passes_regular_match():
    result = screen(
        ScreenInput(
            sport="football",
            league="English Premier League",
            outcomes=[outcome(1.9), outcome(3.5, "away")],
            has_stats=True,
        )
    )
    assert result.passed
    assert result.data_quality == DataQuality.OK
    assert result.odds_in_range == 2


def test_screen_rejects_out_of_whitelist():
    result = screen(
        ScreenInput(sport="football", league="Товарищеские матчи", outcomes=[outcome(2.0)], has_stats=True)
    )
    assert not result.passed
    assert "whitelist" in result.reasons[0]


def test_screen_rejects_without_odds_in_range():
    result = screen(
        ScreenInput(
            sport="football",
            league="English Premier League",
            outcomes=[outcome(1.2), outcome(settings.odds_max + 0.5, "away")],
            has_stats=True,
        )
    )
    assert not result.passed
    assert "диапазон" in result.reasons[0]


def test_screen_rejects_all_suspicious():
    result = screen(
        ScreenInput(
            sport="football",
            league="English Premier League",
            outcomes=[outcome(2.0, suspicious=True), outcome(3.4, "away", suspicious=True)],
            has_stats=True,
        )
    )
    assert not result.passed
    assert "suspicious" in result.reasons[0]


def test_screen_rejects_already_analyzed():
    result = screen(
        ScreenInput(
            sport="football",
            league="English Premier League",
            outcomes=[outcome(2.0)],
            has_stats=True,
            already_analyzed=True,
        )
    )
    assert not result.passed
    assert "уже анализировался" in result.reasons[0]


def test_screen_marks_weak_data_but_passes():
    result = screen(
        ScreenInput(
            sport="football",
            league="English Premier League",
            outcomes=[outcome(2.0)],
            home_matches_played=1,
            away_matches_played=2,
        )
    )
    assert result.passed
    assert result.data_quality == DataQuality.WEAK
    assert any("weak" in note for note in result.checked)


def test_screen_summary_shape():
    payload = summary(
        ScreenInput(
            sport="football",
            league="English Premier League",
            outcomes=[outcome(1.9), outcome(3.5, "away")],
            has_stats=True,
        )
    )
    assert payload["league"] == "English Premier League"
    assert payload["markets"] and payload["markets"][0]["price"] == 3.5
