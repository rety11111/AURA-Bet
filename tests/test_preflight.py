"""Preflight: источник кэфов считается настроенным, если задан Winline/BetBoom URL
либо THE_ODDS_API_KEY при включённом THE_ODDS_API_ENABLED (TheOddsApi)."""

from __future__ import annotations

import pytest

from app.config import settings
from app.main import odds_source_problem


@pytest.fixture
def no_odds_sources(monkeypatch):
    """Чистая база: ни один источник кэфов не настроен."""
    monkeypatch.setattr(settings, "winline_api_base", "")
    monkeypatch.setattr(settings, "betboom_api_base", "")
    monkeypatch.setattr(settings, "the_odds_api_key", "")
    monkeypatch.setattr(settings, "the_odds_api_enabled", False)


def test_problem_when_no_sources_at_all(no_odds_sources):
    problem = odds_source_problem()
    assert problem is not None
    # Сообщение должно называть оба варианта настройки.
    assert "WINLINE_API_BASE" in problem
    assert "BETBOOM_API_BASE" in problem
    assert "THE_ODDS_API_KEY" in problem
    assert "THE_ODDS_API_ENABLED" in problem


def test_winline_alone_is_enough(no_odds_sources, monkeypatch):
    monkeypatch.setattr(settings, "winline_api_base", "https://example.invalid/api")
    assert odds_source_problem() is None


def test_betboom_alone_is_enough(no_odds_sources, monkeypatch):
    monkeypatch.setattr(settings, "betboom_api_base", "https://example.invalid/api")
    assert odds_source_problem() is None


def test_theoddsapi_key_and_enabled_is_enough(no_odds_sources, monkeypatch):
    monkeypatch.setattr(settings, "the_odds_api_key", "test-key-not-real")
    monkeypatch.setattr(settings, "the_odds_api_enabled", True)
    assert odds_source_problem() is None


def test_theoddsapi_key_without_enabled_is_not_enough(no_odds_sources, monkeypatch):
    monkeypatch.setattr(settings, "the_odds_api_key", "test-key-not-real")
    monkeypatch.setattr(settings, "the_odds_api_enabled", False)
    assert odds_source_problem() is not None


def test_theoddsapi_enabled_without_key_is_not_enough(no_odds_sources, monkeypatch):
    monkeypatch.setattr(settings, "the_odds_api_key", "")
    monkeypatch.setattr(settings, "the_odds_api_enabled", True)
    assert odds_source_problem() is not None
