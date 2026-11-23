"""Агент-сборщик: обходит RSS и поиск Google News, достаёт полный текст статьи."""
import calendar
import hashlib
import html
import logging
import re
import time
from urllib.parse import quote_plus, urlsplit, urlunsplit

import feedparser
import trafilatura

log = logging.getLogger("collector")
UA = "Mozilla/5.0 (compatible; NewsBot/1.0)"


def _norm_url(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", ""))


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _feeds(cfg: dict):
    for url in cfg["sources"].get("rss", []) or []:
        yield url, urlsplit(url).netloc
    for q in cfg["sources"].get("google_news_queries", []) or []:
        lang = q.get("lang", "ru")
        gl = "RU" if lang == "ru" else "US"
        url = (f"https://news.google.com/rss/search?q={quote_plus(q['query'])}+when:1d"
               f"&hl={lang}&gl={gl}&ceid={gl}:{lang}")
        yield url, f"Google News: {q['query']}"


def collect(cfg: dict, db) -> int:
    max_age = cfg["filter"]["max_age_hours"] * 3600
    added = 0
    for feed_url, source in _feeds(cfg):
        try:
            feed = feedparser.parse(feed_url, agent=UA)
        except Exception as e:  # noqa: BLE001
            log.warning("Ошибка ленты %s: %s", feed_url, e)
            continue
        for e in feed.entries[:40]:
            link = e.get("link")
            title = _clean(e.get("title", ""))
            if not link or not title:
                continue
            tm = e.get("published_parsed") or e.get("updated_parsed")
            published = calendar.timegm(tm) if tm else time.time()
            if time.time() - published > max_age:
                continue
            item_id = hashlib.sha1(_norm_url(link).encode()).hexdigest()
            if db.exists(item_id):
                continue
            db.add({
                "id": item_id, "url": link, "title": title,
                "summary": _clean(e.get("summary", ""))[:600],
                "source": source, "published": published,
            })
            added += 1
    log.info("Собрано новых материалов: %d", added)
    return added


def fetch_full_text(url: str, fallback: str = "") -> str:
    """Полный текст статьи; если не получилось — анонс из ленты."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
            if text and len(text) > 300:
                return text[:8000]
    except Exception as e:  # noqa: BLE001
        log.debug("Не удалось скачать %s: %s", url, e)
    return fallback
