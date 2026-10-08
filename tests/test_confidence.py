"""Юнит-тесты композитной уверенности (Модуль 5)."""

from __future__ import annotations

import pytest

from app.config import settings
from app.db.models import DataQuality
from app.pipeline.confidence import composite_score, data_score, explain, raw_score


def test_data_score():
    assert data_score("ok") == settings.data_score_ok
    assert data_score("weak") == settings.data_score_weak
    assert data_score(None) == settings.data_score_ok


def test_raw_score_full_formula():
    # edge 0.15 → полный вклад; llm 0.8; data ok
    # 0.45×1 + 0.35×0.8 + 0.20×1 = 0.93
    assert raw_score(0.15, 0.8, "ok") == pytest.approx(0.93)
    # edge 0.06 → 0.4 от полного вклада: 0.45×0.4 + 0.35×0.5 + 0.20×0.5 = 0.455
    assert raw_score(0.06, 0.5, "weak") == pytest.approx(0.455)
    # edge выше порога «полного кредита» не увеличивает вклад
    assert raw_score(0.5, 0.8, "ok") == pytest.approx(0.93)


def test_composite_score_clamps():
    assert composite_score(0.15, 0.8, "ok") == pytest.approx(93.0)
    assert composite_score(0.06, 0.5, "weak") == pytest.approx(45.5)
    # Огромный edge + максимальная уверенность → упирается в верхний кламп
    assert composite_score(1.0, 1.0, "ok") == pytest.approx(settings.conf_clamp_max)
    # Очень слабые данные/нулевая уверенность → нижний кламп
    assert composite_score(0.0, 0.0, "weak") == pytest.approx(settings.conf_clamp_min)


def test_calibration_multiplier():
    base = composite_score(0.10, 0.6, "ok", calibration=1.0)
    boosted = composite_score(0.10, 0.6, "ok", calibration=1.15)
    degraded = composite_score(0.10, 0.6, "ok", calibration=0.75)
    assert boosted > base > degraded
    assert boosted == pytest.approx(base * 1.15, abs=0.1)


def test_judge_delta():
    base = composite_score(0.10, 0.6, "ok")
    assert composite_score(0.10, 0.6, "ok", confidence_delta=0.1) == pytest.approx(min(95.0, base + 10.0))
    assert composite_score(0.10, 0.6, "ok", confidence_delta=-0.2) == pytest.approx(base - 20.0)


def test_judge_delta_respects_clamp():
    assert composite_score(0.15, 1.0, "ok", confidence_delta=0.2) == pytest.approx(settings.conf_clamp_max)
    assert composite_score(0.0, 0.0, "weak", confidence_delta=-0.2) == pytest.approx(settings.conf_clamp_min)


def test_explain_breakdown():
    breakdown = explain(0.15, 0.8, "ok")
    assert breakdown["edge_part"] == pytest.approx(0.45)
    assert breakdown["llm_part"] == pytest.approx(0.28)
    assert breakdown["data_part"] == pytest.approx(0.20)
    assert breakdown["score"] == pytest.approx(93.0)
