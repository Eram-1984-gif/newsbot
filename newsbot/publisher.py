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

    def publish(self, text: str) -> int:
        text = _sanitize(text)[:4000]
        try:
            msg = self._call("sendMessage", chat_id=self.channel, text=text,
                             parse_mode="HTML", disable_web_page_preview=False)
        except RuntimeError as e:
            if "parse entities" not in str(e):
                raise
            log.warning("Ошибка разметки, отправляю без форматирования")
            msg = self._call("sendMessage", chat_id=self.channel, text=_strip_all(text))
        return msg["message_id"]

    def alert(self, text: str):
        if not self.admin:
            return
        try:
            self._call("sendMessage", chat_id=self.admin, text=f"⚠️ NewsBot: {text}"[:4000])
        except Exception as e:  # noqa: BLE001
            log.error("Не удалось отправить алерт: %s", e)
