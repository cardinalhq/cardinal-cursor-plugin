"""Agent-home file layout and atomic write helpers.

AgentPaths is the state-store default: the existing per-agent file layout
under ~/.claude, ~/.codex, ~/.cursor, ~/.gemini. Every core function that
touches state takes an AgentPaths (or a value read from one) — never a
module-level constant — so a server-side consumer (omnigent adapter) can
point state anywhere or replace the layout entirely (spec §omnigent
constraints).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SESSION_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def safe_session(session_id: str) -> str:
    return SESSION_SAFE_RE.sub("_", session_id)[:128]


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(data, indent=2) + "\n")


def atomic_write_json_compact(path: Path, obj: dict[str, Any]) -> None:
    """tempfile + rename variant used by the limits gate so a sync reader
    never sees a half-written verdict."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def atomic_write_secret(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_suffix(f"{path.suffix}.bak.{ts}")
    target.write_text(path.read_text())
    return target


@dataclass(frozen=True)
class AgentPaths:
    """File layout under one agent's home directory.

    `home` is the agent dir itself (e.g. Path.home()/".codex"), not the
    user's home. All derived paths match the layout the four plugins
    already ship, so migration is byte-compatible.
    """

    home: Path

    @property
    def state_path(self) -> Path:
        return self.home / "cardinal.json"

    @property
    def secrets_path(self) -> Path:
        return self.home / "cardinal-secrets.json"

    @property
    def pending_path(self) -> Path:
        return self.home / "cardinal-pending.json"

    @property
    def runtime_dir(self) -> Path:
        return self.home / "cardinal"

    @property
    def telemetry_dir(self) -> Path:
        return self.runtime_dir / "telemetry"

    @property
    def limits_dir(self) -> Path:
        return self.runtime_dir / "limits"

    @property
    def plan_stamp_path(self) -> Path:
        return self.telemetry_dir / "plan.json"

    @property
    def debug_dir(self) -> Path:
        return self.telemetry_dir / "debug"

    def progress_path(self, session_id: str) -> Path:
        return self.telemetry_dir / f"{safe_session(session_id)}.json"

    def verdict_path(self, session_id: str) -> Path:
        return self.limits_dir / f"{safe_session(session_id)}.verdict.json"

    def ack_path(self, session_id: str) -> Path:
        return self.limits_dir / f"{safe_session(session_id)}.ack.json"

    def override_path(self, session_id: str) -> Path:
        return self.limits_dir / f"{safe_session(session_id)}.override.json"

    def notify_path(self, session_id: str) -> Path:
        return self.limits_dir / f"{safe_session(session_id)}.notify.json"

    def read_state(self) -> dict[str, Any]:
        return read_json(self.state_path)

    def read_secrets(self) -> dict[str, Any]:
        return read_json(self.secrets_path)
