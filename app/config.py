"""BetSignals — центральная конфигурация.

Все пороги, таймауты и «магические числа» живут здесь (значения по умолчанию —
в коде, переопределение — через .env / Railway Variables).

ВАЖНО (правило честности): реальные адреса API букмекеров (Winline / BetBoom)
и ID моделей OpenRouter НЕ захардкожены. Они приходят из окружения. ID моделей
по умолчанию проверены через https://openrouter.ai/api/v1/models на 2026-10-08
(см. SETUP.md → «Проверка моделей OpenRouter»), но их следует периодически
перепроверять скриптом scripts/check_models.py.
"""

from __future__ import annotations

import json
from functools import lru_cache
from zoneinfo import ZoneInfo
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Дефолтный whitelist лиг/турниров (Модуль 2). Значения — keywords в нижнем
# регистре; матч проходит, если название лиги из источника содержит любой из
# ключей. Переопределяется целиком переменной LEAGUE_WHITELIST_JSON (см. .env.example).
# ---------------------------------------------------------------------------
DEFAULT_LEAGUE_WHITELIST: dict[str, list[str]] = {
    # Футбол
    "football": [
        "premier league",
        "eng. premier league",
        "epl",
        "la liga",
        "laliga",
        "serie a",
        "bundesliga",
        "ligue 1",
        "rpl",
        "российская премьер",
        "премьер-лига",
        "champions league",
        "лига чемпионов",
        "europa league",
        "лига европы",
        "conference league",
        "лига конференций",
    ],
    # Хоккей
    "hockey": ["nhl", "кхл", "khl"],
    # Баскетбол
    "basketball": ["nba", "евролига", "euroleague"],
    # Теннис
    "tennis": [
        "grand slam",
        "atp 1000",
        "wta 1000",
        "atp 500",
        "wta 500",
        "masters 1000",
        "atp finals",
        "wta finals",
        "шлем",
        "итоговый турнир",
    ],
    # Единоборства
    "mma": ["ufc"],
    "boxing": ["титульный", "title fight", "world championship", "чемпионск"],
    # Киберспорт: прематч только tier-1
    "esports_dota2": ["the international", "ti ", "dota 2 major", "esl one", "pgl major", "riyadh masters"],
    "esports_cs2": ["major", "big event", "esl pro league", "blast premier", "iem ", "s-tier", "katowice", "cologne"],
}

# Спорт-коды, которые использует сервис (совпадают с названиями модулей конфига).
SPORT_CODES = ("football", "hockey", "basketball", "tennis", "mma", "boxing", "dota2", "cs2")

# Sport keys The Odds API (ключи публичного API, не выдуманы — это официальные
# значения из документации the-odds-api.com). Проверить: scripts/check_sources.py.
THE_ODDS_API_SPORT_KEYS: dict[str, str] = {
    "football": "soccer",
    "hockey": "icehockey_nhl",
    "basketball": "basketball_nba",
    "tennis": "tennis_atp",
    "mma": "mma_mixed_martial_arts",
}

MarketsSet = Literal[
    "1x2", "h2h", "totals", "handicap", "double_chance",
]


