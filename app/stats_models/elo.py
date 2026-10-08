"""Elo-модель для киберспорта (Dota 2 и CS2), включая ОТДЕЛЬНЫЙ рейтинг на карту CS2.

Правила из ТЗ:
  * старт 1500, K=32, обновление после каждого завершённого матча;
  * p_stat = 1 / (1 + 10^((elo_away − elo_home)/400));
  * для CS2 — отдельный рейтинг на каждую пару (команда, карта): классический
    «map Elo» лучше предсказывает исход на Ancient/Nuke/Mirage, чем общий рейтинг.

Домашнее преимущество в киберспорте — не «дом», а преимущество стороны/пика
(first pick / side advantage), поэтому по умолчанию 0; при необходимости
задаётся settings.elo_home_advantage (в пунктах Elo).

Ожидаемая разница (expected_margin) для CS2 — в раундах, для Dota 2 — в «условных
очках»: margin = (p − 0.5) × 2 × elo_margin_scale, sigma = elo_margin_sigma.
Это честная эвристика (у Elo нет распределения по раундам), она помечена в notes,
а арбитр может её скорректировать.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import EloRating
from app.stats_models.base import StatModel, StatPrediction


def expected_score(elo_home: float, elo_away: float, home_advantage: float = 0.0) -> float:
    """p = 1 / (1 + 10^((elo_away − elo_home − home_advantage)/400))."""
    diff = (elo_away - (elo_home + home_advantage)) / 400.0
    return 1.0 / (1.0 + 10.0**diff)


def update_rating(elo: float, actual: float, expected: float, k: float | None = None) -> float:
    """Новый рейтинг: elo + K × (actual − expected)."""
    k = settings.elo_k if k is None else k
    return elo + k * (actual - expected)


def elo_from_margin(margin: float, scale: float = 8.0) -> float:
    """Обновление Elo с учётом разницы (используется для CS2: margin = round diff).

    Возвращает «дробный» результат в диапазоне (0, 1): 0.5 + margin / (2 × scale).
    Так крупные победы дают больше очков — как в рейтингах с margin-of-victory.
    """
    return max(0.0, min(1.0, 0.5 + margin / (2.0 * scale)))


class EloModel(StatModel):
    name = "elo"
    sport = "esports"

    async def predict(  # type: ignore[override]
        self,
        elo_home: float,
        elo_away: float,
        map_name: str | None = None,
        home_advantage: float | None = None,
        margin_scale: float | None = None,
        margin_sigma: float | None = None,
        matches_home: int = 0,
        matches_away: int = 0,
    ) -> StatPrediction:
        advantage = settings.elo_home_advantage if home_advantage is None else home_advantage
        scale = margin_scale or settings.elo_margin_scale
        sigma = margin_sigma or settings.elo_margin_sigma
        prob_home = expected_score(elo_home, elo_away, advantage)
        prob_away = 1.0 - prob_home
        expected_margin = (prob_home - 0.5) * 2.0 * scale

        notes = [
            f"Elo {elo_home:.0f} vs {elo_away:.0f}"
            + (f" (карта {map_name})" if map_name else " (общий рейтинг)"),
            "expected_margin — эвристика Elo (не из распределения раундов)",
        ]
        weak = min(matches_home, matches_away) < settings.min_recent_matches
        if weak:
            notes.append("мало матчей в Elo-истории → data_quality=weak")

        return StatPrediction(
            model=f"{self.name}" + (f"_{map_name.lower()}" if map_name else ""),
            prob_home=prob_home,
            prob_draw=None,
            prob_away=prob_away,
            # Для киберспорта «тотал» — это тотал карт/раундов; у нас нет надёжной модели,
            # поэтому отдаём нейтральную оценку и явно помечаем это в extra.
            expected_total=scale * 1.5,
            total_sigma=max(sigma, 1.0),
            expected_margin=expected_margin,
            margin_sigma=sigma,
            sample_size=min(matches_home, matches_away),
            data_quality="weak" if weak else "ok",
            notes=notes,
            extra={
                "elo_home": round(elo_home, 1),
                "elo_away": round(elo_away, 1),
                "map_name": map_name,
                "home_advantage": advantage,
                "margin_scale": scale,
                "expected_total_reliable": False,
            },
        )


# --------------------------------------------------------------------------- #
# Работа с таблицей elo_ratings
# --------------------------------------------------------------------------- #
async def get_elo(
    session: AsyncSession, sport_id: int, team_id: int, map_name: str | None = None
) -> tuple[float, int]:
    """Возвращает (elo, matches_played). Если записи нет — стартовый рейтинг."""
    row = (
        await session.execute(
            select(EloRating).where(
                EloRating.sport_id == sport_id,
                EloRating.team_id == team_id,
                EloRating.map_name.is_(None) if map_name is None else EloRating.map_name == map_name,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return settings.elo_start, 0
    return row.elo, row.matches_played


async def set_elo(
    session: AsyncSession,
    sport_id: int,
    team_id: int,
    elo: float,
    map_name: str | None = None,
    matches_played: int | None = None,
) -> EloRating:
    row = (
        await session.execute(
            select(EloRating).where(
                EloRating.sport_id == sport_id,
                EloRating.team_id == team_id,
                EloRating.map_name.is_(None) if map_name is None else EloRating.map_name == map_name,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = EloRating(sport_id=sport_id, team_id=team_id, map_name=map_name, elo=elo, matches_played=matches_played or 0)
        session.add(row)
    else:
        row.elo = elo
        if matches_played is not None:
            row.matches_played = matches_played
    await session.flush()
    return row


async def apply_match_result(
    session: AsyncSession,
    sport_id: int,
    home_team_id: int,
    away_team_id: int,
    home_won: bool,
    map_name: str | None = None,
    margin: float | None = None,
    k: float | None = None,
) -> tuple[float, float]:
    """Обновляет Elo обеих команд после завершённого матча.

    Для CS2 вызывайте с map_name=<карта>, чтобы обновить map-Elo; при margin
    (разница раундов) используется margin-of-victory.
    """
    home_elo, home_matches = await get_elo(session, sport_id, home_team_id, map_name)
    away_elo, away_matches = await get_elo(session, sport_id, away_team_id, map_name)
    expected_home = expected_score(home_elo, away_elo, settings.elo_home_advantage)

    if margin is None:
        actual_home = 1.0 if home_won else 0.0
    else:
        actual_home = elo_from_margin(margin if home_won else -margin)
        # Страховка: победитель не может получить меньше 0.5, проигравший — больше 0.5.
        actual_home = max(actual_home, 0.5) if home_won else min(actual_home, 0.5)

    new_home = update_rating(home_elo, actual_home, expected_home, k)
    new_away = update_rating(away_elo, 1.0 - actual_home, 1.0 - expected_home, k)

    await set_elo(session, sport_id, home_team_id, new_home, map_name, home_matches + 1)
    await set_elo(session, sport_id, away_team_id, new_away, map_name, away_matches + 1)
    logger.info(
        "elo: {} vs {} (карта {}) → {:.1f} / {:.1f} (p_home={:.3f}, actual={:.2f})",
        home_team_id, away_team_id, map_name or "общий", new_home, new_away, expected_home, actual_home,
    )
    return new_home, new_away
