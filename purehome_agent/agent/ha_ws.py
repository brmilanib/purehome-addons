"""Cliente mínimo do WebSocket do Home Assistant Core (via proxy do Supervisor)."""
from __future__ import annotations

import asyncio
import itertools
from typing import Any

import aiohttp


class HAError(Exception):
    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class HAWebSocket:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        self.session, self.url, self.token = session, url, token
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._events: dict[int, asyncio.Queue] = {}
        self._reader: asyncio.Task | None = None

    async def __aenter__(self) -> "HAWebSocket":
        self.ws = await self.session.ws_connect(self.url, heartbeat=30, max_msg_size=0)
        msg = await self.ws.receive_json(timeout=15)
        if msg.get("type") != "auth_required":
            raise HAError(f"Handshake inesperado: {msg.get('type')}")
        await self.ws.send_json({"type": "auth", "access_token": self.token})
        msg = await self.ws.receive_json(timeout=15)
        if msg.get("type") != "auth_ok":
            raise HAError("Autenticação no WebSocket do Home Assistant recusada")
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._reader:
            self._reader.cancel()
        if self.ws:
            await self.ws.close()

    async def _read(self) -> None:
        assert self.ws
        try:
            async for raw in self.ws:
                if raw.type != aiohttp.WSMsgType.TEXT:
                    continue
                msg = raw.json()
                mid = msg.get("id")
                if msg.get("type") == "event" and mid in self._events:
                    self._events[mid].put_nowait(msg.get("event"))
                elif msg.get("type") == "result" and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        if msg.get("success"):
                            fut.set_result(msg.get("result"))
                        else:
                            err = msg.get("error") or {}
                            fut.set_exception(HAError(err.get("message") or "erro", err.get("code")))
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(HAError("Conexão com o Home Assistant encerrada"))

    async def call(self, type_: str, timeout: float = 60, **payload: Any) -> Any:
        assert self.ws
        mid = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send_json({"id": mid, "type": type_, **payload})
        return await asyncio.wait_for(fut, timeout)

    async def subscribe(self, type_: str, **payload: Any) -> asyncio.Queue:
        """Assina um comando de eventos (ex.: backup/subscribe_events) e devolve a fila de eventos."""
        assert self.ws
        mid = next(self._ids)
        queue: asyncio.Queue = asyncio.Queue()
        self._events[mid] = queue
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send_json({"id": mid, "type": type_, **payload})
        await asyncio.wait_for(fut, 30)
        return queue
