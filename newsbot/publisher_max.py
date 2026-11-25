"""Публикация в канал мессенджера MAX (VK) через MAX Bot API.

Включается, только если заданы MAX_BOT_TOKEN и MAX_CHAT_ID. Ошибка в MAX не мешает публикации в Telegram.
"""
import logging
import os
import re
import time

import httpx

from .llm import _ssl_context

log = logging.getLogger("max")
API_URL = os.environ.get("MAX_API_URL", "https://platform-api.max.ru")
ALLOWED = re.compile(r"</?(b|i|u|s|a|code|pre)(\s[^>]*)?>", re.I)


def _sanitize(text: str) -> str:
    return re.sub(r"<[^>]+>", lambda m: m.group(0) if ALLOWED.fullmatch(m.group(0)) else "", text)


class Max:
    def __init__(self):
        self.token = os.environ.get("MAX_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("MAX_CHAT_ID", "").strip()
        self.enabled = bool(self.token and self.chat_id)
        if not self.enabled:
            return
        ctx = _ssl_context()
        self.http = httpx.Client(base_url=API_URL, timeout=30, verify=ctx if ctx else True,
                                 headers={"Authorization": self.token})

    def _send(self, body: dict):
        for attempt in range(4):
            r = self.http.post("/messages", params={"chat_id": self.chat_id}, json=body)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"MAX {r.status_code}: {r.text[:300]}")
            data = r.json()
            return (data.get("message") or {}).get("body", {}).get("mid")
        raise RuntimeError("MAX: превышено число попыток")

    def publish(self, text: str, image: str | None = None):
        body = {"text": _sanitize(text)[:4000], "format": "html", "notify": True}
        if image:
            try:
                return self._send({**body, "attachments": [{"type": "image", "payload": {"url": image}}]})
            except RuntimeError as e:
                log.warning("MAX: фото не прошло (%s), отправляю текстом", e)
        return self._send(body)
