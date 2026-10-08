# BetSignals

Сервис поиска «value»-ставок: собирает события из бесплатных источников, прогоняет их
через каскад дешёвых LLM + статистические модели, отправляет кандидатов финальному
арбитру (Claude), а подтверждённые сигналы рассылает подписчикам в Telegram и ведёт
учёт результатов в своей базе.

Один долгоживущий процесс на Railway: `python -m app.main` = Telegram-бот (aiogram 3) +
APScheduler (все задачи) + лайв-воркер киберспорта + healthcheck (`/health`).

> ⚠️ **Дисклеймер.** Это инструмент для анализа, а не гарантия прибыли. Ставки — это
> риск: любой сигнал может проиграть. Ставьте только те деньги, которые готовы
> потерять, и соблюдайте законы своей страны. Размер ставки в сигнале — это доля
> банка, рассчитанная дробным Келли с жёстким кэпом (3%), а не рекомендация играть
> на всё.

---

## Как это работает

```
150 матчей/день
   │
   ├─[L0] ДЕТЕРМИНИРОВАННЫЙ СКРИНЕР (код, бесплатно)  ................ ≈50 матчей
   │      · белый список лиг (5 условий) · кэф в [1.55, 4.0] · есть статистика
   │      · не «suspicious» линия · нет анализа на текущем проходе
   │
   ├─[L1] LLM-СКРИНЕР (SCREENER_MODEL, ~400 токенов)  ................ ≈30 матчей
   │      · «стоит ли тратить анализ?» · строгий JSON по pydantic-схеме
   │
   ├─[L2] СТАТ-МОДЕЛИ + LLM-АНАЛИТИК
   │      · Пуассон (xG Understat / xGoals MoneyPuck): матрица счёта 0..7
   │      · Эло (per-team, per-map для CS2) · баскетбол (pace/ORtg/DRtg, HCA 2.5)
   │      · ANALYZER_MODEL: вероятности 1X2 (Σ = 1 ± 0.02), факторы, риски
   │      · ансамбль: w_llm 0.6 / w_stat 0.4 → пересчёт по Brier score
   │
   ├─ VALUE ENGINE  ................................................... 5–10 кандидатов
   │      · implied-вероятность из ЛУЧШЕЙ цены после удаления маржи
   │      · edge = p_final − implied · тоталы/форы — через гаусс/матрицу · Келли 0.25
   │      · score = 100 × (0.45×min(edge/0.15,1) + 0.35×llm_conf + 0.20×data) × калибровка
   │
   ├─[L3] АРБИТР (JUDGE_MODEL = Claude)  ............................. подтверждённые
   │      · verdict: confirm / reject / adjust · edge пересчитывается при adjust
   │      · reject/adjust обязаны иметь red_flags · таймаут 60 c → решение без арбитра
   │
   ├─ PostgreSQL (16 таблиц) → Telegram (шаблонное сообщение, антифлуд, ≤25 msg/s)
   │
   └─ РЕЗУЛЬТАТЫ → ОБУЧЕНИЕ → обратно в L2
          · трекер каждый час: WON/LOST/VOID, ROI
          · калибровка по спорту/лиге/рынку (≥10 сигналов / 60 дней, clamp 0.75–1.15)
          · веса ансамбля по Brier (окно 100 матчей, clamp 0.3–0.7)
          · недельный self-review → league_insights в промпты анализатора
```

Правила, по которым сигнал попадает в Telegram: `edge ≥ 0.06` (в лайве `≥ 0.09`),
`score ≥ 60`, `data_quality ≠ weak`, кэф в `[1.55, 4.0]`, линия не `suspicious`,
не более 2 сигналов на матч и не более одного на «рынок + исход».

---

## Быстрый старт

Полная инструкция для новичка (со всеми кнопками и путями) — **[SETUP.md](SETUP.md)**.
Коротко:

```bash
cp .env.example .env          # заполняем TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, DATABASE_URL
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

alembic upgrade head          # схема БД (16 таблиц + alembic_version)
python scripts/check_models.py     # проверить модели OpenRouter и ключ
python scripts/check_sources.py    # проверить источники (что включено и что отвечает)
python -m app.main                 # запуск: бот + планировщик + /health
```

Проверка здоровья: `curl -s localhost:8080/health | python -m json.tool`.

---

## Структура проекта

