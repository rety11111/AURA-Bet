"""Склейка названий команд между источниками (rapidfuzz, порог 85).

Задача: «Спартак Мск» (Winline) и «Spartak Moscow» (API-Football) должны стать
одной командой в таблице `teams`. Как это работает:

1. Exact-match по таблице `team_aliases` (регистронезависимо).
2. Fuzzy-матчинг по нормализованным строкам: чистка пунктуации/юридических
   приставок, раскрытие сокращений («мск» → «moscow», «спб» → «saint petersburg»),
   транслитерация кириллицы в латиницу (чтобы «Спартак» совпал со «Spartak»).
3. Score = max(token_set_ratio, WRatio). Score ≥ 85 → склеиваем и записываем
   новый alias для источника. 70 ≤ score < 85 → логируем пару как НЕраспознанную
   (без склейки!). < 70 → создаём новую команду.

Нераспознанные пары не «угадываются»: они попадают в лог warning'ом со
score'ом, чтобы человек мог руками добавить alias в таблицу (см. SETUP.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from loguru import logger
from rapidfuzz import fuzz
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Team, TeamAlias

MATCH_THRESHOLD = 85.0
LOG_THRESHOLD = 70.0

_CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

_ABBREVIATIONS = {
    "мск": "moscow",
    "msk": "moscow",
    "mck": "moscow",
    "спб": "saint petersburg",
    "птг": "petersburg",
    "екб": "yekaterinburg",
    "нн": "nizhny novgorod",
    "уфа": "ufa",
    "дон": "don",
    "фк": "",
    "fk": "",   # после транслитерации «фк» превращается в «fk» — чистим оба варианта
    "fc": "",
    "cf": "",
    "sc": "",
    "afc": "",
    "bc": "",
    "хк": "",
    "пфк": "",
    "клуб": "",
    "club": "",
    "team": "",
    "фарм": "",
    "м": "",
}

_SPACE_RE = re.compile(r"[\s\-–—.,()\[\]{}/\\]+")


# Латинские правила «фонетической» нормализации: русские транскрипции английских
# названий читаются иначе, чем оригинал («Сити» ← city, «Селтик» ← celtic),
# поэтому перед сравнением приводим оба варианта к общему виду.
_LATIN_C_BEFORE_VOWEL_RE = re.compile(r"c(?=[iey])")


def transliterate(text: str) -> str:
    """Кириллица → латиница + фонетическая чистка латиницы.

    Правила (сознательно простые и предсказуемые):
      * «c» перед i/e/y читается как «с»: city → siti, celtic → seltik;
      * «y» → «i»: Sydney → sidnei (и кириллические «й»/«ы» → «i» тоже).
    Они помогают склеивать «Манчестер Сити» ↔ «Manchester City», а спорные пары
    всё равно попадают в лог со score для ручного добавления alias (см. SETUP.md).
    """
    lowered = text.lower()
    latin = _LATIN_C_BEFORE_VOWEL_RE.sub("s", lowered).replace("y", "i")
    return "".join(_CYRILLIC_TO_LATIN.get(ch, ch) for ch in latin)


def normalize_name(name: str) -> str:
    """Нормализация для сравнения: латиница, без пунктуации, без приставок/сокращений."""
    text = transliterate(str(name or "").strip())
    text = _SPACE_RE.sub(" ", text)
    tokens: list[str] = []
    for token in text.split():
        token = token.strip("'`")
        token = _ABBREVIATIONS.get(token, token)
        if token:
            tokens.append(token)
    # «Real Madrid CF» и «FC Real Madrid» должны совпасть → сортируем токены
    return " ".join(sorted(tokens))


def similarity(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    return max(
        fuzz.token_set_ratio(na, nb),
        fuzz.WRatio(na, nb),
        fuzz.partial_ratio(na, nb) if min(len(na), len(nb)) >= 6 else 0.0,
    )


@dataclass
class Resolution:
    team_id: int | None
    canonical_name: str
    score: float
    matched_by: str  # exact | fuzzy | created | unresolved


class TeamMatcher:
    """Резолвер команд в рамках сессии БД и одного вида спорта."""

    def __init__(self, session: AsyncSession, sport_id: int, threshold: float = MATCH_THRESHOLD) -> None:
        self.session = session
        self.sport_id = sport_id
        self.threshold = threshold
        self._alias_index: dict[str, tuple[int, str]] | None = None  # normalized alias → (team_id, canonical)
        self._unresolved_logged: set[tuple[str, str]] = set()

    async def _index(self) -> dict[str, tuple[int, str]]:
        if self._alias_index is None:
            rows = (
                await self.session.execute(
                    select(TeamAlias.alias, Team.id, Team.canonical_name)
                    .join(Team, Team.id == TeamAlias.team_id)
                    .where(Team.sport_id == self.sport_id)
                )
            ).all()
            index: dict[str, tuple[int, str]] = {}
            for alias, team_id, canonical in rows:
                index[normalize_name(alias)] = (team_id, canonical)
                index.setdefault(normalize_name(canonical), (team_id, canonical))
            self._alias_index = index
        return self._alias_index

    async def resolve(self, raw_name: str, source: str) -> Resolution:
        """Находит/создаёт команду. Никогда не склеивает «наугад» (порог 85)."""
        raw_name = str(raw_name or "").strip()
        if not raw_name:
            return Resolution(team_id=None, canonical_name="", score=0.0, matched_by="unresolved")

        index = await self._index()
        normalized = normalize_name(raw_name)

        exact = index.get(normalized)
        if exact:
            return Resolution(team_id=exact[0], canonical_name=exact[1], score=100.0, matched_by="exact")

        best: tuple[float, int, str] | None = None
        for alias_norm, (team_id, canonical) in index.items():
            # Единая функция сравнения (см. similarity): token_set + WRatio + partial_ratio.
            # Одна и та же метрика в resolve/find_team_id и в тестах — без «двух правд».
            score = similarity(normalized, alias_norm)
            if best is None or score > best[0]:
                best = (score, team_id, canonical)

        if best and best[0] >= self.threshold:
            team_id, canonical = best[1], best[2]
            self.session.add(TeamAlias(team_id=team_id, alias=raw_name, source=source))
            await self.session.flush()
            index[normalized] = (team_id, canonical)
            logger.debug(
                "team_matching: «{}» ({}) → «{}» (id={}, score={:.1f})",
                raw_name, source, canonical, team_id, best[0],
            )
            return Resolution(team_id=team_id, canonical_name=canonical, score=best[0], matched_by="fuzzy")

        if best and best[0] >= LOG_THRESHOLD:
            pair = (raw_name.lower(), best[2].lower())
            if pair not in self._unresolved_logged:
                self._unresolved_logged.add(pair)
                logger.warning(
                    "team_matching: НЕраспознанная пара «{}» ({}) ≈ «{}» (score={:.1f} < {}). "
                    "Склейка не выполнена — добавьте alias вручную в team_aliases, если это одна команда.",
                    raw_name, source, best[2], best[0], self.threshold,
                )
        created = await self._create_team(raw_name, source)
        return Resolution(team_id=created[0], canonical_name=created[1], score=best[0] if best else 0.0, matched_by="created")

    async def _create_team(self, raw_name: str, source: str) -> tuple[int, str]:
        team = Team(sport_id=self.sport_id, canonical_name=raw_name)
        self.session.add(team)
        await self.session.flush()
        self.session.add(TeamAlias(team_id=team.id, alias=raw_name, source=source))
        await self.session.flush()
        index = await self._index()
        index[normalize_name(raw_name)] = (team.id, raw_name)
        logger.debug("team_matching: создана команда «{}» (id={}, источник {})", raw_name, team.id, source)
        return team.id, raw_name

    async def get_or_create_team_id(self, raw_name: str, source: str) -> int | None:
        resolution = await self.resolve(raw_name, source)
        return resolution.team_id

    async def add_alias(self, team_id: int, alias: str, source: str = "manual") -> None:
        """Ручное добавление alias (используется из скриптов/админских команд)."""
        self.session.add(TeamAlias(team_id=team_id, alias=alias, source=source))
        await self.session.flush()
        if self._alias_index is not None:
            canonical = (
                await self.session.execute(select(Team.canonical_name).where(Team.id == team_id))
            ).scalar_one_or_none() or alias
            self._alias_index[normalize_name(alias)] = (team_id, canonical)
        logger.info("team_matching: добавлен alias «{}» → team_id={}", alias, team_id)

    async def find_team_id(self, raw_name: str) -> int | None:
        """Только поиск, без создания (для статистических источников)."""
        index = await self._index()
        normalized = normalize_name(raw_name)
        exact = index.get(normalized)
        if exact:
            return exact[0]
        best: tuple[float, int] | None = None
        for alias_norm, (team_id, _canonical) in index.items():
            score = similarity(normalized, alias_norm)
            if best is None or score > best[0]:
                best = (score, team_id)
        if best and best[0] >= self.threshold:
            return best[1]
        return None


async def log_unresolved_alias_report(session: AsyncSession, limit: int = 50) -> list[dict[str, Any]]:
    """Отчёт «сколько alias у каждой команды» — удобно для ручной чистки таблицы."""
    rows = (
        await session.execute(
            select(Team.canonical_name, func.count(TeamAlias.id))
            .join(TeamAlias, TeamAlias.team_id == Team.id)
            .group_by(Team.canonical_name)
            .order_by(func.count(TeamAlias.id).desc())
            .limit(limit)
        )
    ).all()
    return [{"team": canonical, "aliases": count} for canonical, count in rows]


# --------------------------------------------------------------------------- #
# Кэш xG-статистики в БД (таблица xg_cache)
# --------------------------------------------------------------------------- #
async def upsert_xg_cache(session: AsyncSession, team_id: int, source: str, payload: dict[str, Any]) -> None:
    """Записывает/обновляет xG-статистику команды (ключ: team_id + source).

    Использует select+update вместо ON CONFLICT — так одинаково работает и в
    PostgreSQL (продакшен), и в sqlite (тесты).
    """
    from datetime import datetime, timezone

    from app.db.models import XgCache

    row = (
        await session.execute(
            select(XgCache).where(XgCache.team_id == team_id, XgCache.source == source[:40])
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if row is None:
        session.add(XgCache(team_id=team_id, source=source[:40], payload=payload, updated_at=now))
    else:
        row.payload = payload
        row.updated_at = now
    await session.flush()


async def load_xg_cache(session: AsyncSession, team_id: int, source: str, max_age_sec: int) -> dict[str, Any] | None:
    """Читает xG-статистику из xg_cache, если она не старше max_age_sec. Иначе None (пойдём в сеть)."""
    from datetime import datetime, timezone

    from app.db.models import XgCache

    row = (
        await session.execute(
            select(XgCache).where(XgCache.team_id == team_id, XgCache.source == source[:40])
        )
    ).scalar_one_or_none()
    if row is None or not row.payload:
        return None
    updated = row.updated_at
    if updated is None:
        return row.payload
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    age_sec = (datetime.now(timezone.utc) - updated).total_seconds()
    if age_sec > max_age_sec:
        logger.debug(
            "xg_cache: запись team_id={} source={} устарела ({:.0f} сек > {} сек)",
            team_id, source, age_sec, max_age_sec,
        )
        return None
    return row.payload
