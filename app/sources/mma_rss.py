"""MMA/бокс — бесплатные RSS-ленты спортивных новостей (Модуль 1).

Честно о природе источника:
  * Для MMA/бокса НЕТ бесплатного API с травмами и составами. Единственный
    бесплатный вариант — RSS-новости (settings.rss_feeds: championat.com, sports.ru
    и любые другие ленты, которые вы добавите в .env).
  * Мы НЕ выдумываем структуру: текст новости (заголовок + описание) отдаётся
    в LLM-аналитик «как есть» (в контекст news), а травмы извлекаются простым
    правилом «имя бойца + ключевое слово о травме/снятии» (см. INJURY_MARKERS).
    Это эвристика — она помечена в источнике и в SETUP.md.
  * Результаты боёв и H2H из RSS не парсятся: они приходят из LLM-анализа
    новостей (и, для UFC, из расписания букмекеров). Поэтому get_recent_form и
    get_h2h честно возвращают пусто, а не «нули».
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from loguru import logger

from app.config import settings
from app.sources.base import BaseHttpClient, Injury, ScheduleDensity, StatsProvider, TTLCache

NEWS_TTL_SEC = 30 * 60
MAX_ITEMS_PER_FEED = 60

INJURY_MARKERS = (
    "травм", "снял", "снялся", "снят с", "не выступит", "пропустит", "отказ",
    "injur", "withdraw", "out of the fight", "pull out", "pulled out", "ruled out",
    "sidelined", "hospital", "surgery",
)
SUSPENSION_MARKERS = ("дисквалиф", "допинг", "suspend", "suspension", "banned", "failed test")
RESULT_MARKERS = ("победил", "проиграл", "нокаут", "ko", "tko", "решение судей", "decision", "defeated")


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", "".join(node.itertext())).strip()


def _parse_pub_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw.strip(), fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


@dataclass
class RssItem:
    title: str
    summary: str
    link: str
    published: datetime | None
    feed: str

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "title": self.title[:300],
            "summary": self.summary[:600],
            "published": self.published.isoformat() if self.published else None,
            "feed": self.feed,
        }


def parse_feed(xml_text: str, feed_url: str) -> list[RssItem]:
    """Разбор RSS 2.0 и Atom. Битый XML → пустой список (источник не должен ронять сервис)."""
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 — внешние фиды читаются как данные, без eval
    except ET.ParseError as exc:
        logger.warning("rss: {} не разобрался как XML ({})", feed_url, exc)
        return []

    items: list[RssItem] = []
    tags = {node.tag.lower() for node in root.iter()}
    is_atom = any(tag.endswith("entry") for tag in tags)

    if is_atom:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.iter():
            if not entry.tag.lower().endswith("entry"):
                continue
            title = _text(entry.find("atom:title", ns)) or _text(entry.find("title"))
            summary = (
                _text(entry.find("atom:summary", ns))
                or _text(entry.find("atom:content", ns))
                or _text(entry.find("summary"))
            )
            link = ""
            for link_node in list(entry.findall("atom:link", ns)) + list(entry.findall("link")):
                href = link_node.get("href")
                if href:
                    link = href
                    break
            published = _parse_pub_date(
                _text(entry.find("atom:updated", ns)) or _text(entry.find("atom:published", ns))
            )
            if title:
                items.append(RssItem(title=title, summary=summary, link=link, published=published, feed=feed_url))
        return items[:MAX_ITEMS_PER_FEED]

    for item in root.iter():
        if not item.tag.lower().endswith("item"):
            continue
        title = _text(item.find("title"))
        summary = _text(item.find("description")) or _text(item.find("{http://purl.org/rss/1.0/modules/content/}encoded"))
        link = _text(item.find("link"))
        published = _parse_pub_date(_text(item.find("pubDate")) or _text(item.find("published")))
        if title:
            items.append(RssItem(title=title, summary=summary, link=link, published=published, feed=feed_url))
    return items[:MAX_ITEMS_PER_FEED]


def matches_name(item: RssItem, name: str) -> bool:
    """Упоминается ли боец/команда в новости (по фамилии или полному имени)."""
    target = (name or "").strip().lower()
    if not target:
        return False
    haystack = f"{item.title} {item.summary}".lower()
    if target in haystack:
        return True
    parts = [part for part in re.split(r"[\s.\-]+", target) if len(part) >= 4]
    return any(part in haystack for part in parts)


def looks_like_injury(item: RssItem) -> bool:
    haystack = f"{item.title} {item.summary}".lower()
    return any(marker in haystack for marker in INJURY_MARKERS)


def looks_like_suspension(item: RssItem) -> bool:
    haystack = f"{item.title} {item.summary}".lower()
    return any(marker in haystack for marker in SUSPENSION_MARKERS)


class MmaRssProvider(StatsProvider):
    """MMA/бокс: новости из RSS (травмы/снятия — эвристикой по ключевым словам)."""

    provider_name = "mma-rss"
    sport = "mma"

    def __init__(self, feeds: list[str] | None = None) -> None:
        super().__init__(source_name="mma-rss", base_url="", politeness=True)
        self.feeds = feeds if feeds is not None else settings.rss_feed_list
        self._cache = TTLCache(ttl_sec=NEWS_TTL_SEC)

    @property
    def available(self) -> bool:
        return bool(self.feeds)

    # ------------------------------------------------------------------ фиды
    async def _feed_items(self, feed_url: str) -> list[RssItem]:
        async def factory() -> list[RssItem]:
            try:
                xml_text = await self.get_text(feed_url, polite=True)
            except Exception as exc:
                logger.warning("rss: фид {} недоступен ({})", feed_url, exc)
                return []
            items = parse_feed(xml_text, feed_url)
            logger.info("rss: {} → {} новостей", feed_url, len(items))
            return items

        return await self._cache.get_or_set(f"feed:{feed_url}", factory, ttl_sec=NEWS_TTL_SEC)

    async def all_items(self, hours: int = 72) -> list[RssItem]:
        """Все свежие новости со всех фидов (дедуп по заголовку)."""
        edge = datetime.now(timezone.utc) - timedelta(hours=hours)
        seen: set[str] = set()
        items: list[RssItem] = []
        for feed_url in self.feeds:
            for item in await self._feed_items(feed_url):
                key = item.title.lower()[:120]
                if key in seen:
                    continue
                if item.published is not None and item.published < edge:
                    continue
                seen.add(key)
                items.append(item)
        items.sort(key=lambda item: item.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return items

    async def get_news(self, name: str | None = None, limit: int = 8, keywords: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """Новости для LLM-контекста: по имени бойца/события и/или ключевым словам."""
        items = await self.all_items()
        selected: list[RssItem] = []
        for item in items:
            if name and matches_name(item, name):
                selected.append(item)
            elif keywords and any(keyword.lower() in f"{item.title} {item.summary}".lower() for keyword in keywords):
                selected.append(item)
        if not selected:
            selected = items[:limit]  # общий новостной фон — тоже полезен аналитику
        return [item.as_prompt_dict() for item in selected[:limit]]

    # -------------------------------------------------- контракт StatsProvider
    async def get_injuries(self, team: str, **kwargs: Any) -> list[Injury]:
        """«Травма/снятие» = новость с именем бойца и ключевым словом (эвристика)."""
        items = await self.all_items()
        injuries: list[Injury] = []
        for item in items:
            if not matches_name(item, team):
                continue
            haystack = f"{item.title} {item.summary}".lower()
            if looks_like_suspension(item):
                status, reason_prefix = "suspended", "дисквалификация/допинг"
            elif looks_like_injury(item):
                status, reason_prefix = "doubtful", "травма/снятие"
            elif any(marker in haystack for marker in RESULT_MARKERS):
                continue
            else:
                continue
            injuries.append(
                Injury(
                    team=team,
                    player=team,
                    status=status,
                    reason=f"{reason_prefix}: {item.title[:200]}",
                    source=f"rss:{item.feed}",
                    importance="ключевой боец/участник (эвристика по новости)",
                )
            )
            if len(injuries) >= 5:
                break
        if injuries:
            logger.info("rss: по '{}' найдено {} новостей о травмах/снятиях", team, len(injuries))
        return injuries

    async def get_recent_form(self, team: str, n: int = 5, **kwargs: Any) -> list[Any]:
        """RSS не даёт структурированных результатов боёв → пусто (см. docstring модуля)."""
        return []

    async def get_h2h(self, home_team: str, away_team: str, **kwargs: Any) -> list[Any]:
        return []

    async def get_predicted_lineups(self, team: str, **kwargs: Any) -> Any:
        return None

    async def get_schedule_density(self, team: str, **kwargs: Any) -> ScheduleDensity:
        """«Плотность» бойца: сколько раз он упоминался в новостях за 7 дней (грубый сигнал активности)."""
        items = await self.all_items(hours=24 * 7)
        mentions = sum(1 for item in items if matches_name(item, team))
        return ScheduleDensity(
            matches_last_7_days=0,  # точных дат боёв в RSS нет
            matches_next_7_days=0,
            rest_days=None,
            back_to_back=False,
            notes=f"rss: упоминаний о бойце за 7 дней — {mentions} (структурированного календаря нет)",
        )
