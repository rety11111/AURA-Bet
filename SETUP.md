# BetSignals — SETUP.md (пошаговая установка «с нуля»)

Документ рассчитан на человека, который **никогда не получал API-ключи**.
Все шаги — с точными путями, кнопками и командами для копирования.
Если что-то не сходится — сверяйтесь с разделом «14. Если что-то не работает».

> ⚠️ **Честно о том, что нельзя проверить заранее.** Часть источников BetSignals
> использует публичные JSON-API сайтов букмекеров (Winline/BetBoom). Их адреса
> зависят от региона/CDN и **намеренно не захардкожены в коде**: вы достаёте их
> сами через DevTools (шаг 6). Модели OpenRouter, наоборот, проверены 2026-10-08,
> и есть скрипт, который перепроверит их за вас (шаг 5).
>
> **Ставки — это риск.** Сервис не гарантирует прибыль. Не ставьте деньги,
> которые не готовы потерять.

---

## Содержание

1. [Что понадобится](#1-что-понадобится)
2. [Локальный запуск за 5 минут](#2-локальный-запуск-за-5-минут)
3. [Telegram-бот (токен и свой ID)](#3-telegram-бот-токен-и-свой-id)
4. [Ключи источников: RapidAPI, TheOddsApi, balldontlie, Liquipedia](#4-ключи-источников)
5. [Ключ OpenRouter и проверка моделей](#5-ключ-openrouter-и-проверка-моделей)
6. [Как достать реальные URL Winline и BetBoom (DevTools)](#6-как-достать-реальные-url-winline-и-betboom-devtools)
7. [База данных: локально и на Railway](#7-база-данных-локально-и-на-railway)
8. [Миграции Alembic](#8-миграции-alembic)
9. [Деплой на Railway](#9-деплой-на-railway)
10. [Проверка: диагностические скрипты](#10-проверка-диагностические-скрипты)
11. [Что происходит после старта (расписание)](#11-что-происходит-после-старта-расписание)
12. [Настройка порогов и лиг](#12-настройка-порогов-и-лиг)
13. [Словарь команд](#13-словарь-команд)
14. [Если что-то не работает](#14-если-что-то-не-работает)

---

## 1. Что понадобится

| Что | Где взять | Обязательно? |
|---|---|---|
| Python 3.12 (3.11 тоже работает) | https://www.python.org/downloads/ | да (для локального запуска) |
| Telegram-бот | @BotFather | да |
| Ключ OpenRouter | https://openrouter.ai/keys | да |
| PostgreSQL | Railway-плагин (шаг 7) или локальный Docker | да |
| Ключ RapidAPI | https://rapidapi.com | желательно (футбол/баскетбол/теннис: травмы, составы, форма) |
| Ключ TheOddsApi | https://the-odds-api.com | по желанию (500 запросов/мес бесплатно) |
| Ключ balldontlie | https://www.balldontlie.io | по желанию (баскетбольный фолбэк) |
| User-Agent с почтой для Liquipedia | своя почта | да, если нужны CS2/Dota-турниры |

Файл окружения: в корне проекта лежит `.env.example`. Скопируйте его в `.env` и
заполняйте по ходу шагов:

```bash
cp .env.example .env
```

> В `.env` **нельзя** добавлять пробелы вокруг `=` и кавычки: пишите `KEY=value`.

---

## 2. Локальный запуск за 5 минут

```bash
# 1. Зависимости (виртуальное окружение — обязательно)
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-dev.txt # для тестов

# 2. Проверить, что всё собрано корректно
pytest -q                          # ~109 тестов, все должны пройти

# 3. Создать таблицы (для локального старта можно без Alembic)
#    В .env задайте: CREATE_ALL=true (в продакшене так делать НЕ надо, см. шаг 8)

# 4. Запустить сервис
python -m app.main
```

Что вы увидите в логе при успешном старте:

```
preflight: все проверки пройдены
health: сервер слушает http://0.0.0.0:8080/health
health: бот @your_bot на связи
scheduler: создан, задач — 9
scheduler: collect_schedule | cron[hour='6,14', minute='0'] | next=...
...
BetSignals: стартую Telegram polling
```

Проверить здоровье сервиса:

```bash
curl -s localhost:8080/health | python -m json.tool
```

Ожидаемый ответ: `status: "ok"`, `db: true`, `models: {...}`, `providers: {...}`.
Если `status: "degraded"` и `db: false` — неверный `DATABASE_URL` (шаг 7).

---

## 3. Telegram-бот (токен и свой ID)

1. Откройте Telegram, найдите **@BotFather** (только официальный, с галочкой).
2. Отправьте `/newbot` → введите **имя** бота (любое, например `BetSignals`) →
   введите **username**, который заканчивается на `bot` (например `betsignals_value_bot`).
3. BotFather пришлёт сообщение с токеном вида
   `7234567890:AAH...` и кнопкой «Use this token to access the HTTP API».
   Скопируйте **всю строку с двоеточием** — это `TELEGRAM_BOT_TOKEN`.

```
TELEGRAM_BOT_TOKEN=7234567890:AAH...
```

4. **Узнайте свой ID**: отправьте вашему новому боту команду `/start`.
   Бот ответит строкой «Ваш Telegram ID: `123456789`» — это и есть ваш ID.

```
TELEGRAM_ADMIN_IDS=123456789
```

Альтернатива, если бот ещё не запущен: написать боту **@userinfobot** — он вернёт
ваш `Id`. Несколько админов — через запятую: `123,456`.

5. Больше ничего создавать не нужно: сервис сам регистрирует всех, кто нажал
   `/start`, и рассылает им сигналы. `/stop` — отписка.

---

## 4. Ключи источников

### 4.1 RapidAPI (API-Football + API-Basketball + API-Tennis)

Ключ **один**, но подписку «Free» надо оформить **отдельно на каждое API**.

1. Зайдите на https://rapidapi.com и нажмите **Sign Up** (можно через Google).
2. В строке поиска найдите **API-Football** → откройте страницу API → справа
   нажмите **Subscribe to Test** → выберите план **Basic / Free** → **Subscribe**.
3. Повторите то же для **API-Basketball** и **API-Tennis** (поиск → страница API →
   **Subscribe to Test** → **Basic/Free** → **Subscribe**).
4. Возьмите ключ: вверху справа аватар → **Apps** → вкладка **Security** →
   поле **Application Key** (строка вида `1a2b3c...`). У всех трёх API он один и тот же,
   потому что ключ привязан к вашему приложению (Application), а не к API.

```
RAPIDAPI_KEY=1a2b3c...
```

> Хосты (`RAPIDAPI_HOST_APIFOOTBALL` и т.д.) уже заполнены в `.env.example`.
> Если RapidAPI поменяет хост — на странице API есть блок **Endpoints** с полным
> URL запроса: скопируйте домен оттуда в переменную.

### 4.2 TheOddsApi (агрегатор кэфов, 500 запросов/мес бесплатно)

1. https://the-odds-api.com → **Get API Key** → регистрация → ключ на странице
   **Dashboard**.
2. Впишите ключ и **включите** источник (по умолчанию он выключен, чтобы не тратить лимит):

```
THE_ODDS_API_KEY=ваш-ключ
THE_ODDS_API_ENABLED=true
```

> Остаток лимита виден в ответах API (заголовки `x-requests-remaining`) и в логе.
> Включённый источник держите только когда он реально нужен: за месяц 500 запросов
> при опросе каждые 30 минут уходят очень быстро. Локальный предохранитель —
> `THE_ODDS_API_MONTHLY_LIMIT=450`.

### 4.3 balldontlie (баскетбол, бесплатно)

1. https://www.balldontlie.io → **Sign Up** (или страница API на RapidAPI — BetSignals
   использует прямой домен `api.balldontlie.io`).
2. Скопируйте ключ из личного кабинета:

```
BALLDONTLIE_API_KEY=...
```

Без ключа запросы тоже работают, но лимит ~5 запросов/мин; ключ его поднимает.

### 4.4 Liquipedia (CS2/Dota 2 турниры и tier)

Liquipedia **требует честный User-Agent с контактом**. Формат строго такой:

```
LIQUIPEDIA_USER_AGENT=BetSignalsBot/1.0 (contact: your@email.com)
```

Замените `your@email.com` на свою почту. Если оставить `example.com`, провайдер
специально отключается (в коде это предусмотрено, чтобы вы не получили
«тихо нерабочий» источник).

Ключ `LIQUIPEDIA_API_KEY` — необязателен (поднимает лимиты, берётся на
https://liquipedia.net/api), но требует подтверждения заявки.

---

## 5. Ключ OpenRouter и проверка моделей

OpenRouter — это единый шлюз к моделям (OpenAI-совместимый API), поэтому в коде
используется обычный `openai`-клиент с `base_url=https://openrouter.ai/api/v1`.

1. https://openrouter.ai → **Sign in** (Google/GitHub/email).
2. Пополните баланс (кнопка **Credits** в меню слева; минимальный платёж ~$5,
   комиссия платёжной системы ≈5.5%). На дешёвых моделях этого хватает на месяцы.
3. Ключ: https://openrouter.ai/keys → **Create Key** → впишите имя (`betsignals`) →
   **Create**. Скопируйте строку `sk-or-v1-...` (показывается один раз).

```
OPENROUTER_API_KEY=sk-or-v1-...
```

4. **Проверьте модели** (ID проверены 2026-10-08, но каталог меняется):

```bash
python scripts/check_models.py
```

Скрипт:
- скачает живой каталог `https://openrouter.ai/api/v1/models` и сверит ID
  `SCREENER_MODEL` / `ANALYZER_MODEL` / `JUDGE_MODEL`; для отсутствующих предложит
  похожие ID;
- сделает по одному крошечному пробному запросу в каждую модель (проверит ключ и
  JSON-режим) и напечатает расход токенов;
- проверит, что арбитр отвечает по pydantic-схеме проекта.

Если хочется проверить вручную (без скрипта), вот он же одной командой:

```bash
python - <<'PY'
import httpx, json
data = httpx.get("https://openrouter.ai/api/v1/models", timeout=30).json()["data"]
for m in data:
    if m["id"] in {"deepseek/deepseek-v4-flash", "google/gemini-3.8-flash", "anthropic/claude-sonnet-5.5"}:
        print(m["id"], "→", m["pricing"])
PY
```

Управлять моделями можно двумя способами:

* **Дешевле** (`SCREENER_MODEL=deepseek/deepseek-v4-flash`);
* **Качественнее** — подставьте любую строку-`id` из https://openrouter.ai/models
  в `SCREENER_MODEL` / `ANALYZER_MODEL` / `JUDGE_MODEL`.

`JUDGE_ENABLED=false` — режим «без арбитра»: сигналы подтверждаются сами (по ТЗ
такой режим допустим). `JUDGE_TIMEOUT_SEC=60` — если арбитр не ответил за минуту,
решение принимается без него (по лайву — с укороченным пакетом).

---

## 6. Как достать реальные URL Winline и BetBoom (DevTools)

Winline и BetBoom отдают данные собственному сайту через JSON-запросы. Мы делаем
ровно такие же запросы из кода. Адреса **не захардкожены**: их нужно один раз
посмотреть в браузере и вписать в `.env`.

### 6.1 Прематч-URL (на примере Winline)

1. Откройте в **Chrome/Edge** сайт Winline (например `https://winline.ru`).
   Авторизуйтесь, если требуется.
2. Нажмите **F12** (или правой кнопкой → «Просмотреть код») → вкладка **Network**
   (в русской версии — «Сеть»).
3. Над списком запросов нажмите фильтр **Fetch/XHR**.
4. **Перезагрузите страницу** (F5) и выберите в левом меню вид спорта, например
   «Футбол» → «Сегодня». Список запросов обновится.
5. Просмотрите запросы по одному: нажимайте на запрос и смотрите вкладку **Preview/Response**.
   Ищите ответ, в котором есть список событий: массив с командами и кэфами
   (ключи вида `events`, `matches`, `data`; внутри — названия команд и `price`/`coef`).
6. Когда нашли нужный запрос: правой кнопкой по нему → **Copy** → **Copy link address**
   (копируется полный URL с параметрами).
7. Возьмите из него **только часть до `?`** — это и есть базовый URL:

```
WINLINE_API_BASE=https://winline.ru/api/v2/events   (пример формы, у вас будет свой!)
```

8. Параметры запроса (всё, что было после `?`) **не выбрасывайте**: посмотрите,
   какие там ключи. Полезные (например `sport_id`, `lng`, `period`) внесите JSON-объектом:

```
WINLINE_PREMATCH_PARAMS={"sport_id": "1", "lng": "ru"}
WINLINE_LIVE_PARAMS={"lng": "ru"}
```

   Код автоматически добавит в запрос `date=YYYY-MM-DD`, если вы не указали его сами.

### 6.2 Live-URL

1. Там же, на сайте, перейдите в раздел **Live** (живые ставки).
2. В DevTools (Network → Fetch/XHR) нажмите **Clear** (🚫 в левом верхнем углу
   панели) и подождите ~10 секунд: список обновится сам (лайв-счёт меняется).
3. Найдите запрос, в ответе которого есть счёт в реальном времени и кэфы —
   это live-URL. Скопируйте его как в пункте 6.1:

```
WINLINE_LIVE_API_BASE=https://winline.ru/api/v2/live   (пример формы!)
```

### 6.3 BetBoom

Повторите шаги 6.1–6.2 на сайте `https://betboom.ru` (тот же алгоритм: F12 →
Network → Fetch/XHR → ищем JSON со списком событий → Copy link address):

```
BETBOOM_API_BASE=...
BETBOOM_LIVE_API_BASE=...
BETBOOM_PREMATCH_PARAMS={"lng": "ru"}
BETBOOM_LIVE_PARAMS={"lng": "ru"}
```

### 6.4 Проверка, что маппинги совпали

Парсер устроен эвристически: он ищет типовые имена полей (`events`/`matches`,
`price`/`coef`/`kf`, `Ф1`/`П1`/`W1` и т.п.). Проверить, что ваш реальный JSON
разобрался, можно так:

```bash
python scripts/check_sources.py --source winline
```

Скрипт напечатает, сколько событий и кэфов удалось разобрать. Если выводит
`событий = 0`:

* откройте `app/sources/winline.py` и посмотрите константы `FIELD_MAPPING`,
  `MARKET_KEYWORDS`, `SELECTION_KEYWORDS` — это и есть «словарь» распознавания;
* сравните имена полей в вашем JSON (вкладка Response в DevTools) со списками и
  **допишите своё имя ключа в соответствующий кортеж** (это правится в 1 строку);
* перезапустите `check_sources.py`.

> Юридически: вы делаете запросы к публичным API сайтов от своего имени. Не
> снижайте задержки (`SCRAPE_DELAY_MIN_SEC`) и не увеличивайте частоту опроса —
> это и вежливее, и безопаснее для вашего IP. Если сайт требует авторизацию —
> используйте только свои данные и соблюдайте его правила.

---

## 7. База данных: локально и на Railway

### 7.1 Локально (Docker, самый простой способ)

```bash
docker run --name betsignals-pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=betsignals -p 5432:5432 -d postgres:16
```

```
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/betsignals
```

### 7.2 На Railway

1. Создайте проект: https://railway.app → **New Project**.
2. В проекте нажмите **Create** → **Database** → **Add PostgreSQL**.
3. Railway создаёт базу и **сам подставляет переменную `DATABASE_URL`** в сервисы
   проекта (вкладка **Variables** у сервиса с вашим кодом).
4. Скопируйте значение в свой `.env` (локально) — или ничего не делайте при деплое,
   Railway подставит её сам. Код сам превращает `postgresql://` в
   `postgresql+asyncpg://`, поэтому значение можно брать «как есть».
5. Если ваш сервис не видит базу: **Variables** → **Add Reference** →
   выберите `PostgreSQL` → `DATABASE_URL`.

> На бесплатном плане база может «засыпать». Первый запрос после паузы может быть
> с задержкой — это нормально, сервис переживёт (есть ретраи и preflight).

---

## 8. Миграции Alembic

Схема (16 таблиц) создаётся миграцией, а не «на глаз»:

```bash
# укажите базу явно или возьмите её из .env
export DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/betsignals"

alembic upgrade head       # создать/обновить схему
alembic current            # какая версия применена сейчас
alembic check              # убедиться, что модели и схема совпадают
alembic downgrade base     # откатить всё (осторожно: удалит данные)
```

Ожидаемо: `alembic upgrade head` создаёт **17 таблиц** (16 наших + `alembic_version`):
`users, sports, teams, team_aliases, matches, odds, xg_cache, elo_ratings, analyses,
stat_predictions, signals, judge_verdicts, user_deliveries, league_calibration,
ensemble_weights, league_insights`.

* **В продакшене** (Railway) — только `alembic upgrade head`, `CREATE_ALL=false`.
* **Локально** можно поставить `CREATE_ALL=true` — сервис создаст таблицы из моделей
  при старте (так быстрее пробовать, но схему так вести нельзя).

На Railway выполнить миграцию можно из консоли сервиса (Railway → сервис →
**⋮** → **Open Shell**):

```bash
alembic upgrade head
```

Либо добавить в `railway.json`/`Procfile` пре-деплой шаг. Самый простой путь —
один раз открыть Shell и выполнить команду руками.

---

## 9. Деплой на Railway

В репозитории уже есть `Procfile` (`web: python -m app.main`) и `railway.json`
(сборка Nixpacks, `startCommand`, `restartPolicyType=ON_FAILURE`,
`healthcheckPath=/health`).

1. Залейте код в свой GitHub-репозиторий (приватный удобнее).
2. https://railway.app → **New Project** → **Deploy from GitHub repo** → выберите репозиторий.
3. В проект добавьте **PostgreSQL** (шаг 7.2).
4. У сервиса откройте вкладку **Variables** → **Raw Editor** → вставьте содержимое
   своего `.env` (можно целиком, Railway поймёт формат `KEY=value`).
   Обязательные переменные: `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY`,
   `DATABASE_URL` (её обычно подставляет плагин).
   Полезно для прода: `LOG_LEVEL=INFO`, `TZ=Europe/Moscow`.
5. **Settings** → **Networking** → **Generate Domain** (нужно для healthcheck).
   Порт указывать не надо: приложение слушает `$PORT`, который Railway передаёт сам.
6. **Deploy**. В логах (**Deployments** → **View Logs**) должно появиться то же,
   что и при локальном запуске: `preflight: все проверки пройдены`,
   `health: сервер слушает http://0.0.0.0:<PORT>/health`, `scheduler: запланировано ...`.
7. Проверка healthcheck: откройте `https://<ваш-домен>/health` — должен вернуться
   JSON со `"status": "ok"`.

> **Почему один процесс, а не cron.** APScheduler живёт внутри того же
> `python -m app.main`, что и бот (ТЗ требует именно так: cron-сервис не пережил бы
> состояние кэшей и лайв-воркера). На Railway выбирайте тип сервиса **Web**, не
> **Cron**.

---

## 10. Проверка: диагностические скрипты

```bash
# 1) Источники: что включено, что отвечает, что удалось распарсить
python scripts/check_sources.py                 # все включённые источники
python scripts/check_sources.py --offline       # «что включено» без сети
python scripts/check_sources.py --db            # + таблицы и счётчики строк в БД
python scripts/check_sources.py --source winline --source understat

# 2) Модели OpenRouter: сверка ID + пробный вызов каждой модели
python scripts/check_models.py
python scripts/check_models.py --no-probe       # только сверка ID (без трат)

# 3) Тесты (быстрые, без сети)
pytest -q
```

Читайте вывод так:

* `⏭️` — источник выключен (нет ключа/URL). Это нормально: сервис работает на остальных.
* `❌` — источник включён, но не ответил. Смотрите текст ошибки и раздел 14.
* `✅` — источник отвечает и данные парсятся.

---

## 11. Что происходит после старта (расписание)

Все времена — в часовом поясе `TZ` (по умолчанию `Europe/Moscow`):

| Когда (МСК) | Задача (`id`) | Что делает |
|---|---|---|
| 06:00 и 14:00 | `collect_schedule` | Полный сбор расписания на день (все источники) |
| каждые 30 минут | `refresh_odds` | Обновление кэфов по актуальным матчам |
| каждые 10 минут | `prematch_passes` | Два прохода: кандидат за 6 ч до старта и повторный анализ за 90 мин |
| каждые 45 секунд | `live_worker` | Лайв киберспорта (Dota 2/CS2): драфт, окна по минутам и раундам |
| ежечасно (:07) | `results` | Закрытие сигналов, пересчёт ROI/винрейта |
| каждую минуту | `broadcast` | Отправка подтверждённых сигналов подписчикам |
| 04:00 | `daily_learning` | Калибровка по спорту/лиге/рынку + веса ансамбля по Brier |
| воскресенье 05:00 | `self_review` | Самоанализ недели → `league_insights` (подсказки в промпт анализатора) |
| 23:59 | `daily_digest` | Дневной дайджест админам |

Контур целиком:

```
150 матчей/день → [L0 скринер, код] → ~50 → [L1 LLM-скринер] → ~30
   → [L2 стат-модели + LLM-аналитик] → Value Engine (edge/уверенность/ставка)
   → 5–10 кандидатов → [L3 арбитр Claude] → PostgreSQL → Telegram
   → результаты → обучение (калибровка/веса/инсайты) → обратно в L2
```

---

## 12. Настройка порогов и лиг

Все параметры — в `.env` (полный список с комментариями — в `.env.example`).
Что чаще всего крутят:

| Параметр | Смысл | Дефолт |
|---|---|---|
| `VALUE_THRESHOLD` | минимальный edge для прематча | `0.06` (6%) |
| `VALUE_THRESHOLD_LIVE` | минимальный edge в лайве | `0.09` |
| `CONFIDENCE_MIN_SCORE` | порог композитного балла | `60` |
| `ODDS_MIN` / `ODDS_MAX` | рабочий диапазон кэфов | `1.55` / `4.0` |
| `KELLY_FRACTION` / `STAKE_CAP_PCT` | доля Келли и кэп ставки | `0.25` / `3.0` |
| `MAX_SIGNALS_PER_MATCH` | сигналов на матч | `2` |
| `WHITELIST_ENABLED` | включён ли белый список лиг | `true` |
| `LEAGUE_WHITELIST_JSON` | свой список лиг (JSON) | — |
| `ESPORTS_TIER1_ONLY` | только топ-турниры CS2/Dota | `true` |
| `PASS1_HOURS_BEFORE` / `PASS2_MINUTES_BEFORE` | окна двух проходов | `6.0` / `90.0` |

Пример: оставить только РПЛ и АПЛ:

```
LEAGUE_WHITELIST_JSON={"football": ["premier league", "rpl", "премьер-лига"]}
```

Хотите больше сигналов — снижайте `CONFIDENCE_MIN_SCORE` (например до 55) и
`VALUE_THRESHOLD` (до 0.05). Хотите меньше, но качественнее — поднимайте их и
уменьшайте `ODDS_MAX`.

### Добавление алиасов команд

Названия команд у разных источников пишутся по-разному («Спартак Мск» ↔ «Spartak
Moscow»). Сервис сам сопоставляет их (транслитерация + rapidfuzz, порог 85) и
пишет в лог пары, которые **не** склеились:

```
team_matching: НЕраспознанная пара «ЦСКА» (apifootball) ≈ «CSKA Moscow» (score=60.0 < 85.0)
```

Если это одна и та же команда — добавьте алиас вручную в таблицу `team_aliases`
(через psql/Railway Query или свою миграцию данных):

```sql
-- В логе: «НЕраспознанная пара «ЦСКА» (apifootball) ≈ «CSKA Moscow» (score=60.0 < 85.0)».
-- Значит, команда уже есть в teams под именем «CSKA Moscow», а алиас «ЦСКА» от apifootball
-- нужно привязать вручную. Уникальность — по паре (alias, source).
INSERT INTO team_aliases (team_id, alias, source, created_at)
SELECT id, 'ЦСКА', 'apifootball', now()
FROM teams
WHERE canonical_name = 'CSKA Moscow'
ON CONFLICT (alias, source) DO NOTHING;
```

После вставки следующий проход склеит эти названия автоматически (кэш алиасов
читается из БД на каждом матче).

---

## 13. Словарь команд

```bash
pytest -q                              # тесты (без сети и БД-сервера)
python -m app.main                     # запуск сервиса (бот + планировщик + health)
alembic upgrade head                   # миграции
python scripts/check_models.py         # модели OpenRouter
python scripts/check_sources.py --db   # источники + состояние БД
```

Диагностика внутри Telegram (для админов из `TELEGRAM_ADMIN_IDS`):

* `/start`, `/stop` — подписка/отписка;
* `/signals` — последние 10 сигналов со статусами;
* `/stats` — винрейт, ROI, средний кэф/edge, разрез по спорту;
* `/help` — справка.

---

## 14. Если что-то не работает

**`preflight: БД недоступна` / `status: degraded`**
Проверьте `DATABASE_URL`. На Railway возьмите значение из плагина PostgreSQL
(Variables → Add Reference). Локально — что контейнер `postgres` поднят
(`docker ps`) и порт 5432 свободен.

**`alembic: Target database is not up to date`**
Сначала `alembic upgrade head` (на пустой базе), и только потом любые команды
`autogenerate`.

**`OPENROUTER_API_KEY пуст` или 401 на моделях**
Ключ создаётся на https://openrouter.ai/keys и начинается с `sk-or-v1-`. Проверьте,
что скопировали его целиком и что на балансе есть деньги (**Credits** → баланс > $0).
Запустите `python scripts/check_models.py --no-probe`, затем полный вариант.

**Модель «не найдена» / ошибка 404 по модели**
Каталог моделей меняется. `python scripts/check_models.py` покажет актуальные ID и
похожие варианты. Подставьте новый ID в `SCREENER_MODEL`/`ANALYZER_MODEL`/`JUDGE_MODEL`.

**RapidAPI отдаёт 403/429**
Скорее всего, вы не нажали **Subscribe to Test** на нужном API (подписка нужна
**для каждого** из API-Football / API-Basketball / API-Tennis) или исчерпали
бесплатную квоту на сутки. Проверьте `python scripts/check_sources.py`.

**Liquipedia 403 или «провайдер отключён»**
`LIQUIPEDIA_USER_AGENT` должен содержать реальный контакт:
`BetSignalsBot/1.0 (contact: your@email.com)`. Значение из примера
(`you@example.com`) намеренно блокируется кодом.

**Understat вернул 0 команд**
У них менялась вёрстка/ключ в `window.__INITIAL_STATE__`. Откройте
`app/sources/understat.py`, сравните с реальной страницей
`https://understat.com/league/EPL` (View Source) и поправьте список ключей.

**Winline/BetBoom: `событий = 0`**
Смотрите раздел 6.4: сверьте `FIELD_MAPPING` с реальным JSON из DevTools.
Проверьте, что в URL нет `?` (параметры идут в `*_PARAMS`), и что `date`-параметр
подходит сайту (код подставляет сегодняшнюю дату автоматически).

**Сигналов нет**
Это нормально: сервис отправляет только «value» с edge ≥ порога. Проверьте:
1) `/stats` — сколько сигналов было за 30 дней; 2) `LOG_LEVEL=DEBUG` и посмотрите
отклонения в логе (там пишется причина); 3) `ODDS_MIN/ODDS_MAX` не задавили ли
диапазон; 4) в белый список входят ли ваши лиги.

**Telegram: сообщения не приходят**
Пользователь должен нажать `/start` (иначе он не в таблице `users`).
Если бот раньше был заблокирован пользователем — он помечается как недоступный
(`user_deliveries`, статус `blocked`) и пропускается; разблокировка + `/start`
возвращает его в рассылку.

**`429 Too Many Requests` от Telegram**
Это защита от флуда. Сервис сам держит темп ≤25 сообщений/с, до 3 ретраев и
паузы между пачками. Если ошибок много — уменьшите `TG_RATE_PER_SEC` (например до 15).

**Хочу выключить лайв или арбитра**
`LIVE_ENABLED=false` — лайв-воркер не запускается. `JUDGE_ENABLED=false` — сигналы
подтверждаются без Claude (по ТЗ это допустимый режим).

---

## Приложение: что уже проверено автоматически

| Проверено (2026-10-08) | Как воспроизвести |
|---|---|
| Схема БД: 17 таблиц, `alembic check` — «No new upgrade operations detected» | `alembic upgrade head && alembic check` |
| Юнит-тесты формул (Пуассон, Эло, баскетбол, Value Engine, уверенность, скринер, агрегатор, парсер букмекера, расчёт результатов) | `pytest -q` |
| Все модули импортируются, `python -m app.main` доходит до preflight | `python -c "import app.main"` |
| Каталог моделей OpenRouter доступен, ID подставляются скриптом | `python scripts/check_models.py` |

**Требует вашей проверки** (по объективным причинам — см. выше): реальные URL
Winline/BetBoom и вид их JSON, поведение бесплатных лимитов RapidAPI/Liquipedia,
конкретные ID моделей на текущую дату, корректность сигналов на реальных данных.
Для всего перечисленного есть скрипты и подсказки — начните с них.
