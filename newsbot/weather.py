"""Погода: прогноз по Москве и области, предупреждения об опасных явлениях, дайджест погоды в мире.

Прогноз берётся из Open-Meteo (бесплатно, без ключа): https://open-meteo.com
Посты прогноза и предупреждений собираются из цифр без ИИ — ничего не выдумывается.
"""
import html
import logging
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("weather")
API = "https://api.open-meteo.com/v1/forecast"
TZ = ZoneInfo("Europe/Moscow")

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
          "октября", "ноября", "декабря"]
WEEKDAYS = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]

CODES = {0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
         45: "туман", 48: "туман с изморозью", 51: "слабая морось", 53: "морось", 55: "сильная морось",
         56: "ледяная морось", 57: "ледяная морось", 61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
         66: "ледяной дождь", 67: "сильный ледяной дождь", 71: "небольшой снег", 73: "снег",
         75: "сильный снег", 77: "снежная крупа", 80: "ливневый дождь", 81: "ливни", 82: "сильные ливни",
         85: "снегопад", 86: "сильный снегопад", 95: "гроза", 96: "гроза с градом", 99: "сильная гроза с градом"}

DEFAULT_CITIES = [
    {"name": "Москва", "lat": 55.7558, "lon": 37.6173},
    {"name": "Север области (Сергиев Посад)", "lat": 56.3000, "lon": 38.1333},
    {"name": "Запад (Можайск)", "lat": 55.5050, "lon": 36.0270},
    {"name": "Юг (Серпухов)", "lat": 54.9158, "lon": 37.4111},
    {"name": "Восток (Орехово-Зуево)", "lat": 55.8067, "lon": 38.9618},
]

WORLD_WORDS = [r"\bураган\w*", r"\bтайфун\w*", r"\bциклон\w*", r"\bантициклон\w*", r"\bнаводнени\w*",
               r"\bпаводк\w*", r"\bливн\w*", r"\bснегопад\w*", r"\bметел\w*", r"\bбуран\w*", r"\bжар[аы]\b",
               r"\bаномальн\w* (жар|холод|мороз|тепл)\w*", r"\bзасух\w*", r"\bторнадо\b", r"\bсмерч\w*",
               r"\bшторм\w*", r"\bморозы?\b", r"\bпохолодани\w*", r"\bгололед\w*", r"\bпогод\w*",
               r"\bhurricane\b", r"\btyphoon\b", r"\bflood\w*", r"\bheatwave\b", r"\bblizzard\b",
               r"\bstorm\b", r"\btornado\b", r"\bsnowstorm\b", r"\bextreme weather\b"]
_WORLD = [re.compile(p) for p in WORLD_WORDS]


def is_weather(title: str) -> bool:
    t = title.lower().replace("ё", "е")
    return any(p.search(t) for p in _WORLD)


# ---------- данные ----------
def fetch(city: dict) -> dict:
    params = {
        "latitude": city["lat"], "longitude": city["lon"], "timezone": "Europe/Moscow", "forecast_days": 3,
        "wind_speed_unit": "ms",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,rain_sum,"
                 "snowfall_sum,wind_gusts_10m_max",
    }
    for attempt in range(3):
        try:
            r = httpx.get(API, params=params, timeout=20)
            r.raise_for_status()
            d = r.json()["daily"]
            return {k: d[k] for k in d}
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                raise RuntimeError(f"Open-Meteo: {e}") from e
            time.sleep(3)


def _t(x) -> str:
    x = round(x)
    return f"+{x}°" if x > 0 else ("0°" if x == 0 else f"−{abs(x)}°")


def _day(d: str) -> str:
    dt = date.fromisoformat(d)
    return f"{dt.day} {MONTHS[dt.month - 1]}"


def _wd(d: str) -> str:
    return WEEKDAYS[date.fromisoformat(d).weekday()]


def _describe(f: dict, i: int) -> str:
    parts = [CODES.get(f["weather_code"][i], "без осадков").capitalize()]
    snow, rain = f["snowfall_sum"][i] or 0, f["rain_sum"][i] or 0
    if snow >= 0.5:
        parts.append(f"до {round(snow)} см снега")
    elif rain >= 1:
        parts.append(f"осадков {round(f['precipitation_sum'][i])} мм")
    gust = f["wind_gusts_10m_max"][i] or 0
    if gust >= 10:
        parts.append(f"порывы ветра до {round(gust)} м/с")
    return ", ".join(parts)


# ---------- опасные явления ----------
def hazards(f: dict, i: int) -> list[str]:
    """Опасные явления на день i (0 — сегодня). Возвращает список коротких описаний."""
    out = []
    tmax, tmin = f["temperature_2m_max"], f["temperature_2m_min"]
    snow, rain, gust = f["snowfall_sum"][i] or 0, f["rain_sum"][i] or 0, f["wind_gusts_10m_max"][i] or 0
    code = f["weather_code"][i]
    if snow >= 10:
        out.append(f"очень сильный снегопад — до {round(snow)} см снега")
    elif snow >= 5:
        out.append(f"сильный снегопад — до {round(snow)} см снега")
    if rain >= 15:
        out.append(f"сильный дождь — до {round(rain)} мм")
    if code in (56, 57, 66, 67):
        out.append("ледяной дождь и гололёд")
    if gust >= 15:
        out.append(f"сильный ветер — порывы до {round(gust)} м/с")
    if tmin[i] <= -20:
        out.append(f"сильный мороз — ночью до {_t(tmin[i])}")
    if tmax[i] >= 30:
        out.append(f"жара — днём до {_t(tmax[i])}")
    if i > 0:
        diff = tmax[i] - tmax[i - 1]
        if diff <= -7:
            out.append(f"резкое похолодание — днём {_t(tmax[i])} вместо {_t(tmax[i - 1])}")
        elif diff >= 7:
            out.append(f"резкое потепление — днём {_t(tmax[i])} вместо {_t(tmax[i - 1])}")
        if tmax[i - 1] > 0 and tmin[i] <= -2 and (f["precipitation_sum"][i - 1] or 0) >= 1:
            out.append("переход через ноль после осадков — гололедица на дорогах и тротуарах")
    return out