```
.
├── app/
│   ├── main.py                  # точка входа: логи, preflight, /health, polling, graceful shutdown
│   ├── config.py                # 161 настройка (все пороги/лимиты/таймауты) + белый список лиг
│   ├── scheduler.py             # APScheduler: 9 задач в одном процессе
│   ├── llm_client.py            # OpenRouter (OpenAI-совместимый): семафор, ретраи, pydantic
│   ├── db/
│   │   ├── models.py            # 16 таблиц SQLAlchemy 2.0 (JSONB для PostgreSQL)
│   │   └── database.py          # async engine, session_scope, ensure_sports, healthcheck
│   ├── sources/                 # 12 провайдеров + агрегатор + склейка названий
│   │   ├── base.py              # контракты: Odds/Stats/EsportsProvider, TTLCache, BaseHttpClient
│   │   ├── winline.py           # JSON-парсер букмекера (общий с BetBoom), URL — только из .env
│   │   ├── betboom.py           # fallback №1 по кэфам
│   │   ├── theoddsapi.py        # внешний агрегатор (free-tier, выключен по умолчанию)
│   │   ├── apifootball.py       # травмы, составы, форма, H2H (RapidAPI)
│   │   ├── understat.py         # xG футбол (+ кэш xg_cache в БД)
│   │   ├── nhl.py               # xGoals, форма, вероятный вратарь, календарь (официальный API)
│   │   ├── moneypuck.py         # CSV-фолбэк по хоккею
│   │   ├── apibasketball.py     # RapidAPI: игры, pace/ORtg/DRtg
│   │   ├── balldontlie.py       # бесплатный баскетбольный фолбэк
│   │   ├── apitennis.py         # теннис (пути эндпоинтов подбираются, см. честную пометку в файле)
│   │   ├── mma_rss.py           # MMA/боксинг: RSS + LLM (без выдуманных статструктур)
│   │   ├── opendota.py          # Dota 2: лайв-матчи, драфт, статистика карт
│   │   ├── liquipedia.py        # CS2/Dota турниры и tier (нужен User-Agent с контактом)
│   │   ├── team_matching.py     # алиасы + транслитерация + rapidfuzz (порог 85)
│   │   └── odds_aggregator.py   # медиана implied, лучшая цена, флаг suspicious, снимки
│   ├── stats_models/
│   │   ├── base.py              # StatPrediction, Φ, обратная Φ, pmf, хелперы
│   │   ├── poisson.py           # атака/защита × средние лиги, матрица счёта, тоталы/форы
│   │   ├── elo.py               # Эло (1500/K=32), отдельные рейтинги по картам CS2
│   │   └── basketball.py        # pace/ORtg/DRtg, HCA 2.5, σ тотала/маржи
│   ├── pipeline/
│   │   ├── screener.py          # L0: 5 условий + причина отказа (в лог и в /health)
│   │   ├── llm_screener.py      # L1
│   │   ├── analyzer.py          # L2: сбор данных (кэш/TTL), промпт, ансамбль
│   │   ├── llm_prompts.py       # все системные промпты и сборщики user-промптов
│   │   ├── value_engine.py      # implied, гаусс-тоталы, форы, двойной шанс, Келли, сигналы
│   │   ├── confidence.py        # композитный балл 0..100 с калибровкой и поправкой арбитра
│   │   ├── ensemble.py          # смешивание вероятностей LLM и стат-модели
│   │   ├── judge.py             # L3: пакет для арбитра, verdict, пересчёт edge
│   │   └── collector.py         # сбор расписания/кэфов, upsert_match, refresh, провайдеры
│   ├── live/
│   │   ├── esports_worker.py    # Dota: драфт/8–12 мин (стоп после 20); CS2: раунды 6–10
│   │   └── odds_movement.py     # движение линии, флаг market_moves
│   ├── learning/
│   │   ├── calibration.py       # калибровка по спорту/лиге/рынку, веса по Brier
│   │   └── self_review.py       # недельный разбор → league_insights
│   ├── tracking/
│   │   └── results_tracker.py   # WON/LOST/VOID (push = void), ROI, разрез по спорту
│   └── bot/
│       ├── bot.py               # Bot/Dispatcher, антифлуд-мидлварь, notify_admins
│       ├── handlers.py          # /start /stop /signals /stats /help
│       └── broadcaster.py       # шаблон сообщения, пачки, user_deliveries, дайджест
├── alembic/                     # миграция 0001_initial_schema (16 таблиц)
├── scripts/
│   ├── check_models.py          # сверка моделей OpenRouter + пробные вызовы + проверка схемы арбитра
│   └── check_sources.py         # диагностика источников и БД
├── tests/                       # 109 тестов в 11 файлах (формулы, парсер, склейка, контур)
├── .env.example                 # все 161 переменных с комментариями
├── SETUP.md                     # установка для новичка (ключи, DevTools, Railway)
├── Procfile / railway.json      # деплой одним web-процессом
└── requirements*.txt
```

