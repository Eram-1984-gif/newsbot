"""Агент-публикатор: отправка в канал через Telegram Bot API + алерты админу."""
import logging
import os
import re
import time

import httpx

log = logging.getLogger("publisher")
API = "https://api.telegram.org/bot{token}/{method}"
ALLOWED = re.compile(r"</?(b|i|u|s|a|blockquote|code|pre)(\s[^>]*)?>", re.I)


def _sanitize(html_text: str) -> str:
    """Убирает теги, которые Telegram не понимает."""
    return re.sub(r"<[^>]+>", lambda m: m.group(0) if ALLOWED.fullmatch(m.group(0)) else "", html_text)


def _strip_all(html_text: str) -> str:
    return re.sub(r"<[^>]+>", "", html_text)


class Telegram:
    def __init__(self):
        self.token = os.environ["TELEGRAM_BOT_TOKEN"]
        self.channel = os.environ["TELEGRAM_CHANNEL_ID"]
        self.admin = os.environ.get("TELEGRAM_ADMIN_CHAT_ID") or None
        self.http = httpx.Client(timeout=30)

    def _call(self, method: str, **params):
        for attempt in range(4):
            r = self.http.post(API.format(token=self.token, method=method), json=params)
            data = r.json()
            if data.get("ok"):
                return data["result"]
            if r.status_code == 429:  # слишком часто — ждём, сколько просит Telegram
                time.sleep(data.get("parameters", {}).get("retry_after", 5) + 1)
                continue
            raise RuntimeError(f"Telegram {method}: {data.get('description')}")
        raise RuntimeError(f"Telegram {method}: превышено число попыток")

    def _send_text(self, text: str, preview_url: str | None = None) -> int:
        params = {"chat_id": self.channel, "text": text, "parse_mode": "HTML"}
        if preview_url:  # большая картинка над текстом через превью ссылки
            params["link_preview_options"] = {"url": preview_url, "prefer_large_media": True,
                                              "show_above_text": True}
        try:
            return self._call("sendMessage", **params)["message_id"]
        except RuntimeError as e:
            if "parse entities" not in str(e):
                raise
            log.warning("Ошибка разметки, отправляю без форматирования")
            params.pop("parse_mode")
            params["text"] = _strip_all(text)
            return self._call("sendMessage", **params)["message_id"]

    def publish(self, text: str, image: str | None = None, video: str | None = None) -> int:
        text = _sanitize(text)[:4000]
        if len(_strip_all(text)) <= 1024:  # лимит подписи к медиа в Telegram
            for method, field, url in (("sendVideo", "video", video), ("sendPhoto", "photo", image)):
                if not url:
                    continue
                try:
                    msg = self._call(method, chat_id=self.channel, caption=text,
                                     parse_mode="HTML", **{field: url})
                    return msg["message_id"]
                except RuntimeError as e:
                    if "Forbidden" in str(e) or "chat not found" in str(e):
                        raise
                    log.warning("%s не прошёл (%s), пробую дальше", method, e)
            return self._send_text(text)
        return self._send_text(text, preview_url=image)

    def alert(self, text: str):
        if not self.admin:
            return
        try:
            self._call("sendMessage", chat_id=self.admin, text=f"⚠️ NewsBot: {text}"[:4000])
        except Exception as e:  # noqa: BLE001
            log.error("Не удалось отправить алерт: %s", e)
