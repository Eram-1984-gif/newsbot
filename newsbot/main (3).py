"""Оркестратор: Сборщик → Аналитик → Автор → Редактор → Публикатор. Работает без человека."""
import argparse
import logging
import os
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

from .agents import Agents
from .collector import collect, fetch_article
from .db import DB
from .llm import ModelRefused
from .publisher import Telegram
from .publisher_max import Max
from . import template, weather

log = logging.getLogger("main")


def in_quiet_hours(cfg) -> bool:
    start, end = cfg["schedule"].get("quiet_hours") or [0, 0]
    h = datetime.now(ZoneInfo(cfg["schedule"]["timezone"])).hour
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end  # интервал через полночь, напр. [23, 7]


def send_everywhere(tg, mx, text: str, image=None) -> int:
    mid = tg.publish(text, image=image)
    if mx is not None and mx.enabled:
        try:
            mx.publish(text, image=image)
        except Exception as e:  # noqa: BLE001
            log.warning("MAX: не удалось опубликовать: %s", e)
    return mid


def run_weather(cfg, db: DB, agents: Agents, tg: Telegram, mx) -> int:
    """Погодные посты по расписанию. Возвращает, сколько опубликовано."""
    w = cfg.get("weather", {})
    if not w.get("enabled", False):
        return 0
    sig = cfg["channel"].get("signature", "")
    cities = w.get("cities") or weather.DEFAULT_CITIES
    posted = 0
    try:
        for slot in w.get("forecast_times", ["07:30", "19:00"]):
            if weather.due(slot, db, "forecast"):
                evening = int(slot.split(":")[0]) >= 15
                send_everywhere(tg, mx, weather.forecast_post(cities, evening, sig, db))
                weather.mark(slot, db, "forecast")
                log.info("Опубликован прогноз погоды (%s)", slot)
                posted += 1
        if w.get("alerts", True):
            text = weather.alert_post(cities[0], db, sig)
            if text:
                send_everywhere(tg, mx, text)
                log.info("Опубликовано погодное предупреждение")
                posted += 1
        for slot in w.get("world_digest_times", []):
            if weather.due(slot, db, "world"):
                weather.mark(slot, db, "world")  # даже если новостей мало — не пытаться весь вечер
                items = [x for x in db.recent_items(hours=10)
                         if weather.is_weather(x["title"]) and not template.is_sensitive(x["title"])
                         and not db.kv_get(f"world_used:{x['id']}")]
                text = weather.world_digest(items, agents, sig)
                if text:
                    send_everywhere(tg, mx, text)
                    for x in items[:12]:
                        db.kv_set(f"world_used:{x['id']}", "1")
                        if x["status"] == "approved":  # не дублировать отдельными постами
                            db.update(x["id"], status="rejected", reason="вошло в погодный дайджест")
                    log.info("Опубликован дайджест «Погода в мире» (%s)", slot)
                    posted += 1
    except Exception as e:  # noqa: BLE001
        log.warning("Погода: %s", e)
        tg.alert(f"погода: {e}")
    return posted


