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

log = logging.getLogger("main")


def in_quiet_hours(cfg) -> bool:
    start, end = cfg["schedule"].get("quiet_hours") or [0, 0]
    h = datetime.now(ZoneInfo(cfg["schedule"]["timezone"])).hour
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end  # интервал через полночь, напр. [23, 7]


def run_cycle(cfg, db: DB, agents: Agents, tg: Telegram):
    s, f = cfg["schedule"], cfg["filter"]

    # 1. Сбор
    collect(cfg, db)

    # 2. Анализ новых материалов пачками
    new = db.by_status("new", limit=400)
    recent = db.recent_published_titles()
    for i in range(0, len(new), 20):
        batch = new[i:i + 20]
        scored = agents.analyze(batch, recent)
        scored_ids = set()
        for it in scored:
            scored_ids.add(it["id"])
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
    day_ago = time.time() - 86400
    left_today = s["max_posts_per_day"] - db.published_count_since(day_ago)
    queue = sorted(db.by_status("approved"), key=lambda x: (-(x["score"] or 0), -x["published"]))
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
        try:
            draft = agents.write(it, text)
        except ModelRefused:
            db.update(it["id"], status="rejected", reason="модель отказалась писать")
            log.info("Модель отказалась писать: %s", it["title"])
            continue
        verdict = agents.review(draft, text)
        if not verdict.get("approved"):
            db.update(it["id"], status="rejected", reason=f"редактор: {verdict.get('issues')}")
            log.info("Редактор отклонил: %s", it["title"])
            continue

        post = verdict.get("post") or draft
        try:
            media = cfg.get("media", {})
            image = (art["image"] or it.get("image")) if media.get("photos", True) else None
            video = art["video"] if media.get("videos", True) else None
            mid = tg.publish(post, image=image, video=video)
        except Exception as e:  # noqa: BLE001
            tg.alert(f"не удалось опубликовать «{it['title']}»: {e}")
            break  # проблема с каналом — не тратим токены, повторим в следующий раз
        db.update(it["id"], status="published", post=post, published_at=time.time(), message_id=mid)
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
    errors_in_row = 0
    while True:
        try:
            run_cycle(cfg, db, agents, tg)
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
