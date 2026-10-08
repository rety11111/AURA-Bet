"""Preflight-проверка источников коэффициентов (app/main.py → odds_sources_problem).

Источник считается настроенным, если задан URL Winline/BetBoom (прематч или лайв)
либо THE_ODDS_API_KEY и THE_ODDS_API_ENABLED=true. Значения тестируем через monkeypatch,
без реальных URL букмекеров и ключей.
"""

from __future__ import annotations

import pytest

from app import main
from app.config import settings

ODDS_FIELDS = (
    "winline_api_base",
    "winline_live_api_base",
    "betboom_api_base",
    "betboom_live_api_base",
    "the_odds_api_key",
)


@pytest.fixture
def no_odds_sources(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Все источники коэффициентов выключены."""
    for field in ODDS_FIELDS:
        monkeypatch.setattr(settings, field, "")
    monkeypatch.setattr(settings, "the_odds_api_enabled", False)
    return monkeypatch


def test_no_sources_reports_problem(no_odds_sources: pytest.MonkeyPatch) -> None:
    problem = main.odds_sources_problem()
    assert problem is not None
    # Сообщение должно называть все варианты настройки, а не только Winline/BetBoom.
    for name in ("WINLINE_API_BASE", "BETBOOM_API_BASE", "THE_ODDS_API_KEY", "THE_ODDS_API_ENABLED"):
        assert name in problem


def test_winline_prematch_url_is_enough(no_odds_sources: pytest.MonkeyPatch) -> None:
    no_odds_sources.setattr(settings, "winline_api_base", "https://example.invalid/winline")
    assert main.odds_sources_problem() is None


def test_betboom_prematch_url_is_enough(no_odds_sources: pytest.MonkeyPatch) -> None:
    no_odds_sources.setattr(settings, "betboom_api_base", "https://example.invalid/betboom")
    assert main.odds_sources_problem() is None


def test_live_only_bookmaker_url_is_enough(no_odds_sources: pytest.MonkeyPatch) -> None:
    """Провайдер Winline/BetBoom доступен и по лайв-URL (см. winline.py: available)."""
    no_odds_sources.setattr(settings, "betboom_live_api_base", "https://example.invalid/betboom-live")
    assert main.odds_sources_problem() is None


def test_theoddsapi_needs_key_and_enabled_flag(no_odds_sources: pytest.MonkeyPatch) -> None:
    # Только ключ — выключенный провайдер не считается настроенным.
    no_odds_sources.setattr(settings, "the_odds_api_key", "test-key")
    problem = main.odds_sources_problem()
    assert problem is not None
    assert "THE_ODDS_API_ENABLED" in problem

    # Ключ + включение — настроен.
    no_odds_sources.setattr(settings, "the_odds_api_enabled", True)
    assert main.odds_sources_problem() is None


def test_theoddsapi_enabled_without_key_is_not_configured(no_odds_sources: pytest.MonkeyPatch) -> None:
    no_odds_sources.setattr(settings, "the_odds_api_enabled", True)
    problem = main.odds_sources_problem()
    assert problem is not None
    assert "THE_ODDS_API_KEY" in problem
