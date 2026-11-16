"""Cliente da Supervisor API (http://supervisor) com o SUPERVISOR_TOKEN do add-on."""
from __future__ import annotations

import os
from typing import Any

import aiohttp


class SupervisorError(Exception):
    pass


class SupervisorClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str | None = None, token: str | None = None) -> None:
        self.session = session
        self.base = (base_url or os.environ.get("SUPERVISOR_URL", "http://supervisor")).rstrip("/")
        self.token = token if token is not None else os.environ.get("SUPERVISOR_TOKEN", "")

    async def call(self, method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict[str, Any]:
        try:
            async with self.session.request(
                method, f"{self.base}{path}", json=body,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                data = await resp.json(content_type=None)
        except Exception as e:  # rede, JSON inválido, timeout
            raise SupervisorError(f"{method} {path}: {type(e).__name__} {e}") from e
        if not isinstance(data, dict) or data.get("result") != "ok":
            msg = data.get("message") if isinstance(data, dict) else None
            raise SupervisorError(f"{method} {path}: {msg or 'resposta inválida'}")
        return data.get("data") or {}

    async def get(self, path: str) -> dict[str, Any]:
        return await self.call("GET", path)

    @property
    def websocket_url(self) -> str:
        return self.base.replace("https://", "wss://").replace("http://", "ws://") + "/core/websocket"
