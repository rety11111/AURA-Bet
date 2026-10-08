"""Баскетбольная модель: pace + ORtg/DRtg → тотал и разница.

Формулы (соглашение: ORtg/DRtg — очки на 100 владений):
    pace          = (pace_home + pace_away) / 2                 (владений за 48 минут)
    eff_home      = (ORtg_home + DRtg_away) / 2                 («эффективность» хозяев в этой паре)
    eff_away      = (ORtg_away + DRtg_home) / 2
    expected_total  = pace / 100 × (eff_home + eff_away)
    expected_margin = pace / 100 × (eff_home − eff_away) + HOME_ADVANTAGE (≈2.5 очка)
    prob_home       = Φ(expected_margin / margin_sigma)
    prob_away       = 1 − prob_home        (ничьих в баскетболе нет → prob_draw = None)

Дефолты: total_sigma ≈ 14.0 (из ТЗ), margin_sigma ≈ 12.0 (эмпирика NBA;
оба значения — в конфиге и переопределяются через env).

Если у команд нет ORtg/DRtg (free-tier источники часто их не дают), модель
переходит на оценку по набранным/пропущенным очкам и честно помечает
data_quality="weak".
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.config import settings
from app.sources.base import BasketballTeamStats
from app.stats_models.base import StatModel, StatPrediction, norm_cdf

LEAGUE_PACE = {"nba": 99.0, "euroleague": 75.0}
LEAGUE_ORTG = {"nba": 114.0, "euroleague": 108.0}


class BasketballModel(StatModel):
    name = "basketball"
    sport = "basketball"

    async def predict(  # type: ignore[override]
        self,
        home_stats: BasketballTeamStats | None,
        away_stats: BasketballTeamStats | None,
        league_key: str | None = None,
        home_advantage: float | None = None,
        total_sigma: float | None = None,
        historical_sigma: float | None = None,
    ) -> StatPrediction | None:
        if home_stats is None or away_stats is None:
            logger.debug("{}: нет статистики одной из команд — модель пропускает матч", self.name)
            return None

        key = (league_key or "nba").lower()
        default_pace = LEAGUE_PACE.get(key, 97.0)
        default_ortg = LEAGUE_ORTG.get(key, 112.0)

        pace = _blend(home_stats.pace, away_stats.pace, default_pace)
        home_ortg = home_stats.ortg if home_stats.ortg is not None else default_ortg
        away_ortg = away_stats.ortg if away_stats.ortg is not None else default_ortg
        home_drtg = home_stats.drtg if home_stats.drtg is not None else default_ortg
        away_drtg = away_stats.drtg if away_stats.drtg is not None else default_ortg

        weak = home_stats.ortg is None or away_stats.ortg is None or home_stats.games < settings.min_recent_matches

        eff_home = (home_ortg + away_drtg) / 2.0
        eff_away = (away_ortg + home_drtg) / 2.0
        advantage = settings.basketball_home_advantage if home_advantage is None else home_advantage

        expected_total = pace / 100.0 * (eff_home + eff_away)
        expected_margin = pace / 100.0 * (eff_home - eff_away) + advantage

        sigma_total = historical_sigma or total_sigma or settings.basketball_total_sigma
        sigma_margin = settings.basketball_margin_sigma
        prob_home = norm_cdf(0.0, -expected_margin, sigma_margin)  # P(margin > 0)
        prob_away = 1.0 - prob_home

        notes = [
            f"pace={pace:.1f}, eff_home={eff_home:.1f}, eff_away={eff_away:.1f}",
            f"домашнее преимущество={advantage:.1f} очка",
        ]
        if weak:
            notes.append("ORtg/DRtg неполные или мало игр → data_quality=weak (оценка по средним лиги)")

        return StatPrediction(
            model=self.name,
            prob_home=prob_home,
            prob_draw=None,
            prob_away=prob_away,
            expected_total=expected_total,
            total_sigma=sigma_total,
            expected_margin=expected_margin,
            margin_sigma=sigma_margin,
            sample_size=min(home_stats.games, away_stats.games),
            data_quality="weak" if weak else "ok",
            notes=notes,
            extra={
                "pace": round(pace, 2),
                "eff_home": round(eff_home, 2),
                "eff_away": round(eff_away, 2),
                "ortg_home": home_ortg,
                "ortg_away": away_ortg,
                "drtg_home": home_drtg,
                "drtg_away": away_drtg,
            },
        )


def _blend(a: float | None, b: float | None, default: float) -> float:
    values = [v for v in (a, b) if v is not None]
    if not values:
        return default
    return sum(values) / len(values)


def expected_total_from_ratings(pace: float, ortg: float, drtg: float) -> float:
    """Служебная формула для тестов: тотал двух команд с одинаковыми рейтингами."""
    return pace / 100.0 * (ortg + drtg)
