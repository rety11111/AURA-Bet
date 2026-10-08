"""Базовые контракты статистических моделей.

StatPrediction — то, что отдают все модели (Пуассон, баскетбол, Elo) и что уходит
дальше: в ансамбль (pipeline/ensemble.py), в Value Engine и в промпт аналитика.

Математика — чистый Python + стандартная библиотека (statistics.NormalDist),
никаких scipy/numpy: юнит-тесты считают вручную и сверяют с известными значениями.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from statistics import NormalDist
from typing import Any

from pydantic import BaseModel, Field, model_validator

_NORMAL = NormalDist()


def norm_cdf(x: float, mu: float = 0.0, sigma: float = 1.0) -> float:
    """Φ((x − mu) / sigma) — функция распределения нормального закона."""
    if sigma <= 0:
        raise ValueError("sigma must be > 0")
    return _NORMAL.cdf((x - mu) / sigma)


def norm_ppf(p: float, mu: float = 0.0, sigma: float = 1.0) -> float:
    """Обратная функция нормального распределения (квантиль)."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    return mu + sigma * _NORMAL.inv_cdf(p)


def prob_over(line: float, mu: float, sigma: float) -> float:
    """P(тотал > line) = 1 − Φ((line − mu)/sigma)."""
    return 1.0 - norm_cdf(line, mu, sigma)


def prob_under(line: float, mu: float, sigma: float) -> float:
    """P(тотал < line) = Φ((line − mu)/sigma)."""
    return norm_cdf(line, mu, sigma)


def prob_handicap(home_handicap: float, margin_mu: float, margin_sigma: float) -> float:
    """P(фора зашла хозяевам) = P(margin > |h|) для отрицательной форы h.

    Принято: фора хозяевам задаётся как число h (например −1.5). Хозяева «покрывают»
    фору, если margin (хозяева − гости) больше −h, то есть margin + h > 0.
    """
    threshold = -home_handicap
    return 1.0 - norm_cdf(threshold, margin_mu, margin_sigma)


class StatPrediction(BaseModel):
    """Прогноз статистической модели."""

    model: str
    prob_home: float
    prob_draw: float | None = None
    prob_away: float
    expected_total: float
    total_sigma: float
    expected_margin: float
    margin_sigma: float
    sample_size: int = 0
    data_quality: str = "ok"          # ok | weak
    notes: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize(self) -> StatPrediction:
        total = self.prob_home + (self.prob_draw or 0.0) + self.prob_away
        if total <= 0:
            raise ValueError("сумма вероятностей должна быть > 0")
        if abs(total - 1.0) > 1e-6:
            # Модель обязана отдавать нормированное распределение — нормируем сами,
            # но фиксируем это в notes (значит, где-то была численная погрешность/ошибка).
            self.prob_home /= total
            if self.prob_draw is not None:
                self.prob_draw /= total
            self.prob_away /= total
            self.notes.append(f"вероятности нормированы (исходная сумма {total:.6f})")
        return self

    # ------------------------------------------------------------- helpers
    def prob_over(self, line: float) -> float:
        return prob_over(line, self.expected_total, self.total_sigma)

    def prob_under(self, line: float) -> float:
        return prob_under(line, self.expected_total, self.total_sigma)

    def prob_home_handicap(self, handicap: float) -> float:
        return prob_handicap(handicap, self.expected_margin, self.margin_sigma)

    def prob_away_handicap(self, handicap: float) -> float:
        """Фора гостям: line = +1.5 → P(margin < 1.5) = 1 − P(margin > line).

        Зеркально prob_home_handicap: для пары Ф1 (−1.5) / Ф2 (+1.5)
        вероятности складываются в 1 (без учёта «пуша» на целых линиях).
        """
        return 1.0 - prob_handicap(-handicap, self.expected_margin, self.margin_sigma)

    def to_prompt_json(self) -> dict[str, Any]:
        """Компактное представление для промпта аналитика/арбитра."""
        payload: dict[str, Any] = {
            "model": self.model,
            "prob_home": round(self.prob_home, 4),
            "prob_away": round(self.prob_away, 4),
            "expected_total": round(self.expected_total, 2),
            "total_sigma": round(self.total_sigma, 2),
            "expected_margin": round(self.expected_margin, 2),
            "margin_sigma": round(self.margin_sigma, 2),
            "sample_size": self.sample_size,
            "data_quality": self.data_quality,
            "notes": self.notes[:5],
        }
        if self.prob_draw is not None:
            payload["prob_draw"] = round(self.prob_draw, 4)
        if self.extra:
            payload["extra"] = {k: v for k, v in list(self.extra.items())[:10]}
        return payload


class StatModel(ABC):
    """Интерфейс статистической модели."""

    name = "base"
    sport = "generic"

    @abstractmethod
    async def predict(self, *args: Any, **kwargs: Any) -> StatPrediction | None:
        """Возвращает прогноз или None, если данных недостаточно (деградация)."""

    def is_available(self) -> bool:
        return True


def poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam**k / math.factorial(k)


def poisson_mean_sigma(lams: list[float]) -> tuple[float, float]:
    """Для суммы независимых пуассоновских величин: mean = Σλ, sigma = sqrt(Σλ)."""
    mu = sum(lams)
    return mu, math.sqrt(mu)
