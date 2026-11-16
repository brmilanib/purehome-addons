"""Executores das tarefas pedidas pelo portal: backup, atualizar, descobrir, provisionar."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .ha_ws import HAError, HAWebSocket
from .supervisor import SupervisorClient, SupervisorError

log = logging.getLogger("purehome")

# tempo máximo por tipo de tarefa (s); o portal marca "tempo esgotado" com 30 min
TIMEOUTS = {"backup": 25 * 60, "atualizar": 28 * 60, "descobrir": 120, "provisionar": 60}

PROVISIONAR_INDISPONIVEL = "Aplicar configuração PureHome ainda não disponível nesta versão do agente"


@dataclass
class TaskResult:
    status: str  # "ok" | "erro"
    text: str
    data: dict[str, Any] | None = field(default=None)


WsFactory = Callable[[], HAWebSocket]


def _fmt_bytes(n: float | None) -> str:
    if not n:
        return "tamanho desconhecido"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _destino(agent_id: str) -> str:
    if agent_id.startswith("google_drive"):
        return "Google Drive"
    if agent_id in ("hassio.local", "backup.local") or agent_id.endswith(".local"):
        return "local"
    return agent_id


def _latest(backups: list[dict[str, Any]]) -> dict[str, Any] | None:
    def when(b: dict[str, Any]) -> datetime:
        try:
            d = datetime.fromisoformat(str(b.get("date")).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    validos = [b for b in backups if b.get("date")]
    return max(validos, key=when) if validos else None


class Executor:
    """Executa uma tarefa por vez. Para criar a próxima fase (provisionar), registre em `extra_handlers`."""

    def __init__(self, sup: SupervisorClient, ws_factory: WsFactory) -> None:
        self.sup = sup
        self.ws_factory = ws_factory
        self.extra_handlers: dict[str, Callable[[dict[str, Any]], Awaitable[TaskResult]]] = {}

    async def run(self, task: dict[str, Any]) -> TaskResult:
        kind = str(task.get("type") or "")
        payload = task.get("payload") or {}
        handler = self.extra_handlers.get(kind) or {
            "backup": self.backup,
            "atualizar": self.atualizar,
            "descobrir": self.descobrir,
            "provisionar": self.provisionar,
        }.get(kind)
        if handler is None:
            return TaskResult("erro", f"Tipo de tarefa desconhecido para este agente: '{kind}'")
        try:
            return await asyncio.wait_for(handler(payload), TIMEOUTS.get(kind, 300))
        except asyncio.TimeoutError:
            return TaskResult("erro", f"Tempo esgotado executando '{kind}' no equipamento")
        except (SupervisorError, HAError) as e:
            return TaskResult("erro", f"Falha ao executar '{kind}': {e}")
        except Exception as e:  # nunca derrubar o agente por causa de uma tarefa
            log.exception("Erro inesperado na tarefa %s", kind)
            return TaskResult("erro", f"Erro inesperado no agente: {type(e).__name__}: {e}")

    # ───────────── backup ─────────────
    async def backup(self, payload: dict[str, Any] | None = None) -> TaskResult:
        """Backup pelos destinos já configurados no HA (Google Drive + local). Sem a API nova: backup local pelo Supervisor."""
        try:
            async with self.ws_factory() as ws:
                events = await ws.subscribe("backup/subscribe_events")
                job = await ws.call("backup/generate_with_automatic_settings", timeout=60)
                log.info("Backup iniciado (job %s); aguardando conclusão...", (job or {}).get("backup_job_id"))
                state = await self._esperar_backup(events)
                if state == "failed":
                    return TaskResult("erro", "O Home Assistant informou falha ao criar o backup. Veja Configurações → Sistema → Backups.")
                info = await ws.call("backup/info", timeout=60)
        except HAError as e:
            if e.code != "unknown_command":
                raise
            return await self._backup_supervisor()

        last = _latest((info or {}).get("backups") or [])
        if not last:
            return TaskResult("ok", "Backup concluído (não foi possível ler os detalhes).", {"job": job})
        agents: dict[str, Any] = last.get("agents") or {a: {} for a in last.get("agent_ids") or []}
        size = last.get("size") or max((float(v.get("size") or 0) for v in agents.values()), default=0)
        destinos = sorted({_destino(a) for a in agents})
        data = {
            "backup_id": last.get("backup_id"), "name": last.get("name"), "date": last.get("date"),
            "size_bytes": size, "destinations": destinos, "agents": list(agents),
        }
        return TaskResult("ok", f"Backup “{last.get('name')}” criado ({_fmt_bytes(size)}) em: {', '.join(destinos) or 'local'}.", data)

    async def _esperar_backup(self, events: asyncio.Queue) -> str:
        visto = False
        while True:
            ev = await events.get()
            if not isinstance(ev, dict):
                continue
            if ev.get("manager_state") == "create_backup":
                visto = True
                if ev.get("state") in ("completed", "failed"):
                    return str(ev["state"])
            elif visto and ev.get("manager_state") == "idle":
                return "completed"

    async def _backup_supervisor(self) -> TaskResult:
        nome = f"PureHome {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        data = await self.sup.call("POST", "/backups/new/full", {"name": nome}, timeout=TIMEOUTS["backup"])
        return TaskResult(
            "ok",
            f"Backup “{nome}” criado só no armazenamento local (esta versão do HA não tem a API de backup com destinos automáticos).",
            {"slug": data.get("slug"), "name": nome, "destinations": ["local"]},
        )

    # ───────────── atualizar (só o Core) ─────────────
    async def atualizar(self, payload: dict[str, Any] | None = None) -> TaskResult:
        info = await self.sup.get("/core/info")
        antes, ultima = info.get("version"), info.get("version_latest")
        if not info.get("update_available"):
            return TaskResult("ok", f"Sistema já está atualizado (versão {antes}).", {"before": antes, "after": antes, "updated": False})
        log.info("Atualização disponível: %s → %s. Fazendo backup antes...", antes, ultima)
        bk = await self.backup()
        if bk.status != "ok":
            return TaskResult("erro", f"Atualização cancelada: o backup prévio falhou ({bk.text})", {"before": antes, "backup": bk.data})
        await self.sup.call("POST", "/core/update", {}, timeout=TIMEOUTS["atualizar"])
        depois = antes
        for _ in range(60):  # o Core reinicia; espera até 5 min para ler a versão nova
            try:
                depois = (await self.sup.get("/core/info")).get("version")
                if depois != antes:
                    break
            except SupervisorError:
                pass
            await asyncio.sleep(5)
        if depois == antes:
            return TaskResult("erro", f"A atualização foi pedida, mas a versão continua {antes}. Verifique o log do Supervisor.", {"before": antes, "after": depois})
        return TaskResult("ok", f"Sistema atualizado de {antes} para {depois}. Backup prévio: {bk.text}",
                          {"before": antes, "after": depois, "updated": True, "backup": bk.data})

    # ───────────── descobrir ─────────────
    async def descobrir(self, payload: dict[str, Any] | None = None) -> TaskResult:
        async with self.ws_factory() as ws:
            flows = await ws.call("config_entries/flow/progress") or []
            devices = await ws.call("config/device_registry/list") or []
            areas = await ws.call("config/area_registry/list") or []
        descobertos = []
        for f in flows:
            ctx = f.get("context") or {}
            if ctx.get("source") in ("user", "reauth", "reconfigure"):
                continue  # fluxos abertos à mão por alguém não são "descobertos"
            ph = ctx.get("title_placeholders") or {}
            handler = f.get("handler") or "?"
            uid = ctx.get("unique_id") or f.get("flow_id")
            descobertos.append({
                "key": f"{handler}:{uid}",
                "domain": handler,
                "title": ph.get("name") or ph.get("title") or ph.get("model") or handler,
                "flow_id": f.get("flow_id"),
                "host": ph.get("host") or ph.get("ip") or ctx.get("host"),
                "source": ctx.get("source"),
            })
        resumo = f"{len(descobertos)} descobertos, {len(devices)} dispositivos já configurados, {len(areas)} áreas"
        return TaskResult("ok", resumo, {
            "discovered": descobertos,
            "summary": {"discovered": len(descobertos), "devices": len(devices), "areas": len(areas)},
            "areas": [a.get("name") for a in areas if a.get("name")],
        })

    # ───────────── provisionar (próxima fase: repositório PureHome Base) ─────────────
    async def provisionar(self, payload: dict[str, Any]) -> TaskResult:
        return TaskResult("erro", PROVISIONAR_INDISPONIVEL)
