"""Клиент GigaChat API (Сбер): авторизация по ключу, автообновление токена, повторы."""
import logging
import os
import ssl
import time
import uuid

import httpx

log = logging.getLogger("llm")

OAUTH_URL = os.environ.get("GIGACHAT_OAUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth")
API_URL = os.environ.get("GIGACHAT_API_URL", "https://gigachat.devices.sberbank.ru/api/v1")
CA_FILE = os.environ.get("GIGACHAT_CA_FILE", "certs/russian_trusted_root_ca.cer")


def _ssl_context():
    """Системные сертификаты + корневой сертификат Минцифры (нужен для серверов Сбера)."""
    ctx = ssl.create_default_context()
    try:
        with open(CA_FILE, "rb") as fh:
            data = fh.read()
        if b"BEGIN CERTIFICATE" in data:
            ctx.load_verify_locations(cadata=data.decode())
        else:
            ctx.load_verify_locations(cadata=data)  # формат DER
        return ctx
    except FileNotFoundError:
        log.warning("Сертификат Минцифры не найден (%s) — проверка TLS для GigaChat отключена", CA_FILE)
        return False


class GigaChat:
    def __init__(self, model: str, max_tokens: int):
        self.key = os.environ["GIGACHAT_AUTH_KEY"]
        self.scope = os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
        self.model, self.max_tokens = model, max_tokens
        self.http = httpx.Client(timeout=120, verify=_ssl_context())
        self._token, self._exp = None, 0.0

    def _auth(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        r = self.http.post(OAUTH_URL, data={"scope": self.scope}, headers={
            "Authorization": f"Basic {self.key}",
            "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        })
        r.raise_for_status()
        d = r.json()
        self._token = d["access_token"]
        self._exp = d.get("expires_at", 0) / 1000 or time.time() + 1500  # токен живёт 30 мин
        return self._token

    def chat(self, system: str, user: str, temperature: float = 0.4) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": self.max_tokens,
            "temperature": temperature,
        }
        for attempt in range(5):
            r = self.http.post(f"{API_URL}/chat/completions", json=payload,
                               headers={"Authorization": f"Bearer {self._auth()}"})
            if r.status_code == 401:          # токен истёк — получить новый
                self._token = None
                continue
            if r.status_code in (429, 500, 502, 503):  # у физлиц один поток — ждём и повторяем
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        raise RuntimeError(f"GigaChat не ответил: {r.status_code} {r.text[:200]}")
