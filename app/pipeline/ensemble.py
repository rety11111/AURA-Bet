"""Ансамбль: p_final = w_llm × p_llm + w_stat × p_stat.

Правила из ТЗ:
  * если есть стат-модель → взвешенная смесь вероятностей (веса из ensemble_weights,
    старт 0.6/0.4, адаптация в learning/calibration.py по Brier score);
  * тоталы: mu_final = взвешенное среднее, sigma_final = max(sigma) × 1.1;
  * если стат-модели нет → p_final = p_llm (для тенниса, бокса, MMA — это норма).

Всё чистая математика, без обращений к БД: веса передаются аргументом (их читает
pipeline/analyzer.py из таблицы ensemble_weights).
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from app.config import settings
from app.stats_models.base import StatPrediction


@dataclass
class EnsembleWeights:
    w_llm: float = settings.w_llm_init
    w_stat: float = settings.w_stat_init

    def normalized(self) -> EnsembleWeights:
        total = self.w_llm + self.w_stat
        if total <= 0:
            return EnsembleWeights(settings.w_llm_init, settings.w_stat_init)
        return EnsembleWeights(self.w_llm / total, self.w_stat / total)


@dataclass
class EnsembleResult:
    prob_home: float
    prob_away: float
    prob_draw: float | None
    expected_total: float
    total_sigma: float
    expected_margin: float
    margin_sigma: float
    w_llm: float
    w_stat: float
    used_stat_model: bool
    notes: list[str]

    def as_dict(self) -> dict[str, float | bool | list[str] | None]:
        return {
            "prob_home": round(self.prob_home, 4),
            "prob_draw": round(self.prob_draw, 4) if self.prob_draw is not None else None,
            "prob_away": round(self.prob_away, 4),
            "expected_total": round(self.expected_total, 3),
            "total_sigma": round(self.total_sigma, 3),
            "expected_margin": round(self.expected_margin, 3),
            "margin_sigma": round(self.margin_sigma, 3),
            "w_llm": round(self.w_llm, 3),
            "w_stat": round(self.w_stat, 3),
            "used_stat_model": self.used_stat_model,
            "notes": self.notes,
        }


def blend_probabilities(
    p_llm_home: float,
    p_llm_away: float,
    p_llm_draw: float | None,
    stat: StatPrediction | None,
    weights: EnsembleWeights | None = None,
) -> tuple[float, float, float | None, float, float]:
    """Смешивает вероятности. Возвращает (p_home, p_away, p_draw, w_llm, w_stat)."""
    weights = (weights or EnsembleWeights()).normalized()
    if stat is None:
        return p_llm_home, p_llm_away, p_llm_draw, 1.0, 0.0

    stat_draw = stat.prob_draw or 0.0
    llm_draw = p_llm_draw or 0.0
    prob_home = weights.w_llm * p_llm_home + weights.w_stat * stat.prob_home
    prob_away = weights.w_llm * p_llm_away + weights.w_stat * stat.prob_away
    prob_draw: float | None
    if p_llm_draw is None and stat.prob_draw is None:
        prob_draw = None
    else:
        prob_draw = weights.w_llm * llm_draw + weights.w_stat * stat_draw

    total = prob_home + prob_away + (prob_draw or 0.0)
    if total <= 0:
        return p_llm_home, p_llm_away, p_llm_draw, weights.w_llm, weights.w_stat
    return (
        prob_home / total,
        prob_away / total,
        (prob_draw / total) if prob_draw is not None else None,
        weights.w_llm,
        weights.w_stat,
    )


def blend_totals(
    llm_total: float,
    llm_total_sigma: float,
    stat: StatPrediction | None,
    weights: EnsembleWeights | None = None,
) -> tuple[float, float]:
    """Тотал: mu — взвешенное среднее, sigma = max(sigma) × 1.1 (ТЗ)."""
    weights = (weights or EnsembleWeights()).normalized()
    if stat is None:
        return llm_total, llm_total_sigma
    mu = weights.w_llm * llm_total + weights.w_stat * stat.expected_total
    sigma = max(llm_total_sigma, stat.total_sigma) * settings.sigma_inflation
    return mu, sigma


def blend_margin(
    llm_margin: float,
    llm_margin_sigma: float,
    stat: StatPrediction | None,
    weights: EnsembleWeights | None = None,
) -> tuple[float, float]:
    weights = (weights or EnsembleWeights()).normalized()
    if stat is None:
        return llm_margin, llm_margin_sigma
    margin = weights.w_llm * llm_margin + weights.w_stat * stat.expected_margin
    sigma = max(llm_margin_sigma, stat.margin_sigma) * settings.sigma_inflation
    return margin, sigma


def ensemble(
    *,
    llm_prob_home: float,
    llm_prob_away: float,
    llm_prob_draw: float | None,
    llm_expected_total: float,
    llm_total_sigma: float,
    llm_expected_margin: float,
    llm_margin_sigma: float,
    stat: StatPrediction | None,
    weights: EnsembleWeights | None = None,
) -> EnsembleResult:
    """Полный ансамбль: вероятности + тотал + разница."""
    prob_home, prob_away, prob_draw, w_llm, w_stat = blend_probabilities(
        llm_prob_home, llm_prob_away, llm_prob_draw, stat, weights
    )
    mu, sigma = blend_totals(llm_expected_total, llm_total_sigma, stat, weights)
    margin, margin_sigma = blend_margin(llm_expected_margin, llm_margin_sigma, stat, weights)

    notes: list[str] = []
    if stat is None:
        notes.append("стат-модели нет — p_final = p_llm")
    else:
        diff = abs(stat.prob_home - llm_prob_home)
        if diff > 0.08:
            notes.append(
                f"LLM и стат-модель расходятся на {diff:.1%} по хозяевам — "
                f"итог ближе к {'LLM' if w_llm > w_stat else 'модели'}"
            )
        if stat.data_quality == "weak":
            notes.append("стат-модель на слабых данных (weak)")
    logger.debug(
        "ensemble: p_home={:.3f} p_away={:.3f} (w_llm={:.2f}, w_stat={:.2f})",
        prob_home, prob_away, w_llm, w_stat,
    )
    return EnsembleResult(
        prob_home=prob_home,
        prob_away=prob_away,
        prob_draw=prob_draw,
        expected_total=mu,
        total_sigma=max(sigma, 0.1),
        expected_margin=margin,
        margin_sigma=max(margin_sigma, 0.1),
        w_llm=w_llm,
        w_stat=w_stat,
        used_stat_model=stat is not None,
        notes=notes,
    )