class Settings(BaseSettings):
    """Все настройки сервиса. Имена полей = имена переменных окружения (регистр не важен)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    app_name: str = "BetSignals"
    tz: str = "Europe/Moscow"

    @property
    def tzinfo(self) -> ZoneInfo:
        """Часовой пояс сервиса (Europe/Moscow по ТЗ). Кэшируем — ZoneInfo сам кэширует внутри."""
        return ZoneInfo(self.tz)
    log_level: str = "INFO"
    log_json: bool = False
    # Railway присылает $PORT — поднимаем healthcheck-сервер, чтобы деплой считался
    # живым (Procfile: web). 0 = не поднимать.
    healthcheck_port: int = Field(default=8080)
    # Только для локальных прогонов БЕЗ alembic: создать таблицы через SQLAlchemy.
    # В продакшене (Railway) схема создаётся миграциями: `alembic upgrade head`.
    create_all: bool = False

    # ------------------------------------------------------------- telegram
    telegram_bot_token: str = ""
    telegram_admin_ids: str = ""  # "123,456" — для админ-команд/диагностики
    tg_rate_per_sec: int = 25  # лимит Telegram ~30 msg/sec, держим запас
    tg_batch_size: int = 20
    tg_batch_pause_sec: float = 1.0
    tg_send_retries: int = 3
    # Сообщения шлём всем активным подписчикам; при большом числе — держим
    # небольшой параллелизм, чтобы не упереться в flood limit.
    tg_parallelism: int = 5

    # -------------------------------------------------------------- database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/betsignals"
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_connect_timeout_sec: int = 10

    # ------------------------------------------------------------- openrouter
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_referer: str = "https://github.com/betsignals"
    openrouter_title: str = "BetSignals"
    # ID проверены по https://openrouter.ai/api/v1/models (2026-10-08).
    screener_model: str = "deepseek/deepseek-v4-flash"       # дёшево: ~$0.01–0.09 / 1M in
    analyzer_model: str = "google/gemini-3.8-flash"          # ~$0.375–0.75 / 1M in
    judge_model: str = "anthropic/claude-sonnet-5.5"         # ~$2 / 1M in
    judge_enabled: bool = True
    judge_timeout_sec: int = 60
    llm_concurrency: int = 3
    llm_max_retries: int = 1            # 1 ретрай при невалидном ответе (как в ТЗ)
    llm_http_retries: int = 3           # сетевые ретраи (tenacity)
    llm_timeout_sec: int = 90           # общий таймаут запроса в OpenRouter
    llm_screener_timeout_sec: int = 30
    llm_analyzer_timeout_sec: int = 90
    llm_max_tokens_screener: int = 400
    llm_max_tokens_analyzer: int = 2500
    llm_max_tokens_judge: int = 1500
    llm_temperature_screener: float = 0.1
    llm_temperature_analyzer: float = 0.2
    llm_temperature_judge: float = 0.1
    # Требовать строгий JSON через response_format (OpenRouter: json_object).
    # Если провайдер модели не поддерживает — код автоматически повторит без него.
    llm_use_json_mode: bool = True

    # ----------------------------------------------------- external data keys
    rapidapi_key: str = ""
    rapidapi_host_apifootball: str = "api-football-v1.p.rapidapi.com"
    rapidapi_host_apibasketball: str = "api-basketball.p.rapidapi.com"
    rapidapi_host_apitennis: str = "api-tennis-api.p.rapidapi.com"
    # Базовые URL RapidAPI-обёрток (можно переопределить, если на вашем тарифе другой хост).
    api_basketball_base_url: str = "https://api-basketball.p.rapidapi.com"
    api_tennis_base_url: str = "https://api-tennis-api.p.rapidapi.com"
    the_odds_api_key: str = ""
    the_odds_api_base: str = "https://api.the-odds-api.com/v4"
    # Free tier = 500 запросов/мес. НЕ используем для регулярного поллинга:
    # включается только как fallback №2 и не чаще quota-лимита (см. theoddsapi.py).
    the_odds_api_enabled: bool = False
    the_odds_api_monthly_limit: int = 450  # держим запас к 500

    # ---------------------------------------------------- источник линии (dev)
    # Реальные эндпоинты букмекеров живут ТОЛЬКО здесь. Пусто → провайдер
    # помечается недоступным (OddsAggregator работает на остальных источниках).
    winline_api_base: str = ""
    winline_live_api_base: str = ""
    betboom_api_base: str = ""
    betboom_live_api_base: str = ""
    # Дополнительные query-параметры, которые вы увидите в DevTools (sport/league id).
    # Формат JSON-объекта, напр: {"sport_id": "1", "lang": "ru"}
    winline_prematch_params: str = ""
    winline_live_params: str = ""
    betboom_prematch_params: str = ""
    betboom_live_params: str = ""

    # ------------------------------------------------------------- free APIs
    liquipedia_user_agent: str = "BetSignalsBot/1.0 (contact: you@example.com)"
    liquipedia_api_url: str = "https://api.liquipedia.net/api/v3/query"
    liquipedia_api_key: str = ""  # опционально: v3 может требовать ключ, см. SETUP.md
    opendota_api_base: str = "https://api.opendota.com/api"
    nhl_api_base: str = "https://api-web.nhle.com/v1"
    moneypuck_base_url: str = "https://moneypuck.com/moneypuck/playerData"
    understat_base_url: str = "https://understat.com"
    balldontlie_base: str = "https://api.balldontlie.io/v1"
    balldontlie_api_key: str = ""
    # RSS бокс/MMA (бесплатные ленты, ключ не нужен)
    rss_feeds: str = "https://www.championat.com/rss/news/,https://www.sports.ru/rss/all.xml"

    # ------------------------------------------------------------ http layer
    http_timeout_sec: float = 15.0
    http_connect_timeout_sec: float = 8.0
    http_max_retries: int = 3
    http_backoff_base_sec: float = 1.0
    http_backoff_max_sec: float = 20.0
    # Скрейпинг букмекеров: пауза между запросами и ротация User-Agent.
    scrape_delay_min_sec: float = 2.0
    scrape_delay_max_sec: float = 4.0
    scrape_user_agents: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36,"
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15,"
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )

    # -------------------------------------------------------------- кэш TTL
    cache_ttl_form_sec: int = 6 * 3600
    cache_ttl_h2h_sec: int = 24 * 3600
    cache_ttl_injuries_sec: int = 2 * 3600
    cache_ttl_lineups_sec: int = 45 * 60
    cache_ttl_xg_sec: int = 12 * 3600
    cache_ttl_liquipedia_sec: int = 12 * 3600
    cache_ttl_esports_sec: int = 10 * 60

    # ------------------------------------------------------- screener (Ур.0)
    odds_min: float = 1.55
    odds_max: float = 4.0
    min_recent_matches: int = 5          # меньше → data_quality="weak"
    whitelist_enabled: bool = True
    league_whitelist_json: str = ""      # JSON: переопределяет DEFAULT_LEAGUE_WHITELIST
    esports_tier1_only: bool = True
    esports_allowed_tiers: str = "S-Tier,Tier 1,Major,Tier1"
    max_suspicious_divergence: float = 0.10  # >10% (относительное) расхождение implied → suspicious
    suspicious_min_abs_diff: float = 0.02   # абсолютный минимум расхождения, чтобы не «шуметь» на малых вероятностях

    # ---------------------------------------------------------------- models
    score_grid_max_goals: int = 7        # матрица счётов Пуассона 0..7
    football_form_matches: int = 8       # последние N матчей для xG-модели
    hockey_form_matches: int = 8
    hockey_home_advantage: float = 0.10  # в xGoals
    basketball_home_advantage: float = 2.5
    basketball_total_sigma: float = 14.0
    basketball_margin_sigma: float = 12.0
    basketball_possessions_per_48: float = 100.0
    elo_start: float = 1500.0
    elo_k: float = 32.0
    elo_home_advantage: float = 0.0     # в киберспорте «дом» = сторона/пик, по умолчанию нет
    elo_margin_scale: float = 8.0       # раунды CS2: margin = (p−0.5)×2×scale
    elo_margin_sigma: float = 8.0

    # --------------------------------------------------------- value engine
    value_threshold: float = 0.06
    value_threshold_live: float = 0.09
    confidence_min_score: float = 60.0
    kelly_fraction: float = 0.25
    stake_cap_pct: float = 3.0
    stake_round_step: float = 0.25
    max_signals_per_match: int = 2
    # Веса composite-уверенности (Модуль 5)
    conf_w_edge: float = 0.45
    conf_w_llm: float = 0.35
    conf_w_data: float = 0.20
    conf_edge_full_credit: float = 0.15
    conf_clamp_min: float = 30.0
    conf_clamp_max: float = 95.0
    data_score_ok: float = 1.0
    data_score_weak: float = 0.5

    # ------------------------------------------------------------- ensemble
    w_llm_init: float = 0.6
    w_stat_init: float = 0.4
    weight_clamp_min: float = 0.3
    weight_clamp_max: float = 0.7
    sigma_inflation: float = 1.1          # sigma_final = max(sigma) * 1.1

    # ----------------------------------------------------- two-pass prematch
    pass1_hours_before: float = 6.0
    pass2_minutes_before: float = 90.0
    late_match_hours: float = 2.0         # матч появился позже → один проход
    pass_window_minutes: float = 15.0     # допуск окна T-6h / T-90m
    pass1_max_matches: int = 120          # защита от лавины LLM-запросов за проход

    # ------------------------------------------------------------ live eSports
    live_poll_seconds: int = 45
    live_enabled: bool = True
    live_esports_sports: str = "dota2,cs2"
    live_dota_window1_enabled: bool = True     # после драфта
    live_dota_window2_from_min: int = 8
    live_dota_window2_to_min: int = 12
    live_dota_max_minute: int = 20             # после 20-й минуты сигналы запрещены
    live_cs2_window2_from_round: int = 6
    live_cs2_window2_to_round: int = 10
    live_min_favourite_odds: float = 1.35      # не сигналить на явного фаворита
    live_max_signals_per_match: int = 2
    odds_movement_window_min: int = 10
    odds_movement_min_delta: float = 0.05      # |Δ кэфа| для market_moves
    odds_movement_neutral_threshold: float = 0.35  # |счёт| ниже → счёт «нейтральный»
    live_llm_judge_short_package: bool = True

    # --------------------------------------------------------- odds snapshot
    odds_refresh_minutes: int = 30
    odds_snapshot_min_delta: float = 0.0   # писать ли новый снимок при любой дельте

    # ------------------------------------------------------------- learning
    calibration_min_sample: int = 10
    calibration_window_days: int = 60
    calibration_clamp_min: float = 0.75
    calibration_clamp_max: float = 1.15
    brier_window: int = 100
    self_review_min_signals: int = 15
    self_review_weekday: int = 6  # 6 = воскресенье (APScheduler: mon=0)

    # --------------------------------------------------- results / tracking
    results_settle_after_hours: int = 2
    daily_digest_enabled: bool = True

    # ---------------------------------------------------------- scheduler TZ
    collect_hours: str = "6,14"           # сбор расписания
    calibration_hour: int = 4
    self_review_hour: int = 5
    digest_hour: int = 23
    digest_minute: int = 59

    # -------------------------------------------------------------- derived
    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        """Railway отдаёт postgresql:// — SQLAlchemy async нужен драйвер asyncpg."""
        if v.startswith("postgres://"):
            v = "postgresql+asyncpg://" + v[len("postgres://"):]
        elif v.startswith("postgresql://"):
            v = "postgresql+asyncpg://" + v[len("postgresql://"):]
        return v

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        return v.upper()

    # ------------------------------------------------------------- helpers
    @property
    def admin_ids(self) -> list[int]:
        out: list[int] = []
        for chunk in self.telegram_admin_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.isdigit():
                out.append(int(chunk))
        return out

    @property
    def user_agents(self) -> list[str]:
        return [ua.strip() for ua in self.scrape_user_agents.split(",") if ua.strip()]

    @property
    def rss_feed_list(self) -> list[str]:
        return [u.strip() for u in self.rss_feeds.split(",") if u.strip()]

    @property
    def allowed_esports_tiers(self) -> list[str]:
        return [t.strip().lower() for t in self.esports_allowed_tiers.split(",") if t.strip()]

    @property
    def league_whitelist(self) -> dict[str, list[str]]:
        if self.league_whitelist_json.strip():
            try:
                parsed = json.loads(self.league_whitelist_json)
                if isinstance(parsed, dict):
                    return {str(k): [str(x).lower() for x in v] for k, v in parsed.items()}
            except json.JSONDecodeError:
                # Не роняем сервис из-за плохого JSON — используем дефолт и жалуемся в лог.
                from loguru import logger

                logger.error("LEAGUE_WHITELIST_JSON не является валидным JSON — использую дефолтный whitelist")
        return {k: list(v) for k, v in DEFAULT_LEAGUE_WHITELIST.items()}

    def whitelist_for(self, sport: str) -> list[str]:
        """Ключи whitelist для спорта. Киберспорт хранится под ключами esports_*,
        а в матчах sport = "cs2"/"dota2" — поэтому нужен маппинг (иначе киберспорт
        никогда не проходил бы скринер)."""
        whitelist = self.league_whitelist
        if sport in whitelist:
            return whitelist[sport]
        return whitelist.get(f"esports_{sport}", [])

    @property
    def whitelist_all(self) -> list[str]:
        out: list[str] = []
        for values in self.league_whitelist.values():
            out.extend(values)
        return out

    def extra_params(self, raw: str) -> dict[str, str]:
        """Парсит JSON-строку доп. query-параметров (из DevTools)."""
        raw = (raw or "").strip()
        if not raw:
            return {}
        try:
            parsed: Any = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(k): str(v) for k, v in parsed.items()}

    @property
    def odds_providers_configured(self) -> list[str]:
        """Источники коэффициентов, которые настроены (та же логика, что в провайдерах)."""
        configured: list[str] = []
        if self.winline_api_base or self.winline_live_api_base:
            configured.append("winline")
        if self.betboom_api_base or self.betboom_live_api_base:
            configured.append("betboom")
        if self.the_odds_api_key and self.the_odds_api_enabled:
            configured.append("theoddsapi")
        return configured


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Единый на весь процесс объект настроек."""
    return Settings()


settings = get_settings()
