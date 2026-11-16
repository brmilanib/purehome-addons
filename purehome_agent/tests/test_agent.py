"""Testes do PureHome Agent com Supervisor, WebSocket do HA e portal falsos (servidores aiohttp locais).

Rodar (na pasta purehome_agent):  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp  # noqa: E402

from agent.config import Options, State, StateStore, load_options  # noqa: E402
from agent.ha_ws import HAWebSocket  # noqa: E402
from agent.metrics import collect, hardware_info, latest_backup_iso, primary_ip  # noqa: E402
from agent.portal import PortalClient, PortalError, Revoked  # noqa: E402
from agent.runner import Agent  # noqa: E402
from agent.supervisor import SupervisorClient  # noqa: E402
from agent.tasks import PROVISIONAR_INDISPONIVEL, Executor  # noqa: E402

TOKEN_SUP = "sup-token"


def ok(data: Any) -> web.Response:
    return web.json_response({"result": "ok", "data": data})


class FakeHA:
    """Supervisor + proxy do WebSocket do Core."""

    def __init__(self) -> None:
        self.version = "2026.10.3"
        self.latest = "2026.10.3"
        self.backup_api = True
        self.backup_fail = False
        self.calls: list[str] = []
        self.backups = [{"slug": "a", "date": "2026-11-15T03:00:00+00:00"}, {"slug": "b", "date": "2026-11-16T03:00:00+00:00"}]

    def app(self) -> web.Application:
        app = web.Application()
        r = app.router
        r.add_get("/host/info", self._wrap(lambda: {"operating_system": "Home Assistant OS 16.3", "chassis": "vm", "disk_total": 32.0, "disk_used": 8.0}))
        r.add_get("/info", self._wrap(lambda: {"machine": "qemux86-64", "arch": "amd64"}))
        r.add_get("/core/info", self._wrap(lambda: {"version": self.version, "version_latest": self.latest, "update_available": self.version != self.latest}))
        r.add_get("/core/stats", self._wrap(lambda: {"cpu_percent": 3.0, "memory_percent": 20.0}))
        r.add_get("/network/info", self._wrap(lambda: {"interfaces": [
            {"interface": "docker0", "primary": False, "connected": True, "ipv4": {"address": ["172.30.32.1/23"]}},
            {"interface": "enp0s3", "primary": True, "connected": True, "ipv4": {"address": ["192.168.0.168/24"]}},
        ]}))
        r.add_get("/backups", self._wrap(lambda: {"backups": self.backups}))
        r.add_post("/core/update", self._update)
        r.add_post("/backups/new/full", self._new_full)
        r.add_get("/core/websocket", self._ws)
        return app

    def _wrap(self, fn):
        async def h(request: web.Request) -> web.Response:
            if request.headers.get("Authorization") != f"Bearer {TOKEN_SUP}":
                return web.json_response({"result": "error", "message": "unauthorized"}, status=401)
            self.calls.append(request.path)
            return ok(fn())
        return h

    async def _update(self, request: web.Request) -> web.Response:
        self.calls.append("update")
        self.version = self.latest
        return ok({})

    async def _new_full(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.calls.append(f"new_full:{body.get('name')}")
        return ok({"slug": "local123"})

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        auth = await ws.receive_json()
        if auth.get("access_token") != TOKEN_SUP:
            await ws.send_json({"type": "auth_invalid"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok"})
        sub_id = None
        async for msg in ws:
            m = json.loads(msg.data)
            mid, t = m["id"], m["type"]
            self.calls.append(f"ws:{t}")
            res = lambda r: ws.send_json({"id": mid, "type": "result", "success": True, "result": r})  # noqa: E731
            if t == "backup/subscribe_events":
                sub_id = mid
                await res(None)
                await ws.send_json({"id": mid, "type": "event", "event": {"manager_state": "idle"}})
            elif t == "backup/generate_with_automatic_settings":
                if not self.backup_api:
                    await ws.send_json({"id": mid, "type": "result", "success": False, "error": {"code": "unknown_command", "message": "Unknown command."}})
                    continue
                await res({"backup_job_id": "job1"})
                for st in (["in_progress", "failed"] if self.backup_fail else ["in_progress", "completed"]):
                    await ws.send_json({"id": sub_id, "type": "event", "event": {"manager_state": "create_backup", "stage": None, "state": st}})
            elif t == "backup/info":
                await res({"backups": [
                    {"backup_id": "old", "name": "Antigo", "date": "2026-11-10T03:00:00+00:00", "agents": {"hassio.local": {"size": 10}}},
                    {"backup_id": "new", "name": "Automatic backup 2026.10.3", "date": "2026-11-16T15:00:00+00:00",
                     "agents": {"hassio.local": {"protected": True, "size": 52428800}, "google_drive.abc": {"protected": True, "size": 52428800}}},
                ]})
            elif t == "config_entries/flow/progress":
                await res([
                    {"flow_id": "f1", "handler": "cast", "context": {"source": "zeroconf", "unique_id": "uuid-tv", "title_placeholders": {"name": "TV Sala"}}},
                    {"flow_id": "f2", "handler": "unifi", "context": {"source": "ssdp", "unique_id": "udm", "title_placeholders": {"host": "192.168.0.1", "site": "default"}}},
                    {"flow_id": "f3", "handler": "hue", "context": {"source": "user"}},
                ])
            elif t == "config/device_registry/list":
                await res([{"id": "d1"}, {"id": "d2"}, {"id": "d3"}])
            elif t == "config/area_registry/list":
                await res([{"name": "Showroom"}, {"name": "Escritório"}])
            else:
                await ws.send_json({"id": mid, "type": "result", "success": False, "error": {"code": "unknown_command", "message": "?"}})
        return ws


class FakePortal:
    def __init__(self) -> None:
        self.code = "PH-TEST-0001"
        self.token = "tok-" + "x" * 40
        self.revoked = False
        self.heartbeats: list[dict] = []
        self.results: dict[int, dict] = {}
        self.queue = [{"id": 7, "type": "descobrir", "payload": {}}, {"id": 8, "type": "provisionar", "payload": {}}]
        self.fail_next = 0

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/api/agent/register", self._register)
        app.router.add_post("/api/agent/heartbeat", self._heartbeat)
        app.router.add_get("/api/agent/tasks", self._tasks)
        app.router.add_post("/api/agent/tasks/{id}/result", self._result)
        # imita a proteção da Vercel (401 com outro formato de corpo), liberada pelo header de bypass
        app.router.add_post("/vercel/api/agent/heartbeat", self._vercel)
        return app

    async def _vercel(self, request: web.Request) -> web.Response:
        if request.headers.get("x-vercel-protection-bypass") != "chave-teste":
            return web.json_response({"error": {"message": "Protected deployment", "code": "401"}}, status=401)
        return web.json_response({"ok": True})

    def _auth(self, request: web.Request) -> web.Response | None:
        if self.fail_next:
            self.fail_next -= 1
            return web.json_response({"error": "instável"}, status=503)
        if self.revoked or request.headers.get("Authorization") != f"Bearer {self.token}":
            return web.json_response({"error": "Token do agente inválido ou revogado"}, status=401)
        return None

    async def _register(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("activation_code") != self.code:
            return web.json_response({"error": "Código de ativação inválido"}, status=404)
        self.registered = body
        return web.json_response({"installation_id": 42, "agent_token": self.token, "poll_interval_s": 15, "heartbeat_interval_s": 60})

    async def _heartbeat(self, request: web.Request) -> web.Response:
        if (r := self._auth(request)) is not None:
            return r
        self.heartbeats.append(await request.json())
        return web.json_response({"ok": True})

    async def _tasks(self, request: web.Request) -> web.Response:
        if (r := self._auth(request)) is not None:
            return r
        tasks, self.queue = self.queue, []
        return web.json_response({"tasks": tasks})

    async def _result(self, request: web.Request) -> web.Response:
        if (r := self._auth(request)) is not None:
            return r
        self.results[int(request.match_info["id"])] = await request.json()
        return web.json_response({"ok": True})


def fake_proc(root: Path) -> Path:
    (root / "stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    (root / "meminfo").write_text("MemTotal:       8000000 kB\nMemFree:  1000000 kB\nMemAvailable:   6000000 kB\n")
    (root / "uptime").write_text("3600.55 100.0\n")
    return root


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.ha = FakeHA()
        self.ha_srv = TestServer(self.ha.app())
        await self.ha_srv.start_server()
        self.session = aiohttp.ClientSession()
        self.sup = SupervisorClient(self.session, str(self.ha_srv.make_url("")), TOKEN_SUP)
        self.executor = Executor(self.sup, lambda: HAWebSocket(self.session, self.sup.websocket_url, TOKEN_SUP))

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.ha_srv.close()


class TestMetrics(Base):
    async def test_collect_reais(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            m = await collect(self.sup, fake_proc(Path(d)))
        self.assertEqual(m["mem_pct"], 25.0)
        self.assertEqual(m["disk_pct"], 25.0)
        self.assertEqual(m["ha_version"], "2026.10.3")
        self.assertEqual(m["local_ip"], "192.168.0.168")
        self.assertEqual(m["uptime_s"], 3600)
        self.assertEqual(m["last_backup_at"], "2026-11-16T03:00:00+00:00")
        # /proc/stat estático → sem variação → cai para /core/stats
        self.assertEqual(m["cpu_pct"], 3.0)

    async def test_hardware_info(self) -> None:
        info = await hardware_info(self.sup)
        self.assertEqual(info["hardware_model"], "Home Assistant OS 16.3 · qemux86-64 (vm)")
        self.assertEqual(info["local_ip"], "192.168.0.168")

    def test_helpers(self) -> None:
        self.assertIsNone(primary_ip({}))
        self.assertIsNone(latest_backup_iso({"backups": [{"date": "x"}]}))


class TestTasks(Base):
    async def test_backup_destinos_automaticos(self) -> None:
        r = await self.executor.run({"type": "backup"})
        self.assertEqual(r.status, "ok", r.text)
        self.assertEqual(r.data["destinations"], ["Google Drive", "local"])
        self.assertIn("50.0 MB", r.text)
        self.assertEqual(r.data["name"], "Automatic backup 2026.10.3")

    async def test_backup_falha(self) -> None:
        self.ha.backup_fail = True
        r = await self.executor.run({"type": "backup"})
        self.assertEqual(r.status, "erro")

    async def test_backup_fallback_local(self) -> None:
        self.ha.backup_api = False
        r = await self.executor.run({"type": "backup"})
        self.assertEqual(r.status, "ok")
        self.assertIn("só no armazenamento local", r.text)
        self.assertTrue(any(c.startswith("new_full:") for c in self.ha.calls))

    async def test_atualizar_ja_atualizado(self) -> None:
        r = await self.executor.run({"type": "atualizar"})
        self.assertEqual(r.status, "ok")
        self.assertIn("já está atualizado", r.text)
        self.assertNotIn("update", self.ha.calls)

    async def test_atualizar_com_backup_antes(self) -> None:
        self.ha.latest = "2026.11.0"
        r = await self.executor.run({"type": "atualizar"})
        self.assertEqual(r.status, "ok", r.text)
        self.assertEqual((r.data["before"], r.data["after"]), ("2026.10.3", "2026.11.0"))
        idx_backup = self.ha.calls.index("ws:backup/generate_with_automatic_settings")
        self.assertLess(idx_backup, self.ha.calls.index("update"))

    async def test_descobrir(self) -> None:
        r = await self.executor.run({"type": "descobrir"})
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.text, "2 descobertos, 3 dispositivos já configurados, 2 áreas")
        keys = {d["key"]: d for d in r.data["discovered"]}
        self.assertEqual(set(keys), {"cast:uuid-tv", "unifi:udm"})
        self.assertEqual(keys["cast:uuid-tv"]["title"], "TV Sala")
        self.assertEqual(keys["unifi:udm"]["host"], "192.168.0.1")

    async def test_provisionar_nao_simula(self) -> None:
        r = await self.executor.run({"type": "provisionar", "payload": {"areas": []}})
        self.assertEqual((r.status, r.text), ("erro", PROVISIONAR_INDISPONIVEL))

    async def test_tipo_desconhecido(self) -> None:
        r = await self.executor.run({"type": "formatar"})
        self.assertEqual(r.status, "erro")
        self.assertIn("desconhecido", r.text)


class TestPortalEAgente(Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.portal = FakePortal()
        self.portal_srv = TestServer(self.portal.app())
        await self.portal_srv.start_server()
        self.url = str(self.portal_srv.make_url("")).rstrip("/")
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / "agent.json")

    async def asyncTearDown(self) -> None:
        await self.portal_srv.close()
        self.tmp.cleanup()
        await super().asyncTearDown()

    def agent(self, code: str = "PH-TEST-0001") -> Agent:
        async def metrics() -> dict:
            return {"cpu_pct": 10.0, "mem_pct": 20.0, "disk_pct": 25.0, "ha_version": "2026.10.3"}
        return Agent(self.session, self.store, self.executor, lambda: hardware_info(self.sup), metrics,
                     options_loader=lambda: Options(code, self.url), wait_unregistered_s=0.01)

    async def test_cliente_erros(self) -> None:
        c = PortalClient(self.session, self.url, "errado")
        with self.assertRaises(Revoked):
            await c.heartbeat({})
        self.portal.fail_next = 1
        with self.assertRaises(PortalError) as e:
            await PortalClient(self.session, self.url, self.portal.token).heartbeat({})
        self.assertTrue(e.exception.retry)
        with self.assertRaises(PortalError) as e:
            await c.register("PH-ERRADO", {})
        self.assertFalse(e.exception.retry)
        self.assertIn("inválido", str(e.exception))
        with self.assertRaises(PortalError) as e:
            await PortalClient(self.session, "http://127.0.0.1:9").heartbeat({})
        self.assertTrue(e.exception.retry)

    async def test_protecao_vercel_nao_revoga(self) -> None:
        c = PortalClient(self.session, self.url + "/vercel", self.portal.token)
        with self.assertRaises(PortalError) as e:
            await c.heartbeat({})
        self.assertNotIsInstance(e.exception, Revoked)
        self.assertTrue(e.exception.retry)
        self.assertIn("proteção da Vercel", str(e.exception))
        await PortalClient(self.session, self.url + "/vercel", self.portal.token, "chave-teste").heartbeat({})

    async def test_ciclo_completo_sem_vazar_token(self) -> None:
        with self.assertLogs("purehome", level="INFO") as logs:
            await self.agent().run_once()
        st = self.store.load()
        self.assertEqual((st.installation_id, st.agent_token), (42, self.portal.token))
        self.assertEqual(self.portal.registered["local_ip"], "192.168.0.168")
        self.assertEqual(len(self.portal.heartbeats), 1)
        self.assertEqual(self.portal.results[7]["status"], "ok")
        self.assertEqual(len(self.portal.results[7]["result"]["discovered"]), 2)
        self.assertEqual(self.portal.results[8], {"status": "erro", "result_text": PROVISIONAR_INDISPONIVEL, "result": None})
        self.assertNotIn(self.portal.token, "\n".join(logs.output))

    async def test_revogado_apaga_token(self) -> None:
        await self.agent().run_once()
        self.portal.revoked = True
        with self.assertLogs("purehome", level="ERROR") as logs:
            await self.agent().run_once()
        self.assertIsNone(self.store.load())
        self.assertIn("Agente revogado", "\n".join(logs.output))

    async def test_resultado_reenviado_quando_portal_cai(self) -> None:
        self.store.save(State(42, self.portal.token))
        self.portal.fail_next = 1
        client = PortalClient(self.session, self.url, self.portal.token)
        import agent.portal as portal_mod
        orig = portal_mod.Backoff.next
        portal_mod.Backoff.next = lambda self: 0.01  # type: ignore[method-assign]
        try:
            await self.agent().run_task(client, {"id": 9, "type": "provisionar"})
        finally:
            portal_mod.Backoff.next = orig  # type: ignore[method-assign]
        self.assertEqual(self.portal.results[9]["status"], "erro")

    async def test_sem_codigo_espera(self) -> None:
        a = self.agent(code="")
        with self.assertLogs("purehome", level="WARNING") as logs:
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(a.ensure_registered(), 0.1)
        self.assertIn("Sem código de ativação", "\n".join(logs.output))


class TestConfig(unittest.TestCase):
    def test_options_e_estado(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "options.json"
            p.write_text(json.dumps({"activation_code": " ph-abcd-efgh ", "portal_url": "https://x.test/"}))
            o = load_options(p)
            self.assertEqual((o.activation_code, o.portal_url), ("PH-ABCD-EFGH", "https://x.test"))
            s = StateStore(Path(d) / "agent.json")
            self.assertIsNone(s.load())
            s.save(State(1, "t"))
            self.assertEqual(s.load().agent_token, "t")
            s.clear()
            self.assertIsNone(s.load())


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()
