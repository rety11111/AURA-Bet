"""Пуассоновская модель для футбола (Understat xG) и хоккея (MoneyPuck xGoals).

Идея (классическая Dixon–Coles-подобная атака/защита):
    λ_home = atk_home × def_away × league_avg_home_goals
    λ_away = atk_away × def_home × league_avg_away_goals
где atk = (xG команды за игру) / (средние голы лиги), def = (xGA команды за игру) / (средние голы лиги).
Для хозяев/гостей используются РАЗДЕЛЬНЫЕ показатели (дома/в гостях), если источник их даёт;
иначе применяется домашнее преимущество константой (settings.hockey_home_advantage).

Из матрицы счётов 0..7 считаются 1X2, тоталы и форы — точно, без нормальной аппроксимации.
Но StatPrediction дополнительно несёт expected_total/total_sigma, чтобы Value Engine
мог считать любые линии (в т.ч. «тотал 4.75»), как и требует ТЗ.

⚠️ ЧЕСТНО ПРО SIGMA ⚠️
Теоретическая сигма суммы независимых Пуассонов = sqrt(λh + λa). Реальная сигма
тоталов больше (коррелированность, ошибки модели). Поэтому дефолт:
    total_sigma = sqrt(λh + λa) × sigma_inflation_static (по умолчанию 1.25)
Если у модели есть история ошибок (аргумент historical_sigma — её считает
learning/calibration.py), используется она.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.config import settings
from app.sources.base import TeamXgStats
from app.stats_models.base import StatModel, StatPrediction, poisson_pmf

DEFAULT_SIGMA_INFLATION = 1.25


@dataclass
class AttackDefence:
    """Атака/защита в «домашних» и «гостевых» разрезах (в единицах голов лиги)."""

    atk_home: float
    def_home: float
    atk_away: float
    def_away: float

    def blended(self) -> tuple[float, float]:
        """Общий уровень атаки/защиты (если сплитов нет)."""
        return (self.atk_home + self.atk_away) / 2.0, (self.def_home + self.def_away) / 2.0


def build_attack_defence(
    stats: TeamXgStats,
    league_avg_home: float,
    league_avg_away: float,
    fallback_league_avg: float = 1.35,
) -> AttackDefence:
    """Переводит xG/xGA команды в множители атаки/защиты относительно средних лиги."""
    league_home = league_avg_home if league_avg_home and league_avg_home > 0.05 else fallback_league_avg
    league_away = league_avg_away if league_avg_away and league_avg_away > 0.05 else fallback_league_avg

    overall_for = stats.xg_for_per_game or stats.goals_for_per_game or league_home
    overall_against = stats.xg_against_per_game or stats.goals_against_per_game or league_away
    league_overall = (league_home + league_away) / 2.0 or fallback_league_avg

    atk_home = (stats.xg_for_home or overall_for) / league_home
    def_home = (stats.xg_against_home or overall_against) / league_away
    atk_away = (stats.xg_for_away or overall_for) / league_away
    def_away = (stats.xg_against_away or overall_against) / league_home

    # Защита от вырожденных значений (маленькая выборка, нули в данных)
    return AttackDefence(
        atk_home=_clamp(atk_home),
        def_home=_clamp(def_home),
        atk_away=_clamp(atk_away),
        def_away=_clamp(def_away),
    )


def _clamp(value: float, low: float = 0.25, high: float = 2.5) -> float:
    return max(low, min(high, value))


def score_matrix(lambdas_home: float, lambdas_away: float, max_goals: int | None = None) -> list[list[float]]:
    """Матрица вероятностей счётов (0..max_goals) × (0..max_goals), нормированная к 1.

    Сетка обрезана на max_goals (по ТЗ — 0..7), поэтому «хвост» (например, 8:2)
    в неё не попадает. Чтобы вероятности оставались корректным распределением
    (а не были систематически занижены на массу хвоста), нормируем матрицу на
    её сумму. Особенно важно для хоккея: при λ ≈ 6 потеря массы без нормировки
    достигала бы ~2% и искажала бы value-расчёт тоталов.
    """
    max_goals = max_goals if max_goals is not None else settings.score_grid_max_goals
    home_probs = [poisson_pmf(goal, lambdas_home) for goal in range(max_goals + 1)]
    away_probs = [poisson_pmf(goal, lambdas_away) for goal in range(max_goals + 1)]
    matrix = [[home_p * away_p for away_p in away_probs] for home_p in home_probs]
    mass = sum(sum(row) for row in matrix)
    if mass > 0:
        matrix = [[probability / mass for probability in row] for row in matrix]
    return matrix


def outcomes_from_matrix(matrix: list[list[float]]) -> dict[str, float]:
    """Вероятности 1X2, ожидаемые голы и сигма тотала из матрицы счётов."""
    prob_home = prob_draw = prob_away = 0.0
    total = 0.0
    total_sq = 0.0
    margin = 0.0
    margin_sq = 0.0
    for home_goals, row in enumerate(matrix):
        for away_goals, probability in enumerate(row):
            if probability <= 0.0:
                continue
            prob_home += probability if home_goals > away_goals else 0.0
            prob_draw += probability if home_goals == away_goals else 0.0
            prob_away += probability if home_goals < away_goals else 0.0
            goals_total = home_goals + away_goals
            goals_margin = home_goals - away_goals
            total += probability * goals_total
            total_sq += probability * goals_total * goals_total
            margin += probability * goals_margin
            margin_sq += probability * goals_margin * goals_margin
    total_var = max(0.0, total_sq - total * total)
    margin_var = max(0.0, margin_sq - margin * margin)
    return {
        "prob_home": prob_home,
        "prob_draw": prob_draw,
        "prob_away": prob_away,
        "expected_total": total,
        "matrix_sigma_total": math.sqrt(total_var),
        "expected_margin": margin,
        "matrix_sigma_margin": math.sqrt(margin_var),
    }


class PoissonModel(StatModel):
    """Общая Пуассоновская модель (футбол и хоккей различаются только данными/конфигом)."""

    name = "poisson"

    def __init__(self, sport: str = "football") -> None:
        self.sport = sport
        self.name = "poisson" if sport == "football" else f"poisson_{sport}"

    async def predict(  # type: ignore[override]
        self,
        home_stats: TeamXgStats | None,
        away_stats: TeamXgStats | None,
        league_avg_home: float | None = None,
        league_avg_away: float | None = None,
        home_advantage: float | None = None,
        historical_sigma: float | None = None,
        min_matches: int | None = None,
    ) -> StatPrediction | None:
        """Прогноз по xG двух команд. None — если данных недостаточно."""
        if home_stats is None or away_stats is None:
            logger.debug("{}: нет xG-статистики одной из команд — модель пропускает матч", self.name)
            return None

        min_matches = min_matches if min_matches is not None else (
            settings.football_form_matches if self.sport == "football" else settings.hockey_form_matches
        )
        sample = min(home_stats.matches, away_stats.matches)
        # Порог «слабых данных» — общий конфиг (ТЗ: min_recent_matches). min_matches — окно
        # формы для спорта: если выборка его не дотягивает, прогноз помечается in notes.
        weak = sample < settings.min_recent_matches
        form_window = min_matches if min_matches is not None else (
            settings.football_form_matches if self.sport == "football" else settings.hockey_form_matches
        )

        if self.sport == "hockey":
            default_home, default_away = 3.0, 2.7  # средние голы NHL (≈6.0 на матч с преимуществом)
        else:
            default_home, default_away = 1.55, 1.20

        adv = home_advantage if home_advantage is not None else (
            settings.hockey_home_advantage if self.sport == "hockey" else 0.0
        )

        home_ad = build_attack_defence(home_stats, league_avg_home or default_home, league_avg_away or default_away)
        away_ad = build_attack_defence(away_stats, league_avg_home or default_home, league_avg_away or default_away)

        lh = home_ad.atk_home * away_ad.def_away * (league_avg_home or default_home)
        la = away_ad.atk_away * home_ad.def_home * (league_avg_away or default_away)

        if adv:
            # Домашнее преимущество как множитель к лямбде хозяев (для хоккея — из конфига).
            lh *= 1.0 + adv
            la *= max(0.5, 1.0 - adv / 2.0)

        lh = _clamp(lh, 0.05, 8.0)
        la = _clamp(la, 0.05, 8.0)

        matrix = score_matrix(lh, la)
        result = outcomes_from_matrix(matrix)

        theoretical_sigma = math.sqrt(lh + la)
        total_sigma = historical_sigma or theoretical_sigma * DEFAULT_SIGMA_INFLATION
        total_sigma = max(total_sigma, theoretical_sigma)
        margin_sigma = max(result["matrix_sigma_margin"], theoretical_sigma * 1.05)

        notes = [
            f"λ_home={lh:.2f}, λ_away={la:.2f}",
            f"xG-выборка: {home_stats.matches}/{away_stats.matches} матчей",
        ]
        if weak:
            notes.append("мало матчей в выборке → data_quality=weak")
        elif sample < form_window:
            notes.append(f"выборка меньше окна формы ({form_window} матчей) — прогноз менее устойчив")
        if historical_sigma:
            notes.append(f"total_sigma из истории калибровки: {historical_sigma:.2f}")

        return StatPrediction(
            model=self.name,
            prob_home=result["prob_home"],
            prob_draw=result["prob_draw"],
            prob_away=result["prob_away"],
            expected_total=result["expected_total"],
            total_sigma=total_sigma,
            expected_margin=result["expected_margin"],
            margin_sigma=margin_sigma,
            sample_size=sample,
            data_quality="weak" if weak else "ok",
            notes=notes,
            extra={
                "lambda_home": round(lh, 3),
                "lambda_away": round(la, 3),
                "league_avg_home": league_avg_home,
                "league_avg_away": league_avg_away,
            },
        )

    # Удобный синхронный вариант для тестов/скриптов
    def predict_sync(self, *args: Any, **kwargs: Any) -> StatPrediction | None:
        import asyncio

        return asyncio.get_event_loop().run_until_complete(self.predict(*args, **kwargs))
