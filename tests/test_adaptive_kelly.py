"""Тесты адаптивного Келли и расчета CLV (app/pipeline/value_engine.py)."""

from __future__ import annotations

import pytest

from app.db.models import DataQuality
from app.pipeline.value_engine import calculate_clv, kelly_stake_pct


def test_standard_kelly_unchanged() -> None:
    # Без указания sport и data_quality работает по классической формуле
    assert kelly_stake_pct(0.55, 2.0) == pytest.approx(2.5)


def test_adaptive_kelly_high_odds() -> None:
    # При высоких коэффициентах (> 2.50) доля уменьшается для защиты от дисперсии
    standard = kelly_stake_pct(0.40, 3.0, adaptive=False)  # full kelly = (1.2 - 1)/2 = 0.1 → 2.5%
    adaptive = kelly_stake_pct(0.40, 3.0, adaptive=True, sport="football", data_quality=DataQuality.OK)
    assert adaptive < standard


def test_adaptive_kelly_weak_data_quality() -> None:
    # При неполных данных ставка снижается на 20%
    standard = kelly_stake_pct(0.55, 2.0, data_quality=DataQuality.OK)
    weak = kelly_stake_pct(0.55, 2.0, data_quality=DataQuality.WEAK)
    assert weak < standard


def test_calculate_clv() -> None:
    # Взяли по 2.20, линия закрылась по 2.00 → +10% CLV
    assert calculate_clv(2.20, 2.00) == pytest.approx(10.0)

    # Взяли по 1.90, линия уехала до 2.00 → −5% CLV
    assert calculate_clv(1.90, 2.00) == pytest.approx(-5.0)

    # Некорректные кэфы
    assert calculate_clv(1.0, 2.0) == 0.0