---

## Источники данных

| Источник | Что даёт | Что нужно от вас |
|---|---|---|
| Winline | кэфы прематч + лайв | **URL из DevTools** → `WINLINE_API_BASE`, `WINLINE_LIVE_API_BASE` |
| BetBoom | кэфы прематч + лайв | **URL из DevTools** → `BETBOOM_*` |
| TheOddsApi | кэфы (агрегатор) | ключ (free 500/мес), по умолчанию выключен |
| API-Football | травмы, составы, форма, H2H (футбол) | RapidAPI-ключ + Free Subscribe |
| Understat | xG по командам | ничего (бесплатно), + кэш 12 ч |
| NHL Stats API | xGoals, форма, вратари, календарь | ничего (официальный публичный API) |
| MoneyPuck | xGoals (CSV) | ничего |
| API-Basketball | игры, pace/ORtg/DRtg | RapidAPI-ключ + Free Subscribe |
| balldontlie | баскетбол-фолбэк | ключ по желанию |
| API-Tennis | матчи, форма, H2H | RapidAPI-ключ + Free Subscribe |
| MMA RSS (Чемпионат/Спортс) | новости MMA/боксинга → LLM | ничего (можно заменить `RSS_FEEDS`) |
| OpenDota | Dota 2: лайв, драфт, карты | ничего |
| Liquipedia | CS2/Dota турниры и tier | `LIQUIPEDIA_USER_AGENT` с реальной почтой |

Отказ любого источника изолирован: он логируется, а данные помечаются
`data_quality="weak"` — сигнал по такому матчу не отправляется. Ни одна ошибка сети
или LLM не роняет сервис.

---

## База данных (16 таблиц)

| Таблица | Зачем |
|---|---|
| `users` | подписчики (/start,/stop), антифлуд-состояние |
| `sports` | справочник видов спорта (заполняется при старте) |
| `teams`, `team_aliases` | команды и их написания у разных источников |
| `matches` | события: лига, время, статус, `external_refs` (id в каждом источнике) |
| `odds` | **append-only** снимки кэфов со временем (движение линии) |
| `xg_cache` | кэш xG по команде/сезону (защита free-tier) |
| `elo_ratings` | Эло по команде (и по карте CS2) |
| `analyses` | ответ LLM-анализатора (`parsed` JSONB) + токены/латентность |
| `stat_predictions` | прогноз стат-модели (`parsed` JSONB) |
| `signals` | сигнал: рынок, исход, линия, кэф, edge, score, ставка, статус, обоснование |
| `judge_verdicts` | вердикты арбитра: confirm/reject/adjust/timeout, red_flags |
| `user_deliveries` | журнал доставки (ok/blocked/failed) на каждого пользователя |
| `league_calibration` | калибровка по (спорт, лига, рынок) |
| `ensemble_weights` | история весов ансамбля |
| `league_insights` | выводы недельного self-review (в промпт анализатора) |

Схема создаётся миграцией: `alembic upgrade head` (проверено: 17 таблиц, `alembic check`
чист, `downgrade base` работает).

---

## Расписание (TZ = `Europe/Moscow`)

| Когда | Задача | Что делает |
|---|---|---|
| 06:00, 14:00 | `collect_schedule` | полный сбор расписания на день |
| каждые 30 мин | `refresh_odds` | обновление кэфов по актуальным матчам |
| каждые 10 мин | `prematch_passes` | PASS 1 (T−6 ч) → кандидат; PASS 2 (T−90 мин) → подтверждение |
| каждые 45 с | `live_worker` | лайв киберспорта (Dota/CS2, окна по драфту/минутам/раундам) |
| ежечасно | `results` | закрытие сигналов, ROI, статистика |
| каждую минуту | `broadcast` | рассылка подтверждённых сигналов |
| 04:00 | `daily_learning` | калибровка + веса ансамбля |
| вс 05:00 | `self_review` | недельный самоанализ → `league_insights` |
| 23:59 | `daily_digest` | дневная сводка админам |

