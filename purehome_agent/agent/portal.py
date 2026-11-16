"""Cliente HTTP da API do agente no portal (/api/agent/*)."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import aiohttp

log = logging.getLogger("purehome")


class PortalError(Exception):
    """Erro do portal. `retry` indica se vale tentar de novo (rede, 5xx, 429)."""

    def __init__(self, message: str, status: int | None = None, retry: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retry = retry


class Revoked(PortalError):
    """401: token inválido ou revogado no portal."""


class PortalClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, token: str | None = None, bypass: str | None = None) -> None:
        self.session = session
        self.base = base_url.rstrip("/")
        self.token = token
        # chave "Protection Bypass for Automation" da Vercel: só para testar contra um preview protegido
        self.bypass = bypass or os.environ.get("VERCEL_BYPASS") or None

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json", "User-Agent": "PureHomeAgent"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.bypass:
            h["x-vercel-protection-bypass"] = self.bypass
        return h

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        url = f"{self.base}{path}"
        try:
            async with self.session.request(
                method, url, json=body, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                erro = data.get("error")
                # a API do portal sempre responde {"error": "texto"}; outro formato vem de fora (ex.: proteção da Vercel)
                do_portal = isinstance(erro, str)
                if resp.status == 401 and do_portal:
                    raise Revoked(erro, 401)
                if resp.status in (401, 403) and not do_portal:
                    raise PortalError(
                        "Acesso bloqueado antes de chegar ao portal (proteção da Vercel?). "
                        "Confira o Endereço do portal e a chave de teste do preview.", resp.status, retry=True)
                if resp.status >= 400:
                    msg = erro if do_portal else f"HTTP {resp.status}"
                    raise PortalError(msg, resp.status, retry=resp.status >= 500 or resp.status == 429)
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise PortalError(f"Portal inacessível ({type(e).__name__})", retry=True) from e

    async def register(self, code: str, info: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/agent/register", {"activation_code": code, **info})

    async def heartbeat(self, metrics: dict[str, Any]) -> None:
        await self._request("POST", "/api/agent/heartbeat", metrics)

    async def tasks(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/agent/tasks")
        return list(data.get("tasks") or [])

    async def result(self, task_id: int, status: str, text: str, result: dict | None = None) -> None:
        await self._request(
            "POST", f"/api/agent/tasks/{task_id}/result", {"status": status, "result_text": text[:4000], "result": result}
        )


class Backoff:
    """Backoff exponencial: 5 s, 10 s, 20 s... até 5 min."""

    def __init__(self, start: float = 5, maximum: float = 300) -> None:
        self.start, self.maximum, self.current = start, maximum, start

    def next(self) -> float:
        d = self.current
        self.current = min(self.current * 2, self.maximum)
        return d

    def reset(self) -> None:
        self.current = self.start