def run_cycle(cfg, db: DB, agents: Agents, tg: Telegram, mx=None):
    s, f = cfg["schedule"], cfg["filter"]

    # 1. Сбор
    collect(cfg, db)

    # 2. Анализ новых материалов пачками
    new = db.by_status("new", limit=400)
    recent = db.recent_published_titles()
    ref = cfg.get("refusals", {})
    approved_now = [r["title"] for r in db.by_status("approved")]
    # Экономия токенов: явные новости СВО GigaChat всё равно не оценивает — сразу в шаблон, без модели
    if ref.get("mode", "template") == "template":
        rest = []
        for it in new:
            if template.is_sensitive(it["title"]):
                if template.is_repeat(it["title"], recent + approved_now):
                    db.update(it["id"], status="rejected", reason="повтор")
                else:
                    db.update(it["id"], status="approved", reason="шаблон",
                              score=max(float(ref.get("score", 8)), f["min_score"]))
                    approved_now.append(it["title"])
            else:
                rest.append(it)
        log.info("Новости СВО/Украина сразу в шаблон (без модели): %d", len(new) - len(rest))
        new = rest
    for i in range(0, len(new), 20):
        batch = new[i:i + 20]
        scored = agents.analyze(batch, recent)
        scored_ids = set()
        for it in scored:
            scored_ids.add(it["id"])
            if it["reason"] == "модель отказалась" and ref.get("mode", "template") == "template":
                # без ИИ: важность по ключевым словам рубрик, повторы — по похожести заголовков
                if template.rubric(it["title"]) and not template.is_repeat(it["title"], recent + approved_now):
                    it["score"], it["reason"] = max(float(ref.get("score", 8)), f["min_score"]), "шаблон"
                    approved_now.append(it["title"])
            ok = it["score"] >= f["min_score"] and not it["duplicate"]
            db.update(it["id"], status="approved" if ok else "rejected",
                      score=it["score"], reason=it["reason"])
        for it in batch:  # то, что аналитик пропустил, не держим вечно
            if scored and it["id"] not in scored_ids:
                db.update(it["id"], status="rejected", reason="не оценено")

    # 3. Публикация с учётом лимитов
    if in_quiet_hours(cfg):
        log.info("Тихие часы — публикация отложена")
        return
    if run_weather(cfg, db, agents, tg, mx):
        return  # погодный пост вышел — новости в следующей проверке, чтобы не идти подряд
    day_ago = time.time() - 86400
    left_today = s["max_posts_per_day"] - db.published_count_since(day_ago)
    queue = sorted(db.by_status("approved", limit=1000), key=lambda x: (-(x["score"] or 0), -x["published"]))
    # чередуем: пост от ИИ (наука, медицина, космос…) ↔ пост по шаблону (СВО, политика)
    ai_q = [x for x in queue if x["reason"] != "шаблон"]
    tpl_q = [x for x in queue if x["reason"] == "шаблон"]
    tpl_q.sort(key=lambda x: -x["published"])  # среди шаблонных — самые свежие
    first, second = (ai_q, tpl_q) if db.last_published_reason() == "шаблон" else (tpl_q, ai_q)
    queue = [x for pair in zip(first, second) for x in pair] + first[len(second):] + second[len(first):]
    log.info("В очереди: от ИИ %d, по шаблону (СВО/политика) %d", len(ai_q), len(tpl_q))
    max_age = f["max_age_hours"] * 3600
    posted = 0
    log.info("Одобрено и ждёт публикации: %d; осталось постов на сутки: %d", len(queue), left_today)
    wait = s["min_minutes_between_posts"] * 60 - (time.time() - db.last_published_at())
    if wait > 0:
        log.info("Пауза между постами: следующий пост можно через %d мин", wait // 60 + 1)

    for it in queue:
        if posted >= min(s["max_posts_per_run"], left_today):
            break
        if time.time() - db.last_published_at() < s["min_minutes_between_posts"] * 60:
            break
        if time.time() - it["published"] > max_age:
            db.update(it["id"], status="rejected", reason="устарело в очереди")
            continue

        art = fetch_article(it["url"], fallback=f"{it['title']}. {it['summary']}")
        text = art["text"]
        it = {**it, "url": art["url"]}  # в посте — прямая ссылка на источник, а не на Google
        use_template = ref.get("mode", "template") == "template"
        if it["reason"] == "шаблон" and not template.rubric(it["title"]):  # перепроверка по точным словам
            db.update(it["id"], status="rejected", reason="не по теме (перепроверка)")
            continue
        if template.is_repeat(it["title"], recent):
            db.update(it["id"], status="rejected", reason="повтор уже опубликованного")
            continue
        post = None
        if it["reason"] != "шаблон":
            try:
                draft = agents.write(it, text)
                verdict = agents.review(draft, text)
                if verdict.get("approved"):
                    post = verdict.get("post") or draft
                elif verdict.get("issues") != "модель отказалась" or not use_template:
                    db.update(it["id"], status="rejected", reason=f"редактор: {verdict.get('issues')}")
                    log.info("Редактор отклонил: %s", it["title"])
                    continue
            except ModelRefused:
                if not use_template:
                    db.update(it["id"], status="rejected", reason="модель отказалась писать")
                    continue
        if post is None:  # модель отказалась — публикуем материал источника без ИИ
            post = template.build_post(it, text, cfg["channel"].get("signature", ""))
            log.info("Пост без ИИ (шаблон): %s", it["title"])
        try:
            media = cfg.get("media", {})
            image = (art["image"] or it.get("image")) if media.get("photos", True) else None
            video = art["video"] if media.get("videos", True) else None
            mid = tg.publish(post, image=image, video=video)
        except Exception as e:  # noqa: BLE001
            tg.alert(f"не удалось опубликовать «{it['title']}»: {e}")
            break  # проблема с каналом — не тратим токены, повторим в следующий раз
        db.update(it["id"], status="published", post=post, published_at=time.time(), message_id=mid)
        if mx is not None and mx.enabled:  # дублируем в MAX; сбой там не мешает Telegram
            try:
                mx.publish(post, image=image)
                log.info("Продублировано в MAX")
            except Exception as e:  # noqa: BLE001
                log.warning("MAX: не удалось опубликовать: %s", e)
                tg.alert(f"MAX: не удалось опубликовать «{it['title']}»: {e}")
        recent.insert(0, it["title"])
        posted += 1
        log.info("Опубликовано: %s (score %.0f)", it["title"], it["score"])

    db.cleanup()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true", help="один цикл и выход (для cron)")
    args = ap.parse_args()

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    with open(args.config, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    os.makedirs("data", exist_ok=True)

    db, agents, tg = DB(), Agents(cfg), Telegram()
    mx = Max()
    log.info("MAX: %s", "включён" if mx.enabled else "выключен (нет MAX_BOT_TOKEN / MAX_CHAT_ID)")
    errors_in_row = 0
    while True:
        try:
            run_cycle(cfg, db, agents, tg, mx)
            errors_in_row = 0
        except Exception as e:  # noqa: BLE001
            errors_in_row += 1
            log.error("Ошибка цикла: %s\n%s", e, traceback.format_exc())
            if errors_in_row in (1, 3, 10):
                tg.alert(f"ошибка цикла ({errors_in_row} подряд): {e}")
        if args.once:
            break
        time.sleep(cfg["schedule"]["check_every_minutes"] * 60)


if __name__ == "__main__":
    main()
