"""Тесты парсера JSON букмекера (Winline/BetBoom): маппинги полей и линии.

ВАЖНО: это тесты на НАШИ маппинги (FIELD_MAPPING/MARKET_KEYWORDS/SELECTION_KEYWORDS),
а не на «настоящий» JSON Winline — он не захардкожен и берётся из окружения
(WINLINE_API_BASE). Схема-пример ниже покрывает типовые формы ответа.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.sources.winline import JsonBookmakerOddsProvider, parse_odds_payload


def odds(*pairs, market: str, line=None, source: str = "winline"):
    return [{"market": market, "selection": sel, "line": line, "price": price, "source": source} for sel, price in pairs]


PREMATCH_PAYLOAD = {
    "events": [
        {
            "id": 987654,
            "homeTeam": {"name": "Спартак Мск"},
            "awayTeam": {"name": "Зенит"},
            "league": {"name": "РПЛ"},
            "startTime": "2026-10-10T17:00:00+03:00",
            "markets": [
                {
                    "name": "1X2",
                    "outcomes": [
                        {"name": "1", "price": 2.40},
                        {"name": "X", "price": 3.30},
                        {"name": "2", "price": 3.00},
                    ],
                },
                {
                    "name": "Тотал 2.5",
                    "outcomes": [
                        {"name": "Больше", "price": 1.90, "handicap": 2.5},
                        {"name": "Меньше", "price": 1.95, "handicap": 2.5},
                    ],
                },
                {
                    "name": "Фора",
                    "outcomes": [
                        {"name": "Ф1", "price": 1.80, "handicap": -1.5},
                        {"name": "Ф2", "price": 2.05, "handicap": 1.5},
                    ],
                },
                {
                    "name": "Двойной шанс",
                    "outcomes": [
                        {"name": "1X", "price": 1.25},
                        {"name": "X2", "price": 1.60},
                    ],
                },
            ],
        }
    ]
}


def test_parse_matches_payload():
    provider = JsonBookmakerOddsProvider()
    provider.provider_name = "winline"

    matches = provider.parse_matches_payload(PREMATCH_PAYLOAD, sport="football")
    assert len(matches) == 1
    match = matches[0]
    assert match.home_team == "Спартак Мск"
    assert match.away_team == "Зенит"
    assert match.league == "РПЛ"
    assert match.ext_id == "987654"
    assert match.source == "winline"
    assert match.starts_at == datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc)  # 17:00 МСК = 14:00 UTC


def test_parse_odds_1x2():
    parsed = parse_odds_payload(PREMATCH_PAYLOAD, "winline")
    by_key = {(odd.market, odd.selection, odd.line): odd.price for odd in parsed}
    assert by_key[("1x2", "home", None)] == 2.40
    assert by_key[("1x2", "draw", None)] == 3.30
    assert by_key[("1x2", "away", None)] == 3.00
    assert all(odd.source == "winline" for odd in parsed)


def test_parse_odds_totals_and_handicap_lines():
    parsed = parse_odds_payload(PREMATCH_PAYLOAD, "winline")
    by_key = {(odd.market, odd.selection, odd.line): odd.price for odd in parsed}
    # Тотал: линия берётся из поля handicap или из названия рынка
    assert by_key[("totals", "over", 2.5)] == 1.90
    assert by_key[("totals", "under", 2.5)] == 1.95
    # Фора: сторона из «Ф1»/«Ф2», линия со знаком (−1.5 хозяевам, +1.5 гостям)
    assert by_key[("handicap", "home_handicap", -1.5)] == 1.80
    assert by_key[("handicap", "away_handicap", 1.5)] == 2.05
    # Двойной шанс
    assert by_key[("double_chance", "1x", None)] == 1.25
    assert by_key[("double_chance", "x2", None)] == 1.60


def test_parse_odds_payload_dedupes_duplicates():
    """Один и тот же исход, встреченный дважды (в разных ветках JSON), не дублируется."""
    parsed = parse_odds_payload(PREMATCH_PAYLOAD, "winline")
    keys = [(odd.market, odd.selection, odd.line) for odd in parsed]
    assert len(keys) == len(set(keys))


def test_market_type_detection_without_market_name():
    """Рынок без явного названия («1»/«X»/«2») должен распознаться как 1X2 по структуре."""
    payload = {
        "data": [
            {
                "id": 1,
                "team1": {"name": "A"},
                "team2": {"name": "B"},
                "start": 1770000000,
                "odds": [
                    {"name": "1", "value": 2.1},
                    {"name": "X", "value": 3.2},
                    {"name": "2", "value": 3.4},
                ],
            }
        ]
    }
    parsed = parse_odds_payload(payload, "winline")
    selections = {odd.selection: odd.price for odd in parsed if odd.market == "1x2"}
    assert selections == {"home": 2.1, "draw": 3.2, "away": 3.4}


LIVE_PAYLOAD = {
    "events": [
        {
            "id": 555,
            "team1": {"name": "Team Spirit"},
            "team2": {"name": "Team Liquid"},
            "league": {"name": "The International"},
            "score1": 1,
            "score2": 0,
            "stage": "Карта 2, раунд 7",
            "start": 1770000000,
        }
    ]
}


def test_parse_live_scores_and_stage():
    """Лайв-событие: счёт серии/карты и стадия нужны воркеру киберспорта."""
    provider = JsonBookmakerOddsProvider()
    provider.provider_name = "winline"

    matches = provider.parse_matches_payload(LIVE_PAYLOAD, sport="cs2", live=True)
    assert len(matches) == 1
    match = matches[0]
    assert match.is_live is True and match.status == "live"
    assert match.home_score == 1 and match.away_score == 0
    assert match.live_stage == "Карта 2, раунд 7"
    assert match.league == "The International"


def test_parse_prematch_has_no_scores():
    provider = JsonBookmakerOddsProvider()
    provider.provider_name = "winline"
    match = provider.parse_matches_payload(PREMATCH_PAYLOAD, sport="football")[0]
    assert match.is_live is False and match.status == "scheduled"
    assert match.home_score is None and match.away_score is None
