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

# Cardinal's per-session state home inside a whole-process seatbelt sandbox
# (Gemini CLI's `-s`: it runs the CLI, hooks included, under sandbox-exec and
# sets SANDBOX=sandbox-exec inside). Its profiles let a process write the
# workspace, temp and cache dirs and ~/.cache, never ~/.cardinal, so the
# hooks keep their Investigation binding / inbox / cursor / poller files
# (investigation_events.sessions_dir) and evidence spool
# (evidence.default_root) under <home>/.cache/cardinal/sandboxed/.cardinal.
# That spool and those bindings are agent-writable (the sandboxed agent can
# write ~/.cache): readers treat them as untrusted hints, as they treat any
# binding. cardinal-wake's own state (~/.cardinal/wake) is never relocated.
SANDBOX_ENV = "SANDBOX"
SEATBELT_SANDBOX = "sandbox-exec"
SANDBOXED_HOME_PARTS = (".cache", "cardinal", "sandboxed")


def in_seatbelt_sandbox(environ: Any = None) -> bool:
    """This process runs inside a whole-process seatbelt sandbox (SANDBOX=sandbox-exec)."""
    env = os.environ if environ is None else environ
    return env.get(SANDBOX_ENV) == SEATBELT_SANDBOX


def sandboxed_home(home: Path) -> Path:
    """The relocated Cardinal state home for `home`: <home>/.cache/cardinal/sandboxed."""
    home = Path(home)
    return home if is_sandboxed_home(home) else home.joinpath(*SANDBOXED_HOME_PARTS)


def is_sandboxed_home(home: Path) -> bool:
    return tuple(Path(home).parts[-len(SANDBOXED_HOME_PARTS):]) == SANDBOXED_HOME_PARTS


def unsandboxed_home(home: Path) -> Path:
    """The user's home for a (possibly relocated) state home."""
    home = Path(home)
    return home.parents[len(SANDBOXED_HOME_PARTS) - 1] if is_sandboxed_home(home) else home


def state_home(home: Path, environ: Any = None) -> Path:
    """Where Cardinal's per-session state lives: `home` (its ~/.cardinal), or
    sandboxed_home(home) inside a seatbelt sandbox. The one place this is
    decided; idempotent (a relocated home stays as it is)."""
    home = Path(home)
    if is_sandboxed_home(home):
        return home
    return sandboxed_home(home) if in_seatbelt_sandbox(environ) else home


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

    @property
    def owner_input_consent_path(self) -> Path:
        """The user's connect-time owner-input consent (owner_input.consent;
        only a runtime without a per-session disclosure channel needs it)."""
        return self.runtime_dir / "owner-input-consent.json"

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
