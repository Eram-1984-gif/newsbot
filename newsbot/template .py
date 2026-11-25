"""Публикация без ИИ: когда GigaChat отказывается работать с темой (СВО, острая политика).

Пост собирается из материалов самого источника: заголовок + 1–2 первых предложения + ссылка.
Оценка важности — по ключевым словам рубрик, повторы отсекаются по похожести заголовков.
"""
import html
import re
from urllib.parse import urlsplit

# рубрика: (хэштег, эмодзи, шаблоны слов). \b — начало/конец слова, \w* — любое окончание.
RUBRICS = [
    ("#СВО", "⚡️", [r"\bсво\b", r"\bвсу\b", r"\bвс рф\b", r"\bмо рф\b", r"\bминобороны\b", r"\bфронт\w*",
                    r"\bбпла\b", r"\bбеспилотник\w*", r"\bобстрел\w*", r"\bдонбасс\w*", r"\bднр\b", r"\bлнр\b",
                    r"\bзапорожск\w*", r"\bхерсонск\w*", r"\bкупянск\w*", r"\bпокровск\w*", r"\bгруппировк\w* войск",
                    r"\bтцк\b", r"\bмобилизац\w*", r"\bзоне сво\b"]),
    ("#вооружение", "🛡", [r"\bракет\w*", r"\bтанк\w*", r"\bистребител\w*", r"\bпво\b", r"\bзрк\b",
                          r"\bвооружени\w*", r"\bоборонн\w*", r"\bвоенн\w* техник\w*", r"\bгиперзвук\w*"]),
    ("#политика", "🏛", [r"\bпутин\w*", r"\bлавров\w*", r"\bпесков\w*", r"\bкремл\w*", r"\bгосдум\w*",
                        r"\bзеленск\w*", r"\bтрамп\w*", r"\bнато\b", r"\bсанкци\w*", r"\bпереговор\w*",
                        r"\bмид\b", r"\bукраин\w*", r"\bкиев\w*", r"\bевросоюз\w*", r"\bсовбез\w*"]),
]
_COMPILED = [(tag, emoji, [re.compile(p) for p in pats]) for tag, emoji, pats in RUBRICS]
# Темы, которые GigaChat не обрабатывает: отправляем сразу в шаблон, не тратя токены
_SENSITIVE = [re.compile(p) for p in RUBRICS[0][2] + [r"\bзеленск\w*", r"\bукраин\w*", r"\bкиев\w*"]]


def rubric(title: str):
    t = clean_title(title)[0].lower().replace("ё", "е")
    for tag, emoji, pats in _COMPILED:
        if any(p.search(t) for p in pats):
            return tag, emoji
    return None


def is_sensitive(title: str) -> bool:
    t = clean_title(title)[0].lower().replace("ё", "е")
    return any(p.search(t) for p in _SENSITIVE)


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
