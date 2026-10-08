"""Воскресный самоанализ системы (Модуль 10, «self-review»).

Каждое воскресенье в 05:00 МСК (см. scheduler.py) сервис:

  1. Считает по каждой лиге за неделю: сколько сигналов, винрейт, ROI, средний
     прогнозный edge vs фактический, сколько сигналов скорректировал/отклонил арбитр,
     как часто ловились suspicious-линии.
  2. Отдаёт этот отчёт ANALYZER_MODEL с просьбой найти системные ошибки
     (например: «модель переоценивает тоталы в Серии А») и вернуть JSON по схеме
     SelfReviewResult (см. app/pipeline/llm_prompts.py).
  3. Сохраняет результат в `league_insights` (предыдущие помечает active=False).
     Эти инсайты автоматически подставляются в промпт аналитика и арбитра,
     то есть контур замкнут: ошибки недели → правила следующей недели.

Если LLM недоступен — отчёт сохраняется как есть (текстовая сводка), помеченный
`llm_ok=False`, чтобы контур не рассыпался.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import (
    JudgeVerdict,
    LeagueInsight,
    Match as MatchRow,
    Signal,
    SignalStatus,
    Sport,
)
from app.llm_client import LLMError, OpenRouterClient
from app.pipeline.llm_prompts import SELF_REVIEW_SYSTEM, build_self_review_user_prompt

REVIEW_WINDOW_DAYS = 7
MIN_LOSSES_FOR_EXAMPLE = 3


class InsightItem(BaseModel):
    """Один вывод по лиге: наблюдение → доказательство → правило для анализа."""

    insight: str = Field(description="Что обнаружено — конкретно и проверяемо")
    evidence: str = Field(description="На каких цифрах из отчёта основано")
    action: str = Field(description="Правило для промпта аналитика/арбитра (что делать иначе)")


class SelfReviewResult(BaseModel):
    """Схема ответа самоанализа (строгий JSON, см. llm_client)."""

    league: str
    summary: str
    insights: list[InsightItem] = Field(default_factory=list, max_length=8)
    watch_next_week: list[str] = Field(default_factory=list, max_length=6)
    llm_ok: bool = True


async def league_reports(session: AsyncSession, days: int = REVIEW_WINDOW_DAYS) -> list[dict[str, Any]]:
    """Агрегированная статистика по лигам за окно (вход для LLM-самоанализа)."""
    edge = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                Sport.code,
                MatchRow.league,
                func.count(Signal.id),
                func.sum(case((Signal.status == SignalStatus.WON, 1), else_=0)),
                func.sum(case((Signal.status == SignalStatus.LOST, 1), else_=0)),
                func.avg(Signal.prob_final),
                func.avg(Signal.edge),
                func.avg(Signal.confidence_score),
                func.sum(Signal.stake_pct),
                func.sum(case((Signal.is_live.is_(True), 1), else_=0)),
            )
            .join(MatchRow, MatchRow.id == Signal.match_id)
            .join(Sport, Sport.id == MatchRow.sport_id)
            .where(Signal.settled_at.is_not(None), Signal.settled_at >= edge)
            .group_by(Sport.code, MatchRow.league)
        )
    ).all()

    reports: list[dict[str, Any]] = []
    for (
        sport_code, league, total, wins, losses, avg_prob, avg_edge, avg_score, stake_sum, live_count,
    ) in rows:
        wins = int(wins or 0)
        losses = int(losses or 0)
        settled = wins + losses
        if settled == 0:
            continue
        report = {
            "sport": sport_code,
            "league": league,
            "signals": settled,
            "live_signals": int(live_count or 0),
            "won": wins,
            "lost": losses,
            "win_rate": round(wins / settled, 4),
            "avg_prob_final": round(float(avg_prob or 0.0), 4),
            "avg_edge": round(float(avg_edge or 0.0), 4),
            "avg_confidence": round(float(avg_score or 0.0), 1),
            "roi": None,
            "judge_stats": {},
            "losses": [],
        }
        # ROI считаем отдельным запросом (нужны odds/stake по каждой ставке)
        signals = (
            (
                await session.execute(
                    select(Signal.odds, Signal.stake_pct, Signal.status, Signal.market, Signal.selection, Signal.line, Signal.reasoning, Signal.match_id)
                    .join(MatchRow, MatchRow.id == Signal.match_id)
                    .join(Sport, Sport.id == MatchRow.sport_id)
                    .where(
                        Sport.code == sport_code,
                        MatchRow.league == league,
                        Signal.settled_at.is_not(None),
                        Signal.settled_at >= edge,
                        Signal.status.in_((SignalStatus.WON, SignalStatus.LOST)),
                    )
                )
            )
            .all()
        )
        profit = 0.0
        staked = 0.0
        for odds, stake, status, market, selection, line, reasoning, match_id in signals:
            stake = float(stake or 0.0)
            staked += stake
            if status == SignalStatus.WON:
                profit += stake * (float(odds or 0.0) - 1.0)
            else:
                profit -= stake
                if len(report["losses"]) < 8:
                    report["losses"].append(
                        {
                            "market": market,
                            "selection": selection,
                            "line": line,
                            "odds": round(float(odds or 0.0), 3),
                            "reasoning": (reasoning or "")[:220],
                        }
                    )
        report["roi"] = round(profit / staked, 4) if staked else 0.0
        report["stake_sum"] = round(staked, 2)

        # Вердикты арбитра по лиге (подтверждения/отклонения/корректировки)
        judge_rows = (
            await session.execute(
                select(JudgeVerdict.verdict, func.count(JudgeVerdict.id))
                .join(MatchRow, MatchRow.id == JudgeVerdict.match_id)
                .join(Sport, Sport.id == MatchRow.sport_id)
                .where(Sport.code == sport_code, MatchRow.league == league, JudgeVerdict.created_at >= edge)
                .group_by(JudgeVerdict.status)
            )
        ).all()
        report["judge_stats"] = {verdict: int(count) for verdict, count in judge_rows}
        reports.append(report)

    reports.sort(key=lambda item: (item["roi"], -item["signals"]))
    return reports


def _build_report(report: dict[str, Any]) -> str:
    """Человекочитаемая сводка для промпта (без «сырого» JSON — LLM так стабильнее)."""
    lines = [
        f"Лига: {report['league']} ({report['sport']})",
        f"Сигналов: {report['signals']} (лайв: {report['live_signals']}), "
        f"выиграно {report['won']} / проиграно {report['lost']} → винрейт {report['win_rate']:.1%}",
        f"ROI: {report['roi']:+.1%} при суммарной ставке {report['stake_sum']}% банка",
        f"Средний прогноз: p_final={report['avg_prob_final']:.3f}, edge={report['avg_edge']:+.2%}, "
        f"уверенность={report['avg_confidence']}/100",
    ]
    if report["judge_stats"]:
        lines.append(f"Вердикты арбитра: {report['judge_stats']}")
    losses = report.get("losses") or []
    if losses:
        lines.append("Примеры проигранных сигналов:")
        for loss in losses[:6]:
            lines.append(
                f"  • {loss['market']} {loss['selection']}"
                + (f" (линия {loss['line']})" if loss.get("line") is not None else "")
                + f" @ {loss['odds']} — обоснование: {loss['reasoning'] or 'нет'}"
            )
    return "\n".join(lines)


async def review_league(client: OpenRouterClient, report: dict[str, Any]) -> SelfReviewResult:
    """Один LLM-вызов самоанализа по лиге (с деградацией в текстовую сводку)."""
    payload = {**_build_report_payload(report), "human_summary": _build_report(report)}
    try:
        result, _raw = await client.complete_model(
            SelfReviewResult,
            model=settings.analyzer_model,
            system_prompt=SELF_REVIEW_SYSTEM,
            user_prompt=build_self_review_user_prompt(payload),
            max_tokens=settings.llm_max_tokens_analyzer,
            temperature=settings.llm_temperature_analyst,
            timeout=settings.llm_analyzer_timeout_sec,
            label="self_review",
        )
        result.league = report["league"]
        result.llm_ok = True
    except LLMError as exc:
        logger.warning("self_review: LLM не смог разобрать лигу {} ({})", report["league"], str(exc)[:200])
        result = SelfReviewResult(
            league=report["league"],
            summary=f"LLM недоступен, сохранена сырая сводка. {report['signals']} сигналов, ROI {report['roi']:+.1%}.",
            insights=[],
            watch_next_week=[f"Проверить вручную: ROI {report['roi']:+.1%} при винрейте {report['win_rate']:.1%}"],
            llm_ok=False,
        )
    return result


def _build_report_payload(report: dict[str, Any]) -> dict[str, Any]:
    """Отчёт лиги в JSON для промпта (без внутренних полей, которые не нужны модели)."""
    return {
        "league": report.get("league"),
        "sport": report.get("sport"),
        "signals": report.get("signals"),
        "live_signals": report.get("live_signals"),
        "won": report.get("won"),
        "lost": report.get("lost"),
        "win_rate": report.get("win_rate"),
        "roi": report.get("roi"),
        "avg_prob_final": report.get("avg_prob_final"),
        "avg_edge": report.get("avg_edge"),
        "avg_confidence": report.get("avg_confidence"),
        "judge_stats": report.get("judge_stats"),
        "judge_adjustments": report.get("judge_adjustments", []),
        "example_losses": report.get("losses", []),
        "note": (
            "roi — в единицах ставки (доля банка), win_rate — доля выигранных, "
            "avg_edge — средний прогнозный перевес. Анализируй системные ошибки, не отдельные матчи."
        ),
    }


async def _judge_adjustments(session: AsyncSession, sport_code: str, league: str, days: int) -> list[dict[str, Any]]:
    """Что арбитр корректировал в этой лиге (для промпта — «где мы ошибались дважды»)."""
    edge = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        await session.execute(
            select(JudgeVerdict.verdict, JudgeVerdict.red_flags, JudgeVerdict.reason, JudgeVerdict.confidence_delta)
            .join(MatchRow, MatchRow.id == JudgeVerdict.match_id)
            .join(Sport, Sport.id == MatchRow.sport_id)
            .where(
                Sport.code == sport_code,
                MatchRow.league == league,
                JudgeVerdict.created_at >= edge,
                JudgeVerdict.verdict != "confirm",
            )
            .limit(20)
        )
    ).all()
    return [
        {"verdict": verdict, "red_flags": red_flags, "reason": (reason or "")[:200], "confidence_delta": confidence_delta}
        for verdict, red_flags, reason, confidence_delta in rows
    ]


async def run_self_review(session: AsyncSession, client: OpenRouterClient | None = None) -> list[dict[str, Any]]:
    """Полный воскресный самоанализ. Возвращает список сохранённых инсайтов."""
    client = client or OpenRouterClient()
    reports = await league_reports(session)
    eligible = [report for report in reports if report["signals"] >= settings.self_review_min_signals]
    if not eligible:
        logger.info(
            "self_review: нет лиг с {} и более закрытыми сигналами за неделю — пропускаю",
            settings.self_review_min_signals,
        )
        return []

    saved: list[dict[str, Any]] = []
    for report in eligible:
        adjustments = await _judge_adjustments(session, report["sport"], report["league"], REVIEW_WINDOW_DAYS)
        if adjustments:
            report["judge_adjustments"] = adjustments
        result = await review_league(client, report)

        # Гасим прошлые инсайты этой лиги — активным должен быть только свежий.
        previous = (
            await session.execute(
                select(LeagueInsight).where(
                    LeagueInsight.sport == report["sport"],
                    LeagueInsight.league == report["league"][:160],
                    LeagueInsight.active.is_(True),
                )
            )
        ).scalars().all()
        for row in previous:
            row.active = False

        payload = {
            "summary": result.summary,
            "insights": [item.model_dump() for item in result.insights],
            "watch_next_week": result.watch_next_week,
            "metrics": {
                key: report[key]
                for key in ("signals", "won", "lost", "win_rate", "roi", "avg_edge", "avg_confidence", "judge_stats")
                if key in report
            },
            "llm_ok": result.llm_ok,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
        insight = LeagueInsight(sport=report["sport"], league=report["league"][:160], insights_json=payload, active=True)
        session.add(insight)
        await session.flush()
        saved.append({"league": report["league"], "sport": report["sport"], "insight_id": insight.id, "llm_ok": result.llm_ok})
        logger.info(
            "self_review: {} ({}) → {} инсайтов (LLM ok={})",
            report["league"], report["sport"], len(result.insights), result.llm_ok,
        )

    await session.commit()
    logger.info("self_review: сохранено инсайтов по лигам — {}", len(saved))
    return saved


async def active_insights(session: AsyncSession, sport_code: str, league: str | None = None) -> list[dict[str, Any]]:
    """Активные инсайты лиги — подставляются в промпты аналитика и арбитра."""
    query = select(LeagueInsight).where(LeagueInsight.sport == sport_code, LeagueInsight.active.is_(True))
    if league:
        query = query.where(LeagueInsight.league == league[:160])
    rows = (await session.execute(query.order_by(LeagueInsight.created_at.desc()).limit(3))).scalars().all()
    insights: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row.insights_json or {})
        payload.setdefault("league", row.league)
        insights.append(payload)
    return insights


def debug_report_json(reports: list[dict[str, Any]]) -> str:
    """Текстовая сводка отчётов (используется в scripts/check_models.py и логах)."""
    import json

    return json.dumps(reports, ensure_ascii=False, indent=2, default=str)
