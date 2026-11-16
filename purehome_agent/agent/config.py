"""Opções do add-on (/data/options.json) e estado persistente (/data/agent.json)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORTAL = "https://www.purehome.com.br"


@dataclass
class Options:
    activation_code: str
    portal_url: str


def data_dir() -> Path:
    return Path(os.environ.get("PUREHOME_DATA", "/data"))


def load_options(path: Path | None = None) -> Options:
    path = path or data_dir() / "options.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    code = str(raw.get("activation_code") or "").strip().upper()
    portal = str(raw.get("portal_url") or DEFAULT_PORTAL).strip().rstrip("/")
    return Options(activation_code=code, portal_url=portal)


@dataclass
class State:
    installation_id: int
    agent_token: str
    poll_interval_s: int = 15
    heartbeat_interval_s: int = 60


class StateStore:
    """Guarda installation_id e token em /data/agent.json (permissão 600). O token nunca vai para o log."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "agent.json"

    def load(self) -> State | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return State(
                installation_id=int(raw["installation_id"]),
                agent_token=str(raw["agent_token"]),
                poll_interval_s=int(raw.get("poll_interval_s", 15)),
                heartbeat_interval_s=int(raw.get("heartbeat_interval_s", 60)),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, state: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state.__dict__), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
