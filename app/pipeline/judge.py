"""Ур.3 — JUDGE_MODEL (Claude Sonnet): финальный арбитраж сигналов.

Вызывается ТОЛЬКО для сигналов, прошедших Value Engine (edge и score выше порогов).

Пакет для арбитра содержит:
  * все данные матча (составы, травмы, форма, H2H, плотность календаря);
  * JSON аналитика включая key_factors и risk_notes;
  * StatPrediction стат-модели;
  * посчитанные edge и confidence_score, implied и лучший кэф;
  * снимки кэфов с таймстампами (движение линии);
  * активные league_insights лиги;
  * (для лайва) игровую ситуацию и движение кэфа.

Вердикты: confirm → сигнал в рассылку; reject → status=rejected + judge_reason;
adjust → Value Engine ПЕРЕСЧИТЫВАЕТ edge/score по скорректированным вероятностям
(и, если пороги не пройдены, сигнал отклоняется).

Все вердикты пишутся в таблицу judge_verdicts (включая timeout, чтобы было видно,
когда арбитр не успел). JUDGE_ENABLED=false — аварийный режим: сигнал идёт в рассылку
без арбитра (осознанное решение оператора, логируется).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import JudgeVerdict as JudgeVerdictRow
from app.db.models import Odd as OddRow
from app.db.models import Signal, SignalStatus
from app.llm_client import LLMError, LLMTimeout, get_llm_client
from app.pipeline.llm_prompts import JUDGE_LIVE_SYSTEM, JUDGE_SYSTEM, build_judge_user_prompt
from app.pipeline.value_engine import (
    MarketProbabilities,
    recompute_after_judge,
)
from app.sources.odds_aggregator import AggregatedOutcome
from app.stats_models.base import StatPrediction


class JudgeResult(BaseModel):
    """Ответ арбитра (строгая валидация)."""

    verdict: Literal["confirm", "reject", "adjust"]
    adjusted_prob_home: float | None = None
    adjusted_prob_draw: float | None = None
    adjusted_prob_away: float | None = None
    adjusted_expected_total: float | None = None
    adjusted_margin: float | None = None
    confidence_delta: float = 0.0
    red_flags: list[str] = Field(default_factory=list)
    reason: str = ""

    @model_validator(mode="after")
    def _validate(self) -> JudgeResult:
        self.confidence_delta = max(-0.2, min(0.2, self.confidence_delta))
        if self.verdict in ("reject", "adjust") and not self.red_flags:
            raise ValueError("red_flags обязательны при verdict=reject/adjust")
        if self.verdict == "adjust":
            if self.adjusted_prob_home is None or self.adjusted_prob_away is None:
                raise ValueError("при adjust нужны adjusted_prob_home и adjusted_prob_away")
            total = self.adjusted_prob_home + (self.adjusted_prob_draw or 0.0) + self.adjusted_prob_away
            if abs(total - 1.0) > 0.02:
                raise ValueError(f"сумма скорректированных вероятностей {total:.4f} отличается от 1 более чем на 0.02")
        return self


async def build_judge_package(
    session: AsyncSession,
    signal: Signal,
    context: dict[str, Any],
    stat_prediction: StatPrediction | None,
    implied: float,
    *,
    is_live: bool = False,
) -> dict[str, Any]:
    """Собирает компактный (для лайва) или полный пакет для арбитра."""
    package: dict[str, Any] = {
        "signal": {
            "market": signal.market,
            "selection": signal.selection,
            "line": signal.line,
            "odds": signal.odds,
            "odds_source": signal.odds_source,
            "prob_final": signal.prob_final,
            "prob_implied": implied,
            "edge": signal.edge,
            "confidence_score": signal.confidence_score,
            "stake_pct": signal.stake_pct,
            "is_live": is_live,
        },
        "match": context.get("match", {}),
        "analyst": {
            "probabilities": context.get("analyst_probabilities"),
            "key_factors": signal.key_factors,
            "risk_notes": signal.risk_notes,
            "reasoning": signal.reasoning,
        },
        "stat_model": stat_prediction.to_prompt_json() if stat_prediction else None,
        "league_insights": context.get("league_insights"),
    }
    # LIVE_LLM_JUDGE_SHORT_PACKAGE=false — отдавать арбитру полный пакет и в лайве
    # (дороже и медленнее; по ТЗ в лайве пакет укорочен, поэтому дефолт true).
    if not is_live or not settings.live_llm_judge_short_package:
        package["data"] = {
            "odds_lines": context.get("odds_lines"),
            "form": context.get("form"),
            "injuries": context.get("injuries"),
            "lineups": context.get("lineups"),
            "h2h": context.get("h2h"),
            "schedule_density": context.get("schedule_density"),
            "news": context.get("news"),
            "suspicious_lines": context.get("suspicious_lines"),
        }
    if is_live:
        package["live_info"] = context.get("live_info")
        package["odds_movement"] = context.get("odds_movement")
    package["odds_snapshots"] = await odds_snapshots(session, signal.match_id, limit=12 if is_live else 8)
    return package


async def odds_snapshots(session: AsyncSession, match_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """Снимки кэфов с таймстампами (движение линии) — для арбитра."""
    rows = (
        await session.execute(
            select(OddRow.market, OddRow.selection, OddRow.line, OddRow.price, OddRow.source, OddRow.captured_at)
            .where(OddRow.match_id == match_id)
            .order_by(desc(OddRow.captured_at))
            .limit(limit)
        )
    ).all()
    return [
        {
            "market": market,
            "selection": selection,
            "line": line,
            "price": price,
            "source": source,
            "at": captured_at.isoformat() if captured_at else None,
        }
        for market, selection, line, price, source, captured_at in rows
    ]


async def request_verdict(package: dict[str, Any], match_id: int | str | None, *, is_live: bool = False) -> JudgeResult | None:
    """Запрос к Claude. None — таймаут/ошибка (сигнал пойдёт без арбитра, см. ТЗ)."""
    client = get_llm_client()
    if not client.available:
        logger.warning("judge: OPENROUTER_API_KEY не задан — арбитраж пропущен (режим без арбитра)")
        return None
    system_prompt = JUDGE_LIVE_SYSTEM if is_live else JUDGE_SYSTEM
    try:
        result, _raw = await asyncio.wait_for(
            client.complete_model(
                JudgeResult,
                model=settings.judge_model,
                system_prompt=system_prompt,
                user_prompt=build_judge_user_prompt(package),
                max_tokens=settings.llm_max_tokens_judge,
                temperature=settings.llm_temperature_judge,
                timeout=settings.judge_timeout_sec,
                label="judge",
                match_id=match_id,
            ),
            timeout=settings.judge_timeout_sec + 5,
        )
        return result
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning("judge: match_id={} — таймаут {}s, решение без арбитра", match_id, settings.judge_timeout_sec)
        return None
    except LLMTimeout as exc:
        logger.warning("judge: match_id={} — LLM-таймаут ({}), решение без арбитра", match_id, exc)
        return None
    except LLMError as exc:
        logger.warning("judge: match_id={} — ошибка LLM ({}), решение без арбитра", match_id, exc)
        return None


async def judge_signal(
    session: AsyncSession,
    signal: Signal,
    context: dict[str, Any],
    stat_prediction: StatPrediction | None,
    outcomes: list[AggregatedOutcome],
    *,
    is_live: bool = False,
) -> tuple[str, JudgeResult | None, float]:
    """Полный цикл арбитража одного сигнала.

    Возвращает (итоговый статус, вердикт|None, score после корректировок).
    """
    score = signal.confidence_score
    if not settings.judge_enabled:
        logger.info("judge: JUDGE_ENABLED=false — сигнал {} идёт в рассылку без арбитра", signal.id)
        verdict_row = JudgeVerdictRow(
            signal_id=signal.id,
            match_id=signal.match_id,
            model=settings.judge_model,
            verdict="skipped",
            reason="JUDGE_ENABLED=false",
            confidence_delta=0.0,
            red_flags=[],
        )
        session.add(verdict_row)
        await session.flush()
        return SignalStatus.CONFIRMED, None, score

    implied = signal.prob_implied
    package = await build_judge_package(session, signal, context, stat_prediction, implied, is_live=is_live)
    result = await request_verdict(package, signal.match_id, is_live=is_live)

    verdict_row = JudgeVerdictRow(
        signal_id=signal.id,
        match_id=signal.match_id,
        model=settings.judge_model,
        raw_json=json.dumps(package, ensure_ascii=False)[:20000],
        verdict=result.verdict if result else "timeout",
        reason=result.reason if result else "арбитр не ответил в отведённое время",
        confidence_delta=result.confidence_delta if result else 0.0,
        red_flags=result.red_flags if result else [],
    )
    session.add(verdict_row)

    if result is None:
        # ТЗ: при таймауте — решение без арбитра. Сигнал подтверждаем, но помечаем в judge_reason.
        signal.judge_reason = "арбитр не ответил (timeout) — решение принято без него"
        await session.flush()
        return SignalStatus.CONFIRMED, None, score

    logger.info(
        "judge: signal_id={} match_id={} → {} (delta {:+.2f}) {}",
        signal.id, signal.match_id, result.verdict, result.confidence_delta, result.reason[:160],
    )

    if result.verdict == "confirm":
        signal.judge_reason = f"Claude: подтверждено. {result.reason}"[:500]
        score = _apply_delta(score, result.confidence_delta)
        signal.confidence_score = score
        await session.flush()
        return SignalStatus.CONFIRMED, result, score

    if result.verdict == "reject":
        signal.status = SignalStatus.REJECTED
        signal.judge_reason = f"Claude отклонил: {result.reason}; red_flags: {'; '.join(result.red_flags)}"[:900]
        await session.flush()
        return SignalStatus.REJECTED, result, score

    # adjust: пересчитываем edge по скорректированным вероятностям
    adjusted = MarketProbabilities(
        prob_home=result.adjusted_prob_home or 0.0,
        prob_away=result.adjusted_prob_away or 0.0,
        prob_draw=result.adjusted_prob_draw,
        expected_total=result.adjusted_expected_total or (stat_prediction.expected_total if stat_prediction else 0.0),
        total_sigma=stat_prediction.total_sigma if stat_prediction else 1.0,
        expected_margin=result.adjusted_margin
        if result.adjusted_margin is not None
        else (stat_prediction.expected_margin if stat_prediction else 0.0),
        margin_sigma=stat_prediction.margin_sigma if stat_prediction else 1.0,
        llm_confidence=min(1.0, max(0.0, (context.get("llm_confidence") or 0.5) + result.confidence_delta)),
        key_factors=signal.key_factors or [],
        risk_notes=(signal.risk_notes or []) + [f"арбитр: {flag}" for flag in result.red_flags],
        reasoning=f"скорректировано арбитром: {result.reason}",
        source="judge_adjusted",
        adjusted_by_judge=True,
    )
    target = _find_outcome(outcomes, signal.market, signal.selection, signal.line)
    if target is None:
        signal.status = SignalStatus.REJECTED
        signal.judge_reason = "арбитр скорректировал, но исход не найден в линии — отклонено"
        await session.flush()
        return SignalStatus.REJECTED, result, score

    candidate, new_score = recompute_after_judge(
        adjusted, target, outcomes, confidence_delta=result.confidence_delta, is_live=is_live
    )
    if candidate is None:
        signal.status = SignalStatus.REJECTED
        signal.judge_reason = (
            f"после корректировки арбитра пороги не пройдены (edge/score): {result.reason}"
        )[:500]
        await session.flush()
        logger.info("judge: signal_id={} отклонён после корректировки", signal.id)
        return SignalStatus.REJECTED, result, new_score

    signal.prob_final = candidate.prob_final
    signal.prob_implied = candidate.prob_implied
    signal.edge = candidate.edge
    signal.confidence_score = new_score
    signal.status = SignalStatus.CONFIRMED
    signal.judge_reason = f"Claude скорректировал: {result.reason}"[:500]
    await session.flush()
    logger.info(
        "judge: signal_id={} скорректирован → edge {:+.2%}, score {:.1f}",
        signal.id, candidate.edge, new_score,
    )
    return SignalStatus.CONFIRMED, result, new_score


def _apply_delta(score: float, delta: float) -> float:
    if not delta:
        return score
    return round(max(settings.conf_clamp_min, min(settings.conf_clamp_max, score + 100.0 * delta)), 1)


def _find_outcome(
    outcomes: list[AggregatedOutcome], market: str, selection: str, line: float | None
) -> AggregatedOutcome | None:
    for outcome in outcomes:
        if outcome.market != market or outcome.selection != selection:
            continue
        if line is None and outcome.line is None:
            return outcome
        if line is not None and outcome.line is not None and abs(outcome.line - line) < 1e-6:
            return outcome
    return None
