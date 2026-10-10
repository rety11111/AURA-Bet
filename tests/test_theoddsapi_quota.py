"""Перезапуск не должен тратить отдельный запрос The Odds API на каждый матч."""

from __future__ import annotations

from typing import Any

from app.sources.theoddsapi import TheOddsApiProvider


async def test_get_odds_shares_sport_snapshot_after_restart(monkeypatch) -> None:
    provider = TheOddsApiProvider(api_key="test-key")
    calls: list[str] = []
    events = [
        {
            "id": f"event-{n}", "home_team": "A", "away_team": "B",
            "bookmakers": [{"title": "book", "markets": [{"key": "h2h", "outcomes": [
                {"name": "A", "price": 2.0}, {"name": "B", "price": 2.1},
            ]}]}],
        }
        for n in range(3)
    ]

    async def fake_get(path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        calls.append(path)
        return events

    monkeypatch.setattr(provider, "_get", fake_get)
    # Симулируем неизвестные после рестарта ID: первый вызов находит спорт,
    # следующий использует индекс и общий снимок вместо нового HTTP-запроса.
    assert len(await provider.get_odds("event-0")) == 2
    assert len(await provider.get_odds("event-1")) == 2
    assert len(await provider.get_odds("event-2")) == 2
    assert len(calls) == 1
    assert calls[0].endswith("/odds")
