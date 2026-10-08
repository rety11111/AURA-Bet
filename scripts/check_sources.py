#!/usr/bin/env python
"""Диагностика источников данных BetSignals.

Скрипт отвечает на три вопроса:
  1) какие источники вообще включены (есть ли ключи/URL в .env);
  2) какие из них реально отвечают ПРЯМО СЕЙЧАС и что удаётся распарсить;
  3) что лежит в БД (таблицы/строки/миграция) — опционально, флагом --db.

Зачем: бесплатные тарифы меняются, эндпоинты переименовываются, а у Winline/BetBoom
URL вообще вводятся вами руками из DevTools. Скрипт печатает количество событий/кэфов
и печатает сырые подсказки (URL), чтобы можно было проверить руками.

Запуск:
    python scripts/check_sources.py                  # все включённые источники
    python scripts/check_sources.py --offline        # только «что включено», без сети
    python scripts/check_sources.py --source winline --source understat
    python scripts/check_sources.py --sport football --team "Спартак"
    python scripts/check_sources.py --db             # + проверка БД
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.pipeline.collector import Providers, build_providers, close_providers  # noqa: E402
from app.sources.base import SourceError  # noqa: E402

OK = "✅"
BAD = "❌"
SKIP = "⏭️"

Probe = Callable[["ProbeContext"], Awaitable[str]]


class ProbeContext:
    def __init__(self, providers: Providers, args: argparse.Namespace) -> None:
        self.providers = providers
        self.args = args

    @property
    def team(self) -> str:
        return self.args.team

    @property
    def team2(self) -> str:
        return self.args.team2

    @property
    def sport(self) -> str:
        return self.args.sport

    @property
    def day(self) -> date:
        return self.args.day or date.today()


def banner(text: str) -> None:
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


def section(title: str) -> None:
    print("\n" + "─" * 78)
    print(title)
    print("─" * 78)


# --------------------------------------------------------------------------- #
# Пробники источников
# --------------------------------------------------------------------------- #
async def probe_odds_bookmaker(ctx: ProbeContext, provider: Any) -> str:
    """Winline/BetBoom: прематч + кэфы первого матча (это и есть проверка маппингов)."""
    matches = await provider.get_upcoming(ctx.sport, ctx.day)
    lines = [f"URL: {provider.base_url or '(не задан)'}", f"прематч {ctx.sport} на {ctx.day}: событий = {len(matches)}"]
    if matches:
        sample = matches[0]
        lines.append(f"пример: {sample.home_team} — {sample.away_team} | {sample.league} | {sample.starts_at}")
        odds = await provider.get_odds(str(sample.ext_id))
        markets = sorted({odd.market for odd in odds})
        lines.append(f"кэфов по матчу {sample.ext_id}: {len(odds)} (рынки: {', '.join(markets) or 'нет'})")
        for odd in odds[:8]:
            lines.append(f"   · {odd.market}/{odd.selection} line={odd.line} → {odd.price}")
        if not odds:
            lines.append("   ⚠️ Кэфы не распарсились — сверьте JSON из DevTools с FIELD_MAPPING в sources/winline.py")
    else:
        lines.append("   ⚠️ Событий нет: проверьте параметры запроса (sport id / период) в .env")
    if provider.live_base_url:
        try:
            live = await provider.get_live_events(ctx.sport)
            lines.append(f"лайв URL: {provider.live_base_url} → событий = {len(live)}")
        except SourceError as exc:
            lines.append(f"лайв недоступен: {exc}")
    return "\n".join(lines)


async def _probe_the_odds_api(ctx: ProbeContext, provider: Any) -> str:
    sports = await provider.get_sports()
    lines = [f"URL: {provider.base_url}", f"доступно спортов в каталоге: {len(sports)}"]
    matches = await provider.get_upcoming(ctx.sport, ctx.day)
    lines.append(f"события {ctx.sport}: {len(matches)}")
    if matches:
        odds = await provider.get_odds(str(matches[0].ext_id))
        lines.append(f"кэфы первого матча: {len(odds)}")
    quota = provider.quota_info()
    lines.append(f"квота: {quota}")
    return "\n".join(lines)


async def probe_apifootball(ctx: ProbeContext) -> str:
    provider = ctx.providers.football
    if provider is None:
        return "провайдер не создан"
    lines = [f"URL: {provider.base_url}", f"ключ задан: {bool(provider.api_key)}"]
    form = await provider.get_recent_form(ctx.team, n=5)
    lines.append(f"форма «{ctx.team}»: {len(form)} матчей" + (f" | пример: {form[0]}" if form else ""))
    h2h = await provider.get_h2h(ctx.team, ctx.team2)
    lines.append(f"H2H {ctx.team} — {ctx.team2}: {len(h2h)}")
    injuries = await provider.get_injuries(ctx.team)
    lines.append(f"травмы «{ctx.team}»: {len(injuries)}")
    lineup = await provider.get_predicted_lineups(ctx.team)
    lines.append(f"прогноз состава: {'есть' if lineup else 'нет (норма вне 48ч до матча)'}")
    return "\n".join(lines)


async def probe_understat(ctx: ProbeContext) -> str:
    provider = ctx.providers.understat
    if provider is None:
        return "провайдер не создан"
    league = await provider.fetch_league("EPL")
    lines = [
        f"URL шаблон: {provider.base_url}",
        f"EPL сезон {league.season}: команд = {len(league.teams)}",
        f"средние голы лиги (дом/гости): {league.league_avg_home:.3f} / {league.league_avg_away:.3f}",
    ]
    stats = await provider.get_team_xg(ctx.team, "EPL")
    if stats:
        lines.append(
            f"xG «{ctx.team}»: дома {stats.xg_for_home or '—'}, в гостях {stats.xg_for_away or '—'}, "
            f"матчей {stats.matches} → data_quality={'ok' if stats.matches >= settings.min_recent_matches else 'weak'}"
        )
    else:
        lines.append(f"xG «{ctx.team}» не найден — подставьте другое имя (--team) или код лиги")
    return "\n".join(lines)


async def probe_nhl(ctx: ProbeContext) -> str:
    provider = ctx.providers.nhl
    if provider is None:
        return "провайдер не создан"
    lines = [f"URL шаблон: {provider.base_url}"]
    games = await provider.get_todays_games()
    lines.append(f"матчи NHL сегодня: {len(games)}")
    form = await provider.get_recent_form(ctx.team, n=5)
    lines.append(f"форма «{ctx.team}»: {len(form)} матчей")
    goalie = await provider.get_probable_goalie(ctx.team, ctx.team2)
    lines.append(f"вероятный вратарь «{ctx.team}»: {(goalie or {}).get('name') or 'не найден'}")
    stats = await provider.get_team_goal_stats(ctx.team, n=10)
    lines.append(f"голевая статистика (xG): {'есть' if stats else 'нет'}")
    return "\n".join(lines)


async def probe_moneypuck(ctx: ProbeContext) -> str:
    provider = ctx.providers.moneypuck
    if provider is None:
        return "провайдер не создан"
    teams = await provider.load_teams()
    lines = [f"CSV каталог: {provider.base_url}", f"команд в seasonSummary: {len(teams)}"]
    stats = await provider.get_team_xg(ctx.team)
    lines.append(f"xG «{ctx.team}»: {'есть' if stats else 'не найдена (проверьте написание)'}")
    return "\n".join(lines)


async def probe_apibasketball(ctx: ProbeContext) -> str:
    provider = ctx.providers.basketball
    if provider is None:
        return "провайдер не создан"
    lines = [f"URL: {provider.base_url}"]
    games = await provider.get_games(ctx.day)
    lines.append(f"игр на {ctx.day}: {len(games)}")
    stats = await provider.get_team_stats(ctx.team)
    lines.append(f"pace/ORtg/DRtg «{ctx.team}»: {'есть' if stats else 'нет'}")
    return "\n".join(lines)


async def probe_balldontlie(ctx: ProbeContext) -> str:
    provider = ctx.providers.balldontlie
    if provider is None:
        return "провайдер не создан"
    lines = [f"URL: {provider.base_url}"]
    games = await provider.get_games(ctx.day, ctx.day + timedelta(days=1))
    lines.append(f"игр на {ctx.day}: {len(games)}")
    stats = await provider.get_team_stats(ctx.team)
    lines.append(f"статистика «{ctx.team}»: {'есть' if stats else 'нет'}")
    return "\n".join(lines)


async def probe_apitennis(ctx: ProbeContext) -> str:
    provider = ctx.providers.tennis
    if provider is None:
        return "провайдер не создан"
    matches = await provider.get_upcoming(ctx.day)
    lines = [f"URL: {provider.base_url}", f"матчей на {ctx.day}: {len(matches)}"]
    if matches:
        lines.append(f"пример: {matches[0].home_team} — {matches[0].away_team} ({matches[0].league})")
    form = await provider.get_recent_form(ctx.team, n=5)
    lines.append(f"форма «{ctx.team}»: {len(form)} матчей")
    if not matches and not form:
        lines.append(
            "   ⚠️ API-Tennis: точные пути эндпоинтов НЕ подтверждены — провайдер пробует известные "
            "варианты (см. PROBE_PATHS в sources/apitennis.py и SETUP.md)"
        )
    return "\n".join(lines)


async def probe_mma_rss(ctx: ProbeContext) -> str:
    provider = ctx.providers.fight_news
    if provider is None:
        return "провайдер не создан"
    items = await provider.all_items(hours=72)
    lines = [f"RSS-ленты: {', '.join(provider.feeds)}", f"новостей за 72ч: {len(items)}"]
    news = await provider.get_news(name=ctx.team, limit=3)
    for item in news:
        lines.append(f"   · {item.get('title', '')[:110]}")
    return "\n".join(lines)


async def probe_opendota(ctx: ProbeContext) -> str:
    provider = ctx.providers.opendota
    if provider is None:
        return "провайдер не создан"
    live = await provider.get_live_matches("dota2")
    lines = [f"URL: {provider.base_url}", f"лайв-матчей Dota 2: {len(live)}"]
    if live:
        first = live[0]
        score = f"{first.series_score[0]}:{first.series_score[1]}" if first.series_score else "—"
        lines.append(f"пример: {first.team_a} — {first.team_b} | счёт {score} | id={first.ext_id} | стадия {first.stage}")
        draft = await provider.get_draft(str(first.ext_id))
        lines.append(f"драфт: пики A={len(draft[0])}, пики B={len(draft[1])}")
    return "\n".join(lines)


async def probe_liquipedia(ctx: ProbeContext) -> str:
    provider = ctx.providers.liquipedia
    if provider is None:
        return "провайдер не создан"
    if not provider.available:
        return (
            "❌ Недоступен: задайте LIQUIPEDIA_USER_AGENT в формате "
            '«BetSignalsBot/1.0 (contact: your@email)» — см. SETUP.md'
        )
    matches = await provider.get_upcoming_matches("cs2", days=3)
    lines = [f"API: {provider.base_url}", f"матчей CS2 на 3 дня: {len(matches)}"]
    tier = await provider.get_tournament_tier("BLAST Premier World Final")
    lines.append(f"tier «BLAST Premier World Final»: {tier or 'не определён'}")
    return "\n".join(lines)


PROBES: dict[str, Probe] = {
    "apifootball": probe_apifootball,
    "understat": probe_understat,
    "nhl": probe_nhl,
    "moneypuck": probe_moneypuck,
    "apibasketball": probe_apibasketball,
    "balldontlie": probe_balldontlie,
    "apitennis": probe_apitennis,
    "mma_rss": probe_mma_rss,
    "opendota": probe_opendota,
    "liquipedia": probe_liquipedia,
}


def availability_table(providers: Providers) -> None:
    info = providers.describe()
    print("Коэффициенты (odds):")
    for name, available in info["odds"].items():
        print(f"  {OK if available else SKIP} {name}")
    print("Статистика/киберспорт (stats):")
    for name, available in info["stats"].items():
        print(f"  {OK if available else SKIP} {name}")


# --------------------------------------------------------------------------- #
# БД
# --------------------------------------------------------------------------- #
async def check_db() -> None:
    section("База данных")
    from sqlalchemy import func, select, text

    from app.db.database import dispose_engine, init_engine, session_scope
    from app.db.models import ALL_TABLES

    print(f"DATABASE_URL: {settings.database_url.split('@')[-1]}")
    init_engine()
    try:
        async with session_scope() as session:
            version = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar()
            print(f"миграция (alembic_version): {version or 'НЕТ — примените `alembic upgrade head`'}")
            for model in ALL_TABLES:
                count = (await session.execute(select(func.count()).select_from(model))).scalar()
                print(f"  {model.__tablename__:>18}: {count} строк")
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD} БД недоступна: {type(exc).__name__}: {exc}")
        print("   Проверьте DATABASE_URL (Railway → PostgreSQL → Variables) и `alembic upgrade head`.")
    finally:
        await dispose_engine()


# --------------------------------------------------------------------------- #
# Основной прогон
# --------------------------------------------------------------------------- #
async def run_probes(providers: Providers, args: argparse.Namespace) -> int:
    ctx = ProbeContext(providers, args)
    results: list[tuple[str, bool, str]] = []
    selected = args.source

    async def run(name: str, coro: Awaitable[str]) -> None:
        section(f"Источник: {name}")
        try:
            output = await asyncio.wait_for(coro, timeout=args.timeout)
            print(f"{OK} OK\n{output}")
            results.append((name, True, output))
        except asyncio.TimeoutError:
            print(f"{BAD} таймаут {args.timeout}s — источник висит (проверьте доступность сети/URL)")
            results.append((name, False, "timeout"))
        except SourceError as exc:
            print(f"{BAD} SourceError: {exc}")
            results.append((name, False, str(exc)))
        except Exception as exc:  # noqa: BLE001
            print(f"{BAD} {type(exc).__name__}: {exc}")
            results.append((name, False, f"{type(exc).__name__}: {exc}"))

    odds_by_name = {provider.provider_name: provider for provider in providers.odds}
    for name in ("winline", "betboom"):
        if name in odds_by_name and (not selected or name in selected):
            provider = odds_by_name[name]
            if not provider.available:
                section(f"Источник: {name}")
                print(f"{SKIP} пропущен: {name.upper()}_API_BASE не задан в .env (см. SETUP.md → DevTools)")
                results.append((name, True, "skipped"))
                continue
            await run(name, probe_odds_bookmaker(ctx, provider))

    if "theoddsapi" in odds_by_name and (not selected or "theoddsapi" in selected):
        provider = odds_by_name["theoddsapi"]
        if not provider.available:
            section("Источник: theoddsapi")
            print(f"{SKIP} пропущен: THE_ODDS_API_KEY не задан")
            results.append(("theoddsapi", True, "skipped"))
        else:
            await run("theoddsapi", _probe_the_odds_api(ctx, provider))

    for name, probe in PROBES.items():
        if selected and name not in selected:
            continue
        await run(name, probe(ctx))

    section("АГРЕГАТОР: сведение линий ≥2 источников")
    available_odds = providers.available_odds()
    if len(available_odds) < 2:
        print(f"{SKIP} Доступно источников кэфов: {len(available_odds)} — сравнить маржу не из чего.")
        print("   Добавьте URL Winline/BetBoom в .env (SETUP.md) или ключ TheOddsApi.")
    else:
        try:
            await asyncio.wait_for(_probe_aggregator(providers, ctx), timeout=args.timeout * 2)
        except Exception as exc:  # noqa: BLE001
            print(f"{BAD} агрегатор: {type(exc).__name__}: {exc}")

    banner("ИТОГ")
    for name, ok, _ in results:
        print(f"  {OK if ok else BAD} {name}")
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        print(f"\nНе ответили: {', '.join(failed)}")
    print(
        "\nНапоминание: отказ любого источника — это НЕ катастрофа. Сервис работает на остальных, "
        "а матчи без данных помечаются data_quality=weak (сигналы по ним не отправляются)."
    )
    return 1 if failed else 0


async def _probe_aggregator(providers: Providers, ctx: ProbeContext) -> None:
    from rapidfuzz import fuzz

    from app.sources.odds_aggregator import aggregate_outcomes

    per_source: dict[str, list[Any]] = {}
    for provider in providers.available_odds():
        try:
            found = await provider.get_upcoming(ctx.sport, ctx.day)
        except Exception as exc:  # noqa: BLE001
            print(f"   {provider.provider_name}: не удалось получить список ({exc})")
            continue
        print(f"   {provider.provider_name}: событий {len(found)}")
        per_source[provider.provider_name] = found

    reference_source = next(iter(per_source), None)
    if reference_source is None or not per_source[reference_source]:
        print("   ⚠️ Ни один источник не отдал событий — агрегатор нечего сводить.")
        return

    reference = per_source[reference_source][0]
    ref_key = f"{reference.home_team} {reference.away_team}".lower()
    print(f"\nОпорный матч ({reference_source}): {reference.home_team} — {reference.away_team}")
    print(f"   время начала: {reference.starts_at}, лига: {reference.league}")

    # Для каждого источника ищем ТОТ ЖЕ матч и берём его собственный ext_id
    ext_ids: dict[str, str] = {}
    for name, matches in per_source.items():
        best = max(
            matches,
            key=lambda match: fuzz.token_set_ratio(ref_key, f"{match.home_team} {match.away_team}".lower()),
            default=None,
        )
        if best is None:
            continue
        score = fuzz.token_set_ratio(ref_key, f"{best.home_team} {best.away_team}".lower())
        marker = "совпал" if score >= 85 else "НЕ уверен"
        print(f"   {name}: «{best.home_team} — {best.away_team}» ({marker}, score={score:.0f}, ext_id={best.ext_id})")
        if score >= 85:
            ext_ids[name] = str(best.ext_id)

    if len(ext_ids) < 2:
        print("   ⚠️ Совпал только один источник — сравнить маржу/медиану не с чем (это норма для одного букмекера).")
    collected = await providers.aggregator.collect_for_match(ext_ids)
    print(f"собрано снимков кэфов: {len(collected)}")
    outcomes = aggregate_outcomes(collected)
    print(f"агрегированных исходов: {len(outcomes)}")
    for outcome in outcomes[:12]:
        flag = f" ⚠️ {outcome.suspicious_reason}" if outcome.suspicious else ""
        print(
            f"   · {outcome.market}/{outcome.selection} line={outcome.line}: "
            f"implied={outcome.implied:.3f} (источников {outcome.sources}), "
            f"best={outcome.best_price} ({outcome.best_source}){flag}"
        )
    if not outcomes:
        print(
            "   ⚠️ Пусто. Проверьте: 1) маппинги полей (sources/winline.py, FIELD_MAPPING); "
            "2) что рынок полный (1X2 = три исхода, тотал = оба плеча)."
        )


def build_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Проверка источников данных BetSignals")
    parser.add_argument("--source", action="append", default=None, help="проверить только указанные источники")
    parser.add_argument("--sport", default="football", help="вид спорта для прематч-запросов (по умолчанию football)")
    parser.add_argument("--team", default="Арсенал", help="команда для проверок формы/травм (по умолчанию «Арсенал»)")
    parser.add_argument("--team2", default="Челси", help="вторая команда для H2H")
    parser.add_argument("--day", type=date.fromisoformat, default=None, help="дата (YYYY-MM-DD), по умолчанию сегодня")
    parser.add_argument("--timeout", type=float, default=30.0, help="таймаут на один источник, сек")
    parser.add_argument("--db", action="store_true", help="дополнительно проверить БД и счётчики строк")
    parser.add_argument("--offline", action="store_true", help="без сети: только список включённых источников")
    return parser.parse_args()


async def main() -> int:
    args = build_args()
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    providers = build_providers()
    banner("BetSignals — диагностика источников")
    print(f"Время: {datetime.now(timezone.utc).isoformat()}")
    print(f"LLM: screener={settings.screener_model}, analyzer={settings.analyzer_model}, judge={settings.judge_model}")
    availability_table(providers)

    exit_code = 0
    try:
        if args.offline:
            print("\n(--offline: сетевые проверки пропущены)")
        else:
            exit_code = await run_probes(providers, args)
        if args.db:
            await check_db()
    finally:
        await close_providers(providers)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
