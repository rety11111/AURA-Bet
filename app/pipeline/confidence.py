"""Композитная уверенность сигнала (Модуль 5).

score = 100 × (0.45 × min(edge/0.15, 1.0) + 0.35 × llm_confidence + 0.20 × data_score) × calibration
где data_score = 1.0 (ok) / 0.5 (weak), calibration — из learning/calibration.py (по умолчанию 1.0).
Итог clamp(30, 95) + confidence_delta от арбитра (тоже с клампом).

Почему так: edge — главный, но далеко не единственный фактор. Модель, которая находит
edge 15%+, но с «weak» данными и низкой уверенностью LLM, не должна давать топ-сигнал.
"""

from __future__ import annotations

from app.config import settings
from app.db.models import DataQuality


def data_score(data_quality: str | None) -> float:
    return settings.data_score_ok if data_quality != DataQuality.WEAK else settings.data_score_weak


def raw_score(edge: float, llm_confidence: float, data_quality: str | None, calibration: float = 1.0) -> float:
    """Сырой балл 0..1 (до клампа). Вынесен отдельно — именно его проверяют тесты."""
    edge_part = min(max(edge, 0.0) / settings.conf_edge_full_credit, 1.0)
    confidence_part = min(max(llm_confidence, 0.0), 1.0)
    composite = (
        settings.conf_w_edge * edge_part
        + settings.conf_w_llm * confidence_part
        + settings.conf_w_data * data_score(data_quality)
    )
    return composite * max(calibration, 0.01)


def composite_score(
    edge: float,
    llm_confidence: float,
    data_quality: str | None,
    calibration: float = 1.0,
    confidence_delta: float = 0.0,
) -> float:
    """Итоговый score 0..100 с клампом [30, 95]."""
    score = 100.0 * raw_score(edge, llm_confidence, data_quality, calibration)
    score = max(settings.conf_clamp_min, min(settings.conf_clamp_max, score))
    if confidence_delta:
        score = max(settings.conf_clamp_min, min(settings.conf_clamp_max, score + 100.0 * confidence_delta))
    return round(score, 1)


def explain(
    edge: float, llm_confidence: float, data_quality: str | None, calibration: float = 1.0
) -> dict[str, float]:
    """Разложение score по компонентам — для логов/отладки."""
    edge_part = min(max(edge, 0.0) / settings.conf_edge_full_credit, 1.0)
    return {
        "edge_part": round(settings.conf_w_edge * edge_part, 4),
        "llm_part": round(settings.conf_w_llm * min(max(llm_confidence, 0.0), 1.0), 4),
        "data_part": round(settings.conf_w_data * data_score(data_quality), 4),
        "calibration": calibration,
        "score": composite_score(edge, llm_confidence, data_quality, calibration),
    }
