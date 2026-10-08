"""Обучающий контур (Модуль 10): калибровка лиг и веса ансамбля.

Что именно обучается на данных (LLM не дообучается — это принципиально из ТЗ):

  1. КАЛИБРОВКА (league_calibration). Для каждой связки (sport, league, market)
     считаем, как факт соотносится с прогнозом:
        expected = средняя прогнозная вероятность по закрытым сигналам,
        actual   = доля выигранных,
        calibration = clamp(actual / expected, 0.75, 1.15).
     Нужно ≥ CALIBRATION_MIN_SAMPLE (10) сигналов за CALIBRATION_WINDOW_DAYS (60 дней),
     иначе коэффициент 1.0 (нет данных — не выдумываем). Множитель применяется
     к confidence-баллу в pipeline/confidence.py.

  2. ВЕСА АНСАМБЛЯ (ensemble_weights). По последним BRIER_WINDOW (100) закрытым
     сигналам считаем Brier score отдельно для LLM-прогноза и для стат-модели:
        Brier = mean((p_selection − факт)^2), факт = 1 при выигрыше, 0 при проигрыше.
     w_llm = brier_stat / (brier_llm + brier_stat), w_stat = 1 − w_llm,
     сжатие к [0.3, 0.7] (см. config). Меньше 20 закрытых сигналов → оставляем 0.6/0.4.

  3. СИГМА ТОТАЛОВ (необязательно, используется как historical_sigma для Пуассона):
     стандартное отклонение фактического тотала по закрытым матчам лиги.

Запуск: ежедневно в 04:00 МСК (см. scheduler.py → run_daily_learning).
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import (
    Analysis,
    EnsembleWeight,
    LeagueCalibration,
    Match as MatchRow,
    Market,
    Signal,
    SignalStatus,
    Sport,
    StatPrediction as StatPredictionRow,
)
from app.pipeline.value_engine import MarketProbabilities

MIN_WEIGHT_SAMPLE = 20  # меньше — веса не трогаем (шум)


# --------------------------------------------------------------------------- #
# 1. Калибровка лиг
# --------------------------------------------------------------------------- #
async def recalibrate_leagues(session: AsyncSession) -> list[dict[str, Any]]:
    """Пересчитывает league_calibration. Возвращает список обновлённых записей."""
    edge = datetime.now(timezone.utc) - timedelta(days=settings.calibration_window_days)
    rows = (
        await session.execute(
            select(
                Sport.code,
                MatchRow.league,
                Signal.market,
                func.count(Signal.id),
                func.sum(case((Signal.status == SignalStatus.WON, 1), else_=0)),
                func.avg(Signal.prob_final),
            )
            .join(MatchRow, MatchRow.id == Signal.match_id)
            .join(Sport, Sport.id == MatchRow.sport_id)
            .where(
                Signal.status.in_((SignalStatus.WON, SignalStatus.LOST)),
                Signal.settled_at.is_not(None),
                Signal.settled_at >= edge,
            )
            .group_by(Sport.code, MatchRow.league, Signal.market)
        )
    ).all()

    updated: list[dict[str, Any]] = []
    for sport_code, league, market, sample, wins, expected_prob in rows:
        sample = int(sample or 0)
        wins = int(wins or 0)
        expected = float(expected_prob or 0.0)
        if sample < settings.calibration_min_sample or expected <= 0.0:
            continue
        actual = wins / sample
        factor = actual / expected
        factor = max(settings.calibration_clamp_min, min(settings.calibration_clamp_max, factor))
        row = (
            await session.execute(
                select(LeagueCalibration).where(
                    LeagueCalibration.sport == sport_code,
                    LeagueCalibration.league == league[:160],
                    LeagueCalibration.market == market,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = LeagueCalibration(sport=sport_code, league=league[:160], market=market)
            session.add(row)
        previous = row.calibration
        row.sample_size = sample
        row.calibration = round(factor, 4)
        row.updated_at = datetime.now(timezone.utc)
        updated.append(
            {
                "sport": sport_code,
                "league": league,
                "market": market,
                "sample": sample,
                "expected": round(expected, 4),
                "actual": round(actual, 4),
                "calibration": row.calibration,
                "previous": previous,
            }
        )
        if previous is None or abs(float(previous) - row.calibration) > 0.01:
            logger.info(
                "learning: калибровка {} / {} / {} = {:.3f} (было {:.3f}, ожидание {:.3f}, факт {:.3f}, n={})",
                sport_code, league, market, row.calibration, float(previous or 1.0), expected, actual, sample,
            )

    await session.flush()
    logger.info("learning: обновлено калибровок — {}", len(updated))
    return updated


async def get_calibration(session: AsyncSession, sport_code: str, league: str, market: str | None = None) -> float | dict[str, float]:
    """Коэффициент калибровки: число (если market задан) или словарь {market: k} для лиги."""
    query = select(LeagueCalibration.market, LeagueCalibration.calibration).where(
        LeagueCalibration.sport == sport_code, LeagueCalibration.league == league[:160]
    )
    if market:
        query = query.where(LeagueCalibration.market == market)
    rows = (await session.execute(query)).all()
    if market:
        return float(rows[0][1]) if rows else 1.0
    return {market_: float(value) for market_, value in rows} or {}


# --------------------------------------------------------------------------- #
# 2. Веса ансамбля (Brier score)
# --------------------------------------------------------------------------- #
def brier_score(predictions: list[float], outcomes: list[int]) -> float | None:
    """Brier score = среднее (p − факт)^2. None при пустой выборке (чистая функция, тестируется)."""
    if not predictions or len(predictions) != len(outcomes):
        return None
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / len(predictions)


def weights_from_brier(brier_llm: float, brier_stat: float) -> tuple[float, float]:
    """Веса из Brier: лучше (меньше) — больше вес. Сжатие к [0.3, 0.7]."""
    total = brier_llm + brier_stat
    if total <= 0:
        return settings.w_llm_init, settings.w_stat_init
    w_llm = brier_stat / total
    w_llm = max(settings.weight_clamp_min, min(settings.weight_clamp_max, w_llm))
    return round(w_llm, 4), round(1.0 - w_llm, 4)


async def _component_probability(
    session: AsyncSession, signal: Signal, match: MatchRow
) -> tuple[float | None, float | None]:
    """Вероятности выбранного исхода по LLM и по стат-модели на момент сигнала."""
    analysis = (
        await session.execute(
            select(Analysis).where(Analysis.match_id == match.id, Analysis.level == 2).order_by(Analysis.created_at.desc())
        )
    ).scalars().first()
    stat_row = (
        await session.execute(
            select(StatPredictionRow).where(StatPredictionRow.match_id == match.id).order_by(StatPredictionRow.created_at.desc())
        )
    ).scalars().first()

    llm_probability = stat_probability = None
    if analysis and isinstance(analysis.parsed, dict):
        llm_payload = analysis.parsed.get("llm") or analysis.parsed
        probabilities = _probabilities_from_payload(llm_payload)
        if probabilities:
            llm_probability = probabilities.probability_for(signal.market, signal.selection, signal.line)
    if stat_row is not None and isinstance(stat_row.parsed, dict):
        # В БД стат-прогноз хранится как JSON (StatPrediction.parsed), а не колонками.
        probabilities = _probabilities_from_payload(stat_row.parsed)
        if probabilities:
            stat_probability = probabilities.probability_for(signal.market, signal.selection, signal.line)
    return llm_probability, stat_probability


def _probabilities_from_payload(payload: dict[str, Any]) -> MarketProbabilities | None:
    """LLM-JSON аналитика → MarketProbabilities (если он ещё не сохранён как MarketProbabilities)."""
    if not isinstance(payload, dict):
        return None
    if "prob_home" not in payload or "prob_away" not in payload:
        return None
    try:
        return MarketProbabilities(
            prob_home=float(payload["prob_home"]),
            prob_away=float(payload["prob_away"]),
            prob_draw=float(payload["prob_draw"]) if payload.get("prob_draw") is not None else None,
            expected_total=float(payload.get("expected_total") or 0.0),
            total_sigma=float(payload.get("total_sigma") or 1.0),
            expected_margin=float(payload.get("expected_margin") or 0.0),
            margin_sigma=float(payload.get("margin_sigma") or 1.0),
        )
    except (TypeError, ValueError):
        return None


async def update_ensemble_weights(session: AsyncSession) -> list[dict[str, Any]]:
    """Пересчитывает веса ансамбля по спорту (Brier score, последние BRIER_WINDOW сигналов)."""
    edge = datetime.now(timezone.utc) - timedelta(days=settings.calibration_window_days)
    signals = (
        await session.execute(
            select(Signal, MatchRow)
            .join(MatchRow, MatchRow.id == Signal.match_id)
            .where(
                Signal.status.in_((SignalStatus.WON, SignalStatus.LOST)),
                Signal.settled_at.is_not(None),
                Signal.settled_at >= edge,
            )
            .order_by(Signal.settled_at.desc())
        )
    ).all()
    sports = dict((await session.execute(select(Sport.id, Sport.code))).all())

    per_sport: dict[str, dict[str, list]] = {}
    for signal, match in signals:
        sport_code = sports.get(match.sport_id)
        if not sport_code:
            continue
        bucket = per_sport.setdefault(sport_code, {"llm": [], "stat": [], "actual": []})
        if len(bucket["actual"]) >= settings.brier_window:
            continue
        llm_probability, stat_probability = await _component_probability(session, signal, match)
        if llm_probability is None:
            continue
        outcome = 1 if signal.status == SignalStatus.WON else 0
        bucket["llm"].append(float(llm_probability))
        bucket["actual"].append(outcome)
        if stat_probability is not None:
            bucket["stat"].append(float(stat_probability))
        else:
            bucket["stat"].append(0.5)  # нет стат-модели (теннис/MMA) → нейтральный прогноз

    results: list[dict[str, Any]] = []
    for sport_code, bucket in per_sport.items():
        sample = len(bucket["actual"])
        row = (
            await session.execute(select(EnsembleWeight).where(EnsembleWeight.sport == sport_code))
        ).scalar_one_or_none()
        if row is None:
            row = EnsembleWeight(sport=sport_code)
            session.add(row)
            await session.flush()
        if sample < MIN_WEIGHT_SAMPLE:
            results.append({"sport": sport_code, "sample": sample, "w_llm": float(row.w_llm), "w_stat": float(row.w_stat), "changed": False})
            logger.debug("learning: {} — только {} сигналов, веса ансамбля не меняю", sport_code, sample)
            continue
        brier_llm = brier_score(bucket["llm"], bucket["actual"])
        brier_stat = brier_score(bucket["stat"], bucket["actual"])
        if brier_llm is None or brier_stat is None:
            continue
        w_llm, w_stat = weights_from_brier(brier_llm, brier_stat)
        changed = abs(w_llm - float(row.w_llm)) > 0.005
        row.w_llm, row.w_stat = w_llm, w_stat
        row.brier_llm, row.brier_stat = round(brier_llm, 4), round(brier_stat, 4)
        row.sample_size = sample
        row.updated_at = datetime.now(timezone.utc)
        results.append(
            {
                "sport": sport_code,
                "sample": sample,
                "brier_llm": round(brier_llm, 4),
                "brier_stat": round(brier_stat, 4),
                "w_llm": w_llm,
                "w_stat": w_stat,
                "changed": changed,
            }
        )
        if changed:
            logger.info(
                "learning: веса ансамбля {} → LLM {:.2f} / стат {:.2f} (Brier {:.3f} vs {:.3f}, n={})",
                sport_code, w_llm, w_stat, brier_llm, brier_stat, sample,
            )

    await session.flush()
    return results


async def get_ensemble_weights(session: AsyncSession, sport_code: str) -> tuple[float, float]:
    """Текущие веса (w_llm, w_stat); по умолчанию из конфига 0.6 / 0.4."""
    row = (
        await session.execute(select(EnsembleWeight).where(EnsembleWeight.sport == sport_code))
    ).scalar_one_or_none()
    if row is None:
        return settings.w_llm_init, settings.w_stat_init
    return float(row.w_llm), float(row.w_stat)


# --------------------------------------------------------------------------- #
# 3. Сигма тоталов из истории
# --------------------------------------------------------------------------- #
async def sigma_from_history(session: AsyncSession, sport_code: str, league: str | None = None, days: int = 120) -> float | None:
    """σ фактического тотала по закрытым матчам (для калибровки Пуассона).

    Используется аналитиком как historical_sigma: если реальный разброс тоталов выше
    модельного, вероятности тоталов становятся менее «уверенными».
    """
    edge = datetime.now(timezone.utc) - timedelta(days=days)
    query = (
        select(MatchRow.result_home, MatchRow.result_away)
        .join(Sport, Sport.id == MatchRow.sport_id)
        .where(
            Sport.code == sport_code,
            MatchRow.status == "finished",
            MatchRow.result_home.is_not(None),
            MatchRow.result_away.is_not(None),
            MatchRow.starts_at >= edge,
        )
        .limit(500)
    )
    if league:
        query = query.where(MatchRow.league == league[:160])
    rows = (await session.execute(query)).all()
    totals = [float(home) + float(away) for home, away in rows if home is not None and away is not None]
    if len(totals) < 10:
        return None
    sigma = statistics.pstdev(totals)
    logger.debug(
        "learning: σ тотала {} {} = {:.2f} по {} матчам", sport_code, league or "все лиги", sigma, len(totals)
    )
    return round(sigma, 3)


# --------------------------------------------------------------------------- #
# Ежедневный прогон
# --------------------------------------------------------------------------- #
async def run_daily_learning(session: AsyncSession) -> dict[str, Any]:
    """Полный цикл обучения: калибровка → веса ансамбля. Вызывается в 04:00 МСК."""
    logger.info("learning: запуск ежедневного обучения")
    calibrations = await recalibrate_leagues(session)
    weights = await update_ensemble_weights(session)
    await session.commit()
    summary = {
        "calibrations": len(calibrations),
        "weights": weights,
        "changed_calibrations": sum(1 for item in calibrations if item["previous"] is not None and abs(float(item["previous"]) - item["calibration"]) > 0.01),
    }
    logger.info("learning: готово — {}", summary)
    return summary
