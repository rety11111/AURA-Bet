"""ORM-модели (Модуль 9). SQLAlchemy 2.0 typed mapping, совместимо с alembic.

Все временные метки — timezone-aware (UTC в БД, отображение в MSK в боте).
JSON-колонки: JSONB в PostgreSQL, обычный JSON — в остальных диалектах
(нужно исключительно для юнит-тестов на sqlite).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSONB = JSON().with_variant(postgresql.JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Константы-статусы (строки, чтобы не тянуть PG-enum миграции)
# --------------------------------------------------------------------------- #
class MatchStatus:
    SCHEDULED = "scheduled"
    LIVE = "live"
    FINISHED = "finished"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"


class SignalStatus:
    CANDIDATE = "candidate"     # PASS 1: кандидат, ждёт подтверждения на PASS 2
    CONFIRMED = "confirmed"     # подтверждён, готов к рассылке
    SENT = "sent"               # разослан
    WON = "won"
    LOST = "lost"
    VOID = "void"
    REJECTED = "rejected"


class DataQuality:
    OK = "ok"
    WEAK = "weak"


class Market:
    ONE_X_TWO = "1x2"           # 3-way (футбол/хоккей)
    H2H = "1x2"                 # 2-way (теннис/баскетбол/киберспорт) — то же поле, явность в selection
    TOTALS = "totals"
    HANDICAP = "handicap"
    DOUBLE_CHANCE = "double_chance"


class Selection:
    HOME = "home"
    DRAW = "draw"
    AWAY = "away"
    OVER = "over"
    UNDER = "under"
    HOME_HANDICAP = "home_handicap"
    AWAY_HANDICAP = "away_handicap"
    DC_1X = "1x"
    DC_12 = "12"
    DC_X2 = "x2"


# --------------------------------------------------------------------------- #
# Таблицы
# --------------------------------------------------------------------------- #
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    deliveries: Mapped[list[UserDelivery]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Sport(Base):
    __tablename__ = "sports"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # football, hockey, dota2, ...
    name: Mapped[str] = mapped_column(String(64))

    teams: Mapped[list[Team]] = relationship(back_populates="sport")


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("sport_id", "canonical_name", name="uq_team_sport_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id", ondelete="CASCADE"), index=True)
    canonical_name: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sport: Mapped[Sport] = relationship(back_populates="teams")
    aliases: Mapped[list[TeamAlias]] = relationship(back_populates="team", cascade="all, delete-orphan")


class TeamAlias(Base):
    __tablename__ = "team_aliases"
    __table_args__ = (
        UniqueConstraint("alias", "source", name="uq_alias_source"),
        Index("ix_team_aliases_alias_lower", "alias"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(40), default="unknown")  # winline, apifootball, ...
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    team: Mapped[Team] = relationship(back_populates="aliases")


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint("sport_id", "ext_id", name="uq_match_ext"),
        Index("ix_matches_start_status", "starts_at", "status"),
        Index("ix_matches_fixture", "sport_id", "home_team_id", "away_team_id", "starts_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id", ondelete="CASCADE"), index=True)
    ext_id: Mapped[str] = mapped_column(String(120))  # id у основного источника
    league: Mapped[str] = mapped_column(String(160), index=True)
    league_tier: Mapped[str | None] = mapped_column(String(40), nullable=True)  # S-Tier / Tier 1 / ...
    home_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True, index=True)
    away_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True, index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(20), default=MatchStatus.SCHEDULED, index=True)
    result_home: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_away: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_market_moves: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    data_quality: Mapped[str] = mapped_column(String(10), default=DataQuality.OK, server_default="ok")
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Идентификаторы матча в разных источниках: {"winline": "...", "apifootball": "123"}
    external_refs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    sport: Mapped[Sport] = relationship()
    odds: Mapped[list[Odd]] = relationship(back_populates="match", cascade="all, delete-orphan")
    signals: Mapped[list[Signal]] = relationship(back_populates="match", cascade="all, delete-orphan")


class Odd(Base):
    """СНИМКИ коэффициентов. Никогда не перезаписываем — история движения линии."""

    __tablename__ = "odds"
    __table_args__ = (
        Index("ix_odds_lookup", "match_id", "market", "selection", "captured_at"),
        Index("ix_odds_match_captured", "match_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    market: Mapped[str] = mapped_column(String(24))          # 1x2 / totals / handicap / double_chance
    selection: Mapped[str] = mapped_column(String(24))       # home / over / under / home_handicap / 1x ...
    line: Mapped[float | None] = mapped_column(Float, nullable=True)  # 2.5 (тотал), -1.5 (фора)
    price: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(40), index=True)  # winline / betboom / theoddsapi
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    match: Mapped[Match] = relationship(back_populates="odds")


class XgCache(Base):
    __tablename__ = "xg_cache"
    __table_args__ = (UniqueConstraint("team_id", "source", name="uq_xg_team_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(40))  # understat / moneypuck / moneypuck_team
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EloRating(Base):
    __tablename__ = "elo_ratings"
    __table_args__ = (
        UniqueConstraint("sport_id", "team_id", "map_name", name="uq_elo_team_map"),
        Index("ix_elo_sport_map", "sport_id", "map_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    map_name: Mapped[str | None] = mapped_column(String(60), nullable=True)  # None = общий Elo; иначе CS2-карта
    elo: Mapped[float] = mapped_column(Float, default=1500.0)
    matches_played: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (Index("ix_analyses_match_level", "match_id", "level", "pass_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(String(80))
    level: Mapped[int] = mapped_column(Integer, default=2)  # 1 = скринер, 2 = аналитик, 3 = арбитр (см. judge_verdicts)
    pass_no: Mapped[int] = mapped_column(Integer, default=1)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StatPrediction(Base):
    __tablename__ = "stat_predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(String(40))  # poisson / hockey_poisson / basketball / elo
    parsed: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signals_match_market", "match_id", "market", "selection", "line"),
        Index("ix_signals_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    market: Mapped[str] = mapped_column(String(24))
    selection: Mapped[str] = mapped_column(String(24))
    line: Mapped[float | None] = mapped_column(Float, nullable=True)  # фора/тотал
    odds: Mapped[float] = mapped_column(Float)
    prob_final: Mapped[float] = mapped_column(Float)
    prob_implied: Mapped[float] = mapped_column(Float)
    edge: Mapped[float] = mapped_column(Float, index=True)
    confidence_score: Mapped[float] = mapped_column(Float)
    stake_pct: Mapped[float] = mapped_column(Float)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_factors: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    risk_notes: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(16), default=SignalStatus.CANDIDATE, index=True)
    judge_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_live: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    live_stage: Mapped[str | None] = mapped_column(String(80), nullable=True)
    pass_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    odds_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    match: Mapped[Match] = relationship(back_populates="signals")
    deliveries: Mapped[list[UserDelivery]] = relationship(back_populates="signal", cascade="all, delete-orphan")
    judge_verdicts: Mapped[list[JudgeVerdict]] = relationship(
        back_populates="signal", cascade="all, delete-orphan"
    )


class JudgeVerdict(Base):
    __tablename__ = "judge_verdicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id", ondelete="CASCADE"), nullable=True, index=True
    )
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), nullable=True, index=True)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict: Mapped[str] = mapped_column(String(16), index=True)  # confirm / reject / adjust / timeout
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence_delta: Mapped[float] = mapped_column(Float, default=0.0)
    red_flags: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    signal: Mapped[Signal] = relationship(back_populates="judge_verdicts")


class UserDelivery(Base):
    __tablename__ = "user_deliveries"
    __table_args__ = (UniqueConstraint("signal_id", "user_id", name="uq_delivery_signal_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    signal: Mapped[Signal] = relationship(back_populates="deliveries")
    user: Mapped[User] = relationship(back_populates="deliveries")


class LeagueCalibration(Base):
    __tablename__ = "league_calibration"
    __table_args__ = (UniqueConstraint("sport", "league", "market", name="uq_calibration_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[str] = mapped_column(String(32), index=True)
    league: Mapped[str] = mapped_column(String(160), index=True)
    market: Mapped[str] = mapped_column(String(24))
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    calibration: Mapped[float] = mapped_column(Float, default=1.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EnsembleWeight(Base):
    __tablename__ = "ensemble_weights"

    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    w_llm: Mapped[float] = mapped_column(Float, default=0.6)
    w_stat: Mapped[float] = mapped_column(Float, default=0.4)
    brier_llm: Mapped[float | None] = mapped_column(Float, nullable=True)
    brier_stat: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LeagueInsight(Base):
    __tablename__ = "league_insights"
    __table_args__ = (Index("ix_insights_sport_league_active", "sport", "league", "active"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[str] = mapped_column(String(32), index=True)
    league: Mapped[str] = mapped_column(String(160), index=True)
    insights_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


ALL_TABLES = (
    User,
    Sport,
    Team,
    TeamAlias,
    Match,
    Odd,
    XgCache,
    EloRating,
    Analysis,
    StatPrediction,
    Signal,
    JudgeVerdict,
    UserDelivery,
    LeagueCalibration,
    EnsembleWeight,
    LeagueInsight,
)

# Справочник видов спорта — используется при старте (ensure_sports).
SPORT_NAMES: dict[str, str] = {
    "football": "Футбол",
    "hockey": "Хоккей",
    "basketball": "Баскетбол",
    "tennis": "Теннис",
    "mma": "MMA",
    "boxing": "Бокс",
    "dota2": "Dota 2",
    "cs2": "CS2",
}