Матч, появившийся позже чем за 2 часа до старта, анализируется одним проходом
(`LATE_MATCH_HOURS`).

---

## Telegram

Команды: `/start` (подписка + показывает ваш ID), `/stop`, `/signals`, `/stats`,
`/help`. Рассылка идёт пачками с ограничением скорости, ретраями и журналом
доставки; заблокировавшие бота помечаются и пропускаются, остальные не страдают.

Сообщение сигнала содержит: спорт/лигу/матч и время, рынок и исход в человеческом
виде, кэф и лучший источник, вероятности (модель/implied), edge, уверенность 0–100,
долю банка, «Почему» (ключевые факторы), «Риски», вердикт арбитра и подпись сервиса.

---

## Диагностика и тесты

```bash
pytest -q                            # 109 тестов, без сети и без БД-сервера
python scripts/check_sources.py --db # источники + состояние БД
python scripts/check_models.py       # модели OpenRouter (сверка ID + пробные вызовы)
python -c "import app.main"          # проверка импортов/конфигурации
```

---

## Деплой

Railway, один **web**-сервис (НЕ cron): `Procfile` → `web: python -m app.main`,
`railway.json` → `healthcheckPath=/health`, добавьте плагин PostgreSQL (он подставит
`DATABASE_URL`), перенесите `.env` в Variables, один раз выполните `alembic upgrade head`
в Shell сервиса. Подробно — [SETUP.md, шаг 9](SETUP.md#9-деплой-на-railway).

---

## Статус: что проверено, а что нужно проверить вам

**Проверено при разработке (2026-10-08):**

* схема БД: `alembic upgrade head` → 17 таблиц, `alembic check` → «No new upgrade
  operations detected», `downgrade base` — без ошибок;
* все модули импортируются, `python -m app.main` доходит до preflight;
* тесты формул и контура проходят: `109 passed` (`pytest -q`);
* ID моделей OpenRouter (`deepseek/deepseek-v4-flash`, `google/gemini-3.8-flash`,
  `anthropic/claude-sonnet-5.5`) существуют в каталоге и подставляются скриптом;
* `scripts/check_sources.py --offline` и `scripts/check_models.py --no-probe`
  корректно работают без сети.

**Требует вашей проверки (объективно нельзя проверить заранее):**

1. **URL Winline/BetBoom и вид их JSON** — достаются из DevTools (SETUP.md, шаг 6),
   проверяются `python scripts/check_sources.py --source winline`. Парсер построен на
   маппингах `FIELD_MAPPING`/`MARKET_KEYWORDS`/`SELECTION_KEYWORDS` в
   `app/sources/winline.py`; если сайт поменял имена полей — правится в одну строку,
   скрипт покажет, что именно не распозналось.
2. **Бесплатные лимиты** RapidAPI / TheOddsApi / Liquipedia / balldontlie — условия
   меняются; кэш (TTL в конфиге) и предохранитель `THE_ODDS_API_MONTHLY_LIMIT` снижают
   риск, но актуальные лимиты смотрите на сайтах.
3. **Пути эндпоинтов API-Tennis** — в файле `app/sources/apitennis.py` честно указано,
   что точные пути не подтверждены: провайдер пробует известные варианты и логирует,
   какой сработал.
4. **Стабильность разметки Understat** — парсер читает `window.__INITIAL_STATE__`;
   если Understat поменяет ключи, `check_sources.py` покажет «0 команд».
5. **Реальное качество сигналов** — сервис начинает собирать историю с нуля; ROI и
   винрейт смотрите в `/stats` через несколько недель. Калибровка и веса ансамбля
   настраиваются по накопленным данным автоматически.

Философия проекта: лучше честное «проверь это» со скриптом и логом, чем тихо
нерабочий код. Все места, где автор не мог проверить эндпоинт или лимит, помечены
в коде и в документации.

---

## Лицензия и использование

Код предоставляется «как есть» для личного использования. Убедитесь, что сбор данных
не нарушает правила сайтов-источников и законодательство вашей юрисдикции; задержки
между запросами (`SCRAPE_DELAY_MIN_SEC`) специально сделаны вежливыми. Автор не несёт
ответственности за финансовые потери.
