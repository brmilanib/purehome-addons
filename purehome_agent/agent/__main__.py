"""Ponto de entrada: `python -m agent` (no add-on) ou `python -m agent --dry-run ...` (teste local)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import platform
from pathlib import Path
from typing import Any

import aiohttp

from . import VERSION
from .config import Options, StateStore, data_dir, load_options
from .ha_ws import HAWebSocket
from .metrics import collect, hardware_info
from .runner import Agent
from .supervisor import SupervisorClient
from .tasks import Executor, TaskResult


class DryRunExecutor(Executor):
    """Fora do Home Assistant não há o que executar: a tarefa volta como erro, deixando claro que foi dry-run."""

    async def run(self, task: dict[str, Any]) -> TaskResult:
        return TaskResult("erro", f"Modo dry-run: tarefa '{task.get('type')}' recebida, mas não executada (agente fora do Home Assistant).")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="purehome-agent", description="PureHome Agent")
    p.add_argument("--dry-run", action="store_true", help="roda fora do HA contra um portal (ex.: preview da Vercel); não executa tarefas")
    p.add_argument("--portal", help="URL do portal (dry-run)")
    p.add_argument("--code", help="código de ativação (dry-run)")
    p.add_argument("--data", help="pasta de estado (dry-run; padrão ./.dryrun)")
    p.add_argument("--once", action="store_true", help="um ciclo só e sai")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger("purehome")
    log.info("PureHome Agent %s%s", VERSION, " (dry-run)" if args.dry_run else "")

    async with aiohttp.ClientSession() as session:
        if args.dry_run:
            pasta = Path(args.data or ".dryrun")
            os.environ["PUREHOME_DATA"] = str(pasta)
            portal = (args.portal or "http://localhost:3000").rstrip("/")
            code = (args.code or "").strip().upper()
            store = StateStore(pasta / "agent.json")
            sup = SupervisorClient(session, "http://127.0.0.1:9", "")

            async def hw() -> dict[str, Any]:
                return {"hardware_model": f"dry-run ({platform.system()} {platform.machine()})", "ha_version": None,
                        "agent_version": f"{VERSION}-dryrun", "local_ip": None}

            async def metrics() -> dict[str, Any]:
                return {"agent_version": f"{VERSION}-dryrun"}

            agent = Agent(session, store, DryRunExecutor(sup, lambda: None),  # type: ignore[arg-type]
                          hw, metrics, options_loader=lambda: Options(code, portal), wait_unregistered_s=30)
        else:
            store = StateStore(data_dir() / "agent.json")
            sup = SupervisorClient(session)
            executor = Executor(sup, lambda: HAWebSocket(session, sup.websocket_url, sup.token))
            agent = Agent(session, store, executor, lambda: hardware_info(sup), lambda: collect(sup), options_loader=load_options)

        if args.once:
            await agent.run_once()
        else:
            await agent.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
