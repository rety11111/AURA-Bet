"""Интеграционный смоук-тест контура: агрегатор → value engine → сигнал → сообщение.

Проверяем стыки модулей на синтетических данных (никакой сети/БД):
  1) OddsAggregator сводит кэфы двух «букмекеров» (медиана implied, лучшая цена);
  2) Л0-скринер пропускает матч;
  3) value_engine находит кандидата и считает score Келли;
  4) select_signals выдаёт сигнал с ожидаемыми параметрами;
  5) сообщение для Telegram собирается по шаблону ТЗ (проверяем ключевые строки);
  6) confidence/calibration применяются к баллу.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.bot.broadcaster import format_signal_message
from app.db.models import DataQuality, Match as MatchRow, Signal, SignalStatus
from app.pipeline.confidence import composite_score
from app.pipeline.screener import ScreenInput, screen
from app.pipeline.value_engine import (
    MarketProbabilities,
    build_signal_payload,
    find_candidates,
    select_signals,
)
from app.sources.base import Odd
from app.sources.odds_aggregator import aggregate_outcomes


def make_odds(source: str, home: float, draw: float, away: float) -> list[Odd]:
    return [
        Odd(market="1x2", selection="home", price=home, source=source),
        Odd(market="1x2", selection="draw", price=draw, source=source),
        Odd(market="1x2", selection="away", price=away, source=source),
    ]


@pytest.mark.asyncio
async def test_full_contour_synthetic_match():
    # 1) агрегатор: Winline чуть «дышит» в сторону BetBoom (медиана и лучшая цена)
    odds = make_odds("winline", 2.55, 3.40, 2.95) + make_odds("betboom", 2.70, 3.30, 2.85)
    outcomes = aggregate_outcomes(odds)
    assert {outcome.selection for outcome in outcomes} == {"home", "draw", "away"}
    home = next(outcome for outcome in outcomes if outcome.selection == "home")
    assert home.best_price == 2.70 and home.best_source == "betboom"
    assert not home.suspicious

    # 2) скринер: лига из whitelist, кэф в диапазоне, статистика есть
    result = screen(
        ScreenInput(
            sport="football",
            league="English Premier League",
            outcomes=outcomes,
            has_stats=True,
            starts_at=datetime.now(timezone.utc) + timedelta(hours=5),
        )
    )
    assert result.passed and result.data_quality == DataQuality.OK

    # 3) аналитический слой (в живой системе это ансамбль LLM + стат-модель)
    probabilities = MarketProbabilities(
        prob_home=0.55,
        prob_draw=0.22,
        prob_away=0.23,
        expected_total=2.8,
        total_sigma=1.5,
        expected_margin=0.6,
        margin_sigma=1.6,
        llm_confidence=0.7,
        key_factors=["форма хозяев 5 побед в 6 играх", "гости без основного защитника"],
    )
    candidates = find_candidates(outcomes, probabilities)
    assert candidates, "должен найтись хотя бы один кандидат (edge ≥ 0.06)"
    best = max(candidates, key=lambda candidate: candidate.edge)
    assert best.selection == "home" and best.odds == 2.70

    # 4) отбор сигналов: score с калибровкой лиги
    selected = select_signals(
        candidates,
        confidence_score=probabilities.llm_confidence,
        data_quality=DataQuality.OK,
        calibration=1.0,
    )
    assert selected, "сигнал должен пройти порог уверенности"
    candidate, raw_score = selected[0]
    assert raw_score >= 60
    calibrated = composite_score(
        edge=candidate.edge, llm_confidence=0.7, data_quality="ok", calibration=0.75
    )
    assert calibrated < raw_score  # плохая калибровка лиги снижает балл

    # 5) payload для записи в signals
    payload = build_signal_payload(candidate, raw_score, probabilities, data_quality="ok")
    assert payload["stake_pct"] > 0 and payload["stake_pct"] <= 3.0
    assert payload["edge"] == pytest.approx(candidate.edge, abs=1e-4)
    assert payload["key_factors"]

    # 6) сообщение по шаблону ТЗ
    match = MatchRow(
        id=42,
        sport_id=1,
        ext_id="winline:42",
        league="English Premier League",
        home_team_id=1,
        away_team_id=2,
        starts_at=datetime.now(timezone.utc) + timedelta(hours=5),
        external_refs={},
    )
    signal = Signal(
        match_id=42,
        market=payload["market"],
        selection=payload["selection"],
        line=payload["line"],
        odds=payload["odds"],
        prob_final=payload["prob_final"],
        prob_implied=payload["prob_implied"],
        edge=payload["edge"],
        confidence_score=payload["confidence_score"],
        stake_pct=payload["stake_pct"],
        reasoning=payload["reasoning"],
        key_factors=payload["key_factors"],
        risk_notes=["состав хозяев объявят за час до матча"],
        status=SignalStatus.CONFIRMED,
        odds_source=payload["odds_source"],
        judge_reason="перевес подтверждён, линия не двигалась",
    )

    text = await format_signal_message(signal, match, "football", "Арсенал", "Челси")
    assert "⚽" in text and "Арсенал" in text and "Челси" in text
    assert "Ставка:" in text and "П1" in text
    assert "Value:" in text and "Уверенность:" in text
    assert "банка" in text and "Почему:" in text and "Риски:" in text
    assert "Арбитр" in text
    assert text.count("<b>") == text.count("</b>")  # HTML сбалансирован
