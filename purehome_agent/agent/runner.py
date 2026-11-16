"""Laço principal: registro, heartbeat a cada 60 s e busca de tarefas a cada 15 s."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import aiohttp

from .config import Options, State, StateStore, load_options
from .portal import Backoff, PortalClient, PortalError, Revoked
from .tasks import Executor, TaskResult

log = logging.getLogger("purehome")

MetricsFn = Callable[[], Awaitable[dict[str, Any]]]


class Agent:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        store: StateStore,
        executor: Executor,
        hardware_info: MetricsFn,
        collect_metrics: MetricsFn,
        options_loader: Callable[[], Options] = load_options,
        wait_unregistered_s: float = 300,
    ) -> None:
        self.session = session
        self.store = store
        self.executor = executor
        self.hardware_info = hardware_info
        self.collect_metrics = collect_metrics
        self.options_loader = options_loader
        self.wait_unregistered_s = wait_unregistered_s

    # ───────────── registro ─────────────
    async def ensure_registered(self) -> tuple[State, Options]:
        backoff = Backoff()
        avisado = False
        while True:
            opts = self.options_loader()
            state = self.store.load()
            if state:
                return state, opts
            if not opts.activation_code:
                if not avisado:
                    log.warning("Sem código de ativação. Copie o código da instalação no Portal PureHome, cole na aba "
                                "Configuração deste add-on e reinicie o add-on.")
                    avisado = True
                await asyncio.sleep(self.wait_unregistered_s)
                continue
            try:
                info = await self.hardware_info()
                log.info("Registrando no portal %s com o código %s...", opts.portal_url, opts.activation_code)
                resp = await PortalClient(self.session, opts.portal_url).register(opts.activation_code, info)
                state = State(
                    installation_id=int(resp["installation_id"]),
                    agent_token=str(resp["agent_token"]),
                    poll_interval_s=int(resp.get("poll_interval_s") or 15),
                    heartbeat_interval_s=int(resp.get("heartbeat_interval_s") or 60),
                )
                self.store.save(state)
                log.info("Equipamento conectado ao portal (instalação #%s).", state.installation_id)
                return state, opts
            except PortalError as e:
                if e.retry:
                    espera = backoff.next()
                    log.warning("Portal indisponível no registro (%s). Nova tentativa em %.0f s.", e, espera)
                    await asyncio.sleep(espera)
                else:
                    log.error("Registro recusado pelo portal: %s. Gere um novo código no portal, "
                              "atualize a Configuração e reinicie o add-on.", e)
                    await asyncio.sleep(self.wait_unregistered_s)

    # ───────────── laços ─────────────
    async def heartbeat_loop(self, client: PortalClient, interval: float) -> None:
        backoff = Backoff()
        primeira = True
        while True:
            try:
                metrics = await self.collect_metrics()
                await client.heartbeat(metrics)
                if primeira:
                    log.info("Heartbeat ok (CPU %s%%, memória %s%%, disco %s%%, HA %s, IP %s).",
                             metrics.get("cpu_pct"), metrics.get("mem_pct"), metrics.get("disk_pct"),
                             metrics.get("ha_version"), metrics.get("local_ip"))
                    primeira = False
                backoff.reset()
                await asyncio.sleep(interval)
            except Revoked:
                raise
            except PortalError as e:
                espera = backoff.next() if e.retry else interval
                log.warning("Heartbeat falhou (%s). Nova tentativa em %.0f s.", e, espera)
                await asyncio.sleep(espera)

    async def tasks_loop(self, client: PortalClient, interval: float) -> None:
        backoff = Backoff()
        while True:
            try:
                tasks = await client.tasks()
                backoff.reset()
            except Revoked:
                raise
            except PortalError as e:
                espera = backoff.next() if e.retry else interval
                log.warning("Não foi possível buscar tarefas (%s). Nova tentativa em %.0f s.", e, espera)
                await asyncio.sleep(espera)
                continue
            for task in tasks:
                await self.run_task(client, task)
            await asyncio.sleep(interval)

    async def run_task(self, client: PortalClient, task: dict[str, Any]) -> TaskResult:
        tid, kind = task.get("id"), task.get("type")
        log.info("Tarefa #%s (%s) iniciada.", tid, kind)
        result = await self.executor.run(task)
        log.info("Tarefa #%s (%s): %s — %s", tid, kind, result.status, result.text)
        backoff = Backoff()
        while True:  # o resultado só é descartado se o portal recusar de vez
            try:
                await client.result(int(tid), result.status, result.text, result.data)
                return result
            except Revoked:
                raise
            except PortalError as e:
                if not e.retry:
                    log.error("Portal recusou o resultado da tarefa #%s: %s", tid, e)
                    return result
                espera = backoff.next()
                log.warning("Não foi possível enviar o resultado da tarefa #%s (%s). Nova tentativa em %.0f s.", tid, e, espera)
                await asyncio.sleep(espera)

    async def run_forever(self) -> None:
        while True:
            state, opts = await self.ensure_registered()
            client = PortalClient(self.session, opts.portal_url, state.agent_token)
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self.heartbeat_loop(client, state.heartbeat_interval_s))
                    tg.create_task(self.tasks_loop(client, state.poll_interval_s))
            except* Revoked:
                self.store.clear()
                log.error("Agente revogado — informe novo código de ativação na Configuração do add-on e reinicie.")

    async def run_once(self) -> None:
        """Um ciclo só (testes e --dry-run --once): registra, 1 heartbeat, 1 busca de tarefas."""
        state, opts = await self.ensure_registered()
        client = PortalClient(self.session, opts.portal_url, state.agent_token)
        try:
            await client.heartbeat(await self.collect_metrics())
            for task in await client.tasks():
                await self.run_task(client, task)
        except Revoked:
            self.store.clear()
            log.error("Agente revogado — informe novo código de ativação.")
