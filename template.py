"""Публикация без ИИ: когда GigaChat отказывается работать с темой (СВО, острая политика).

Пост собирается из материалов самого источника: заголовок + 1–2 первых предложения + ссылка.
Оценка важности — по ключевым словам рубрик, повторы отсекаются по похожести заголовков.
"""
import html
import re
from urllib.parse import urlsplit

# рубрика: (хэштег, эмодзи, ключевые слова в нижнем регистре)
RUBRICS = [
    ("#СВО", "⚡️", ["сво", "всу", "вс рф", "минобороны", "фронт", "удар", "обстрел", "беспилотник", "бпла",
                    "освобож", "наступ", "позиции", "донбасс", "херсон", "запорож", "курск", "белгород"]),
    ("#вооружение", "🛡", ["вооружен", "ракет", "танк", "истребител", "пво", "зрк", "оружи", "оборонн",
                          "испытан", "военная техника", "дрон"]),
    ("#политика", "🏛", ["путин", "лавров", "песков", "кремл", "госдум", "зеленск", "трамп", "нато",
                        "санкци", "переговор", "мид", "президент", "премьер", "выбор", "евросоюз"]),
]


def rubric(title: str):
    t = title.lower()
    for tag, emoji, words in RUBRICS:
        if any(w in t for w in words):
            return tag, emoji
    return None


def clean_title(title: str) -> tuple[str, str | None]:
    """«Заголовок - Издание» (формат Google News) → (заголовок, издание)."""
    m = re.match(r"^(.*\S)\s+[-–—]\s+([^-–—]{2,60})$", title)
    return (m.group(1), m.group(2)) if m else (title, None)


def _words(title: str) -> set[str]:
    """Значимые слова заголовка (обрезаем окончания, чтобы «взяли/взял» совпадали)."""
    return {w[:6] for w in re.findall(r"[a-zа-яё0-9]+", clean_title(title)[0].lower()) if len(w) > 3}


def is_repeat(title: str, recent: list[str], threshold: float = 0.6) -> bool:
    """Повтор: большинство значимых слов короткого заголовка есть в другом заголовке."""
    a = _words(title)
    for r in recent:
        b = _words(r)
        if a and b and len(a & b) / min(len(a), len(b)) >= threshold:
            return True
    return False


def lead(text: str, limit: int = 380) -> str:
    """Первые 1–2 предложения статьи, не длиннее limit символов."""
    text = re.sub(r"\s+", " ", text or "").strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for s in sentences:
        if len(out) + len(s) > limit:
            break
        out = f"{out} {s}".strip()
        if len(out) > 150:
            break
    return out or text[:limit].rsplit(" ", 1)[0] + "…"


def build_post(item: dict, article_text: str, signature: str = "") -> str:
    title, outlet = clean_title(item["title"])
    tag, emoji = rubric(title) or ("", "📰")
    outlet = outlet or urlsplit(item["url"]).netloc.replace("www.", "")
    body = lead(article_text)
    if body and clean_title(body)[0].lower().startswith(title.lower()[:40]):
        body = ""  # анонс повторяет заголовок — не дублируем
    e = html.escape
    parts = [f"<b>{emoji} {e(title)}</b>"]
    if body:
        parts.append(e(body))
    parts.append(f'<a href="{e(item["url"], quote=True)}">Источник: {e(outlet)}</a>' + (f"\n{tag}" if tag else ""))
    if signature:
        parts.append(e(signature))
    return "\n\n".join(parts)
