"""The Notetaker app, as the bot sees it: /api/bot/worker/* with a token."""
from __future__ import annotations

import os

import httpx

APP = os.environ.get("NOTETAKER_URL", "http://notetaker:8430").rstrip("/")
KEY_FILE = os.environ.get("BOT_KEY_FILE", "/bot-key/token")


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def read_token() -> str:
    env = os.environ.get("BOT_TOKEN", "").strip()
    if env:
        return env
    try:
        with open(KEY_FILE) as fh:
            return fh.read().strip()
    except OSError:
        return ""


class Api:
    def __init__(self):
        self.http = httpx.AsyncClient(base_url=f"{APP}/api/bot/worker",
                                      timeout=httpx.Timeout(60.0, connect=10.0))

    def _headers(self, ctype: str = "application/json") -> dict:
        return {"Authorization": f"Bearer {read_token()}", "Content-Type": ctype}

    @staticmethod
    def _check(r: httpx.Response) -> dict:
        if r.status_code >= 400:
            try:
                msg = r.json().get("detail") or r.text
            except ValueError:
                msg = r.text
            raise ApiError(r.status_code, str(msg)[:300])
        return r.json()

    async def get(self, path: str, **params) -> dict:
        return self._check(await self.http.get(path, params=params, headers=self._headers()))

    async def post(self, path: str, body: dict) -> dict:
        return self._check(await self.http.post(path, json=body, headers=self._headers()))

    async def put(self, path: str, data: bytes, ctype: str = "application/octet-stream") -> dict:
        return self._check(await self.http.put(path, content=data, headers=self._headers(ctype)))
