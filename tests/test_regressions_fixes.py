"""Регрессионные тесты: прематч-проходы, stale-кандидаты, BigInteger tg_id, Liquipedia."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

import app.scheduler as scheduler_module
from app.db.models import Match, MatchStatus, Signal, SignalStatus, User
from app.pipeline import analyzer
from app.pipeline.analyzer import _context_stats, reject_stale_candidates


# --- 1. scheduler: именованные аргументы -------------------------------------
async def test_job_prematch_passes_uses_keyword_args(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, Any]] = []
    providers = object()

    async def fake_pass(session: Any, pass_no: int, providers: Any = None) -> int:
        calls.append((pass_no, providers))
        return pass_no

    class _Scope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *exc: object) -> None:
            return None

    async def noop(*_a: object, **_k: object) -> int:
        return 0

    monkeypatch.setattr(scheduler_module, "get_providers", lambda: providers)
    monkeypatch.setattr(scheduler_module, "session_scope", lambda: _Scope())
    monkeypatch.setattr(scheduler_module, "run_prematch_pass", fake_pass)
    monkeypatch.setattr(scheduler_module, "run_late_matches", noop)
    monkeypatch.setattr(scheduler_module, "reject_stale_candidates", noop)

    result = await scheduler_module.job_prematch_passes()
    assert result["pass1"] == 1 and result["pass2"] == 2
    assert calls == [(1, providers), (2, providers)]


# --- 2. reject_stale_candidates ----------------------------------------------
async def _match_with_signal(session: Any, sport_id: int, ext: str, starts_at: datetime) -> Signal:
    match = Match(sport_id=sport_id, ext_id=ext, league="L", starts_at=starts_at, status=MatchStatus.SCHEDULED)
    session.add(match)
    await session.flush()
    signal = Signal(
        match_id=match.id, market="1x2", selection="home", odds=2.0, prob_final=0.6,
        prob_implied=0.5, edge=0.1, confidence_score=70.0, stake_pct=1.0,
        status=SignalStatus.CANDIDATE,
    )
    session.add(signal)
    await session.flush()
    return signal


async def test_reject_stale_candidates_without_ids(session: Any, sport_id: int) -> None:
    now = datetime.now(timezone.utc)
    started = await _match_with_signal(session, sport_id, "a", now - timedelta(minutes=30))
    future = await _match_with_signal(session, sport_id, "b", now + timedelta(hours=3))

    assert await reject_stale_candidates(session) == 1
    await session.refresh(started)
    await session.refresh(future)
    assert started.status == SignalStatus.REJECTED
    assert future.status == SignalStatus.CANDIDATE


async def test_reject_stale_candidates_with_ids_and_empty(session: Any, sport_id: int) -> None:
    now = datetime.now(timezone.utc)
    sig = await _match_with_signal(session, sport_id, "c", now + timedelta(hours=3))
    assert await reject_stale_candidates(session, []) == 0
    assert await reject_stale_candidates(session, [sig.match_id]) == 1


# --- 3. BigInteger tg_id -----------------------------------------------------
def test_user_tg_id_is_biginteger() -> None:
    assert isinstance(User.__table__.c.tg_id.type, sa.BigInteger)


async def test_user_accepts_64bit_tg_id(session: Any) -> None:
    session.add(User(tg_id=7_000_000_000))
    await session.flush()


def test_migration_0002_alters_tg_id() -> None:
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0002_user_tg_id_bigint.py"
    spec = importlib.util.spec_from_file_location("mig0002", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0001_initial"

    recorded: list[tuple[tuple, dict]] = []
    module.op = SimpleNamespace(alter_column=lambda *a, **k: recorded.append((a, k)))
    module.upgrade()
    (args, kwargs), = recorded
    assert args == ("users", "tg_id")
    assert isinstance(kwargs["existing_type"], sa.Integer)
    assert isinstance(kwargs["type_"], sa.BigInteger)


# --- 4. Liquipedia resilience ------------------------------------------------
class _BrokenLiquipedia:
    available = True

    async def get_tournament_tier(self, league: str) -> str:
        raise RuntimeError("404")

    async def get_team_roster(self, sport: str, team: str) -> list[str]:
        raise RuntimeError("circuit open")


class _OkLiquipedia:
    available = True

    async def get_tournament_tier(self, league: str) -> str:
        return "S-Tier"

    async def get_team_roster(self, sport: str, team: str) -> list[str]:
        return [team + "1"]


async def _esports_ctx(liquipedia: Any) -> dict[str, Any]:
    match = SimpleNamespace(id=1, league="ESL")
    return await _context_stats(
        SimpleNamespace(liquipedia=liquipedia), "cs2", match, "A", "B", is_live=False, live_context=None
    )


async def test_liquipedia_errors_do_not_break_context() -> None:
    out = await _esports_ctx(_BrokenLiquipedia())
    assert out["esports"] == {"tier": None, "roster_home": None, "roster_away": None}


async def test_liquipedia_unavailable_or_missing_is_skipped() -> None:
    assert "esports" not in await _esports_ctx(None)
    unavailable = _OkLiquipedia()
    unavailable.available = False
    assert "esports" not in await _esports_ctx(unavailable)


async def test_liquipedia_ok() -> None:
    out = await _esports_ctx(_OkLiquipedia())
    assert out["esports"]["tier"] == "S-Tier"
    assert out["esports"]["roster_home"] == ["A1"]
