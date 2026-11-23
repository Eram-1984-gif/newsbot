"""Агент-сборщик: обходит RSS и поиск Google News, достаёт полный текст статьи."""
import calendar
import hashlib
import html
import json
import logging
import re
import time
from urllib.parse import quote_plus, urlsplit, urlunsplit

import feedparser
import httpx
import trafilatura

log = logging.getLogger("collector")
UA = "Mozilla/5.0 (compatible; NewsBot/1.0)"


def _norm_url(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", ""))


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _get(url: str, max_seconds: float = 20, max_bytes: int = 5_000_000) -> bytes:
    """Скачивание с жёстким лимитом на ОБЩЕЕ время и размер (медленный сайт не повесит бота)."""
    start = time.monotonic()
    chunks, size = [], 0
    with httpx.stream("GET", url, headers={"User-Agent": UA}, timeout=10, follow_redirects=True) as r:
        r.raise_for_status()
        for chunk in r.iter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                break
            if time.monotonic() - start > max_seconds:
                raise TimeoutError(f"дольше {max_seconds:.0f} с")
    return b"".join(chunks)


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
        t0 = time.monotonic()
        try:  # жёсткий лимит: зависший сайт не должен останавливать бота
            feed = feedparser.parse(_get(feed_url, max_seconds=20))
        except Exception as e:  # noqa: BLE001
            log.warning("Лента недоступна, пропускаю: %s (%s)", source, type(e).__name__)
            continue
        log.info("Лента %s: %d записей за %.1f с", source, len(feed.entries), time.monotonic() - t0)
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


def resolve_google_news(url: str) -> str:
    """Ссылка Google News → настоящий адрес статьи (иначе нет ни текста, ни фото)."""
    m = re.search(r"news\.google\.com/(?:rss/)?articles/([^?/]+)", url)
    if not m:
        return url
    gid = m.group(1)
    try:
        page = _get(f"https://news.google.com/articles/{gid}", max_seconds=10).decode("utf-8", "replace")
        sig = re.search(r'data-n-a-sg="([^"]+)"', page).group(1)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page).group(1)
        req = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None,
                               None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
               gid, int(ts), sig]
        r = httpx.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                       data={"f.req": json.dumps([[["Fbv4je", json.dumps(req), None, "generic"]]])},
                       headers={"User-Agent": UA}, timeout=10)
        body = r.text.split("\n\n", 1)[1]
        real = json.loads(json.loads(body)[0][2])[1]
        if isinstance(real, str) and real.startswith("http"):
            log.info("Google News → %s", real)
            return real
    except Exception as e:  # noqa: BLE001
        log.info("Не удалось раскрыть ссылку Google News (%s)", type(e).__name__)
    return url


def _og(html_text: str, prop: str) -> str | None:
    for pat in (rf'<meta[^>]+(?:property|name)="{prop}"[^>]+content="([^"]+)"',
                rf'<meta[^>]+content="([^"]+)"[^>]+(?:property|name)="{prop}"'):
        m = re.search(pat, html_text, re.I)
        if m:
            return html.unescape(m.group(1))
    return None


def fetch_article(url: str, fallback: str = "") -> dict:
    """Полный текст статьи + главное фото + видео (если это прямой mp4)."""
    url = resolve_google_news(url)
    out = {"text": fallback, "image": None, "video": None, "url": url}
    try:
        page = _get(url, max_seconds=20, max_bytes=3_000_000).decode("utf-8", errors="replace")
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
