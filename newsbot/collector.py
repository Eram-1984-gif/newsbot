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


BAD_IMG = ("google", "gstatic", ".svg", "logo", "favicon", "pixel", "1x1", "avatar")


def _good_image(url) -> bool:
    return bool(url) and url.startswith("http") and not any(b in url.lower() for b in BAD_IMG)


def _entry_image(e) -> str | None:
    """Картинка из RSS: media:content, media:thumbnail, enclosure или <img> в анонсе."""
    cands = []
    for m in e.get("media_content", []) or []:
        if "image" in (m.get("medium", "") + m.get("type", "image")):
            cands.append(m.get("url"))
    cands += [m.get("url") for m in e.get("media_thumbnail", []) or []]
    cands += [en.get("href") for en in e.get("enclosures", []) or [] if "image" in en.get("type", "")]
    m = re.search(r'<img[^>]+src="([^"]+)"', e.get("summary", "") or "")
    if m:
        cands.append(html.unescape(m.group(1)))
    return next((c for c in cands if _good_image(c)), None)


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
                "image": _entry_image(e),
            })
            added += 1
    log.info("Собрано новых материалов: %d", added)
    return added


def _og(html_text: str, prop: str) -> str | None:
    for pat in (rf'<meta[^>]+(?:property|name)="{prop}"[^>]+content="([^"]+)"',
                rf'<meta[^>]+content="([^"]+)"[^>]+(?:property|name)="{prop}"'):
        m = re.search(pat, html_text, re.I)
        if m:
            return html.unescape(m.group(1))
    return None


def fetch_article(url: str, fallback: str = "") -> dict:
    """Полный текст статьи + главное фото + видео (если это прямой mp4)."""
    out = {"text": fallback, "image": None, "video": None}
    try:
        page = trafilatura.fetch_url(url)
        if not page:
            return out
        text = trafilatura.extract(page, include_comments=False, include_tables=False)
        if text and len(text) > 300:
            out["text"] = text[:8000]
        img = _og(page, "og:image") or _og(page, "twitter:image")
        if _good_image(img):
            out["image"] = img
        for prop in ("og:video:secure_url", "og:video:url", "og:video"):
            vid = _og(page, prop)
            if vid and vid.startswith("http") and vid.lower().split("?")[0].endswith(".mp4"):
                out["video"] = vid
                break
    except Exception as e:  # noqa: BLE001
        log.debug("Не удалось скачать %s: %s", url, e)
    return out
