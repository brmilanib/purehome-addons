"""Coleta de métricas reais do equipamento para o registro e o heartbeat."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import VERSION
from .supervisor import SupervisorClient, SupervisorError

log = logging.getLogger("purehome")
PROC = Path("/proc")


def _cpu_times(proc: Path) -> tuple[int, int] | None:
    try:
        parts = (proc / "stat").read_text().splitlines()[0].split()[1:]
        vals = [int(x) for x in parts]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return idle, sum(vals)
    except (OSError, ValueError, IndexError):
        return None


async def host_cpu_pct(proc: Path = PROC, interval: float = 1.0) -> float | None:
    """CPU do sistema inteiro (a /proc/stat dentro do contêiner é a do kernel do equipamento)."""
    a = _cpu_times(proc)
    await asyncio.sleep(interval)
    b = _cpu_times(proc)
    if not a or not b or b[1] == a[1]:
        return None
    return round(100.0 * (1 - (b[0] - a[0]) / (b[1] - a[1])), 1)


def host_mem_pct(proc: Path = PROC) -> float | None:
    try:
        info: dict[str, int] = {}
        for line in (proc / "meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])
        total = info["MemTotal"]
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        return round(100.0 * (total - avail) / total, 1) if total else None
    except (OSError, ValueError, KeyError):
        return None


def host_uptime_s(proc: Path = PROC) -> int | None:
    try:
        return int(float((proc / "uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def primary_ip(network_info: dict[str, Any]) -> str | None:
    ifaces = network_info.get("interfaces") or []
    ordered = sorted(ifaces, key=lambda i: (not i.get("primary"), not i.get("connected")))
    for iface in ordered:
        for addr in (iface.get("ipv4") or {}).get("address") or []:
            ip = str(addr).split("/")[0]
            if ip and not ip.startswith("127."):
                return ip
    return None


def latest_backup_iso(backups: dict[str, Any]) -> str | None:
    best: datetime | None = None
    for b in backups.get("backups") or []:
        try:
            d = datetime.fromisoformat(str(b.get("date")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        if best is None or d > best:
            best = d
    return best.isoformat() if best else None


async def safe(coro: Any, default: Any = None) -> Any:
    try:
        return await coro
    except SupervisorError as e:
        log.debug("Supervisor: %s", e)
        return default


async def hardware_info(sup: SupervisorClient) -> dict[str, Any]:
    """Dados enviados no registro: modelo, versão do HA, versão do agente e IP."""
    host = await safe(sup.get("/host/info"), {}) or {}
    info = await safe(sup.get("/info"), {}) or {}
    core = await safe(sup.get("/core/info"), {}) or {}
    net = await safe(sup.get("/network/info"), {}) or {}
    partes = [host.get("operating_system"), info.get("machine")]
    modelo = " · ".join(str(p) for p in partes if p) or "Home Assistant"
    if host.get("chassis"):
        modelo += f" ({host['chassis']})"
    return {
        "hardware_model": modelo,
        "ha_version": core.get("version"),
        "agent_version": VERSION,
        "local_ip": primary_ip(net),
    }


def _clamp(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return max(0.0, min(100.0, float(v)))
    except (TypeError, ValueError):
        return None


async def collect(sup: SupervisorClient, proc: Path = PROC) -> dict[str, Any]:
    host, core, net, backups, cpu = await asyncio.gather(
        safe(sup.get("/host/info"), {}), safe(sup.get("/core/info"), {}), safe(sup.get("/network/info"), {}),
        safe(sup.get("/backups"), {}), host_cpu_pct(proc),
    )
    host, core, net, backups = host or {}, core or {}, net or {}, backups or {}
    mem = host_mem_pct(proc)
    if cpu is None or mem is None:  # sem /proc legível: usa as estatísticas do contêiner do Core
        stats = await safe(sup.get("/core/stats"), {}) or {}
        cpu = cpu if cpu is not None else stats.get("cpu_percent")
        mem = mem if mem is not None else stats.get("memory_percent")
    disk = None
    if host.get("disk_total"):
        disk = 100.0 * float(host.get("disk_used") or 0) / float(host["disk_total"])
    return {
        "cpu_pct": _clamp(cpu),
        "mem_pct": _clamp(mem),
        "disk_pct": None if disk is None else round(_clamp(disk) or 0.0, 1),
        "ha_version": core.get("version"),
        "agent_version": VERSION,
        "local_ip": primary_ip(net),
        "last_backup_at": latest_backup_iso(backups),
        "uptime_s": host_uptime_s(proc),
    }