# ---------- посты ----------
def forecast_post(cities: list[dict], evening: bool, signature: str = "", db=None) -> str:
    i = 1 if evening else 0  # вечером — прогноз на завтра
    data = [(c, fetch(c)) for c in cities]
    msk_city, msk = data[0]
    d = msk["time"][i]
    head = f"🌤 <b>Погода в Москве {'на завтра' if evening else 'на сегодня'}, {_day(d)}</b>"
    lines = [head, "",
             f"Днём {_t(msk['temperature_2m_max'][i])}, ночью {_t(msk['temperature_2m_min'][i])}. "
             f"{_describe(msk, i)}."]
    if len(data) > 1:
        lines += ["", "<b>Подмосковье:</b>"]
        for c, f in data[1:]:
            lines.append(f"• {html.escape(c['name'])}: {_t(f['temperature_2m_min'][i])}…{_t(f['temperature_2m_max'][i])}, "
                         f"{CODES.get(f['weather_code'][i], '').lower()}")
    hz = hazards(msk, i)
    if db is not None:  # уже сказали в прогнозе — отдельное предупреждение не нужно
        for h in hz:
            db.kv_set(f"alert:{d}:{h.split(' —')[0]}", "1")
    if hz:
        lines += ["", "⚠️ <b>Внимание:</b> " + "; ".join(hz) + "."]
    nxt = i + 1
    if nxt < len(msk["time"]):
        lines += ["", f"Дальше, в {_wd(msk['time'][nxt])}: днём {_t(msk['temperature_2m_max'][nxt])}, "
                      f"{CODES.get(msk['weather_code'][nxt], '').lower()}."]
    lines += ["", "#погода"]
    if signature:
        lines.append(html.escape(signature))
    return "\n".join(lines)


def alert_post(city: dict, db, signature: str = "") -> str | None:
    """Пост-предупреждение, если на сегодня/завтра/послезавтра появились новые опасные явления."""
    f = fetch(city)
    found = []
    for i in range(len(f["time"])):
        for h in hazards(f, i):
            key = f"alert:{f['time'][i]}:{h.split(' —')[0]}"
            if not db.kv_get(key):
                found.append((i, h, key))
    if not found:
        return None
    for _, _, key in found:
        db.kv_set(key, "1")
    lines = ["⚠️ <b>Погодное предупреждение: Москва и область</b>", ""]
    for i, h, _ in found:
        when = "сегодня" if i == 0 else ("завтра" if i == 1 else f"в {_wd(f['time'][i])}")
        lines.append(f"• {when.capitalize()}, {_day(f['time'][i])}: {h}.")
    lines += ["", "Данные: Open-Meteo. Следите за официальными предупреждениями МЧС и Гидрометцентра.", "#погода"]
    if signature:
        lines.append(html.escape(signature))
    return "\n".join(lines)


def world_digest(items: list[dict], agents, signature: str = "") -> str | None:
    """Дайджест погодных событий в мире из свежих новостей (ИИ сводит, при отказе — список ссылок)."""
    if len(items) < 2:
        return None
    items = items[:12]
    listing = "\n".join(f"[{n}] {it['title']} — {it['summary'][:200]} | {it['url']}" for n, it in enumerate(items))
    system = ("Ты — редактор раздела погоды. Составь дайджест «Погода в мире»: 3–5 самых заметных погодных "
              "событий (ураганы, наводнения, снегопады, жара, морозы). Используй ТОЛЬКО факты из списка. "
              "Формат Telegram HTML: первая строка «🌍 <b>Погода в мире</b>», затем пункты «• короткое "
              "предложение (<a href=\"URL\">источник</a>)». Без вступлений и выводов. До 900 символов. "
              "Последняя строка: #погодавмире")
    try:
        text = agents._ask(system, listing, temperature=0.3)
        if "<a href" in text and "Погода в мире" in text:
            return text + (f"\n{html.escape(signature)}" if signature else "")
    except Exception as e:  # noqa: BLE001
        log.info("Дайджест без ИИ (%s)", type(e).__name__)
    lines = ["🌍 <b>Погода в мире</b>", ""]
    for it in items[:5]:
        lines.append(f'• <a href="{html.escape(it["url"], quote=True)}">{html.escape(it["title"])}</a>')
    lines += ["", "#погодавмире"]
    return "\n".join(lines)


# ---------- расписание ----------
def due(slot: str, db, key: str, window_hours: float = 3) -> bool:
    """Пора ли публиковать: время слота наступило (не позже window_hours) и сегодня ещё не публиковали."""
    now = datetime.now(TZ)
    hh, mm = map(int, slot.split(":"))
    start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if not (start <= now < start + timedelta(hours=window_hours)):
        return False
    return not db.kv_get(f"{key}:{now.date()}:{slot}")


def mark(slot: str, db, key: str):
    db.kv_set(f"{key}:{datetime.now(TZ).date()}:{slot}", "1")
