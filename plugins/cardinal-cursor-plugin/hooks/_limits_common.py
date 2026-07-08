"""Shared spend-limits delivery helpers for the Cardinal Cursor hooks.

Cursor port of the Codex plugin's _limits_common.py — same server-side
contract, same file layout, retargeted at Cursor paths. Keep the three
plugins (Claude, Codex, Cursor) in lockstep per
docs/specs/cursor-parity.md §Keeping the repos in lockstep.

The limits feature splits across the single telemetry hook's events so
the turn-critical path never touches the network:

  beforeSubmitPrompt  — sync gate FIRST (file I/O only → hook JSON:
                        `continue:false` + `user_message` for block),
                        then the git_state OTLP post, then a TTL-driven
                        verdict refresh (network, short timeout). Notify
                        / warn context is STAGED for the next
                        turn-boundary hook (postToolUse) because
                        beforeSubmitPrompt has no `additional_context`
                        output slot in Cursor's schema.
  sessionStart        — one synchronous forced fetch (short timeout,
                        fail open) so budget standing is in context from
                        turn one; delivered via `additional_context`.

File layout under ~/.cursor/cardinal/limits/ — single-writer ownership:

  <conv>.verdict.json   written by the refresh; server response plus a
                        fetched_at stamp.
  <conv>.ack.json       written by the gate; last band surfaced
                        (hysteresis state).
  <conv>.notify.json    written by the gate; pending notify/warn message
                        for the next postToolUse to surface (unique to
                        the Cursor plugin — Claude/Codex use
                        systemMessage inline).
  <conv>.override.json  presence downgrades a block to warn-tier.

Everything is best-effort: any failure returns None / does nothing. A
missing verdict means "allow" — fail open is the contract.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


CURSOR_DIR = Path.home() / ".cursor"
STATE_PATH = CURSOR_DIR / "cardinal.json"
SECRETS_PATH = CURSOR_DIR / "cardinal-secrets.json"
LIMITS_DIR = CURSOR_DIR / "cardinal" / "limits"

FETCH_TIMEOUT_SEC = 2.0
DEFAULT_TTL_SEC = 120
WARN_MAX_AGE_SEC = 10 * 60
BLOCK_MAX_AGE_SEC = 60 * 60

# Opt-in escalation: with this env var set, a warn-band verdict becomes
# a hard block instead of a silent notify (Cursor divergence E — the
# only way to surface a warn-band message is via block copy, so users
# who want that behaviour opt in explicitly).
STRICT_WARN_ENV = "CARDINAL_CURSOR_STRICT_WARN"

_SESSION_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_REMOTE_URL_RE = re.compile(r"(?:git@|https?://)([^:/]+)[:/]([^/]+)/(.+?)(?:\.git)?/?$")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _read_json_file(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def limits_config() -> dict | None:
    """The limits block cardinal-connect persisted from the device-flow
    bundle. None = server doesn't speak the protocol / not connected —
    every limits path is a no-op then (zero overhead for older backends)."""
    limits = _read_json_file(STATE_PATH).get("limits")
    if not isinstance(limits, dict):
        return None
    url = limits.get("status_url")
    if not url or not limits.get("enabled", True):
        return None
    return {"status_url": url}


def ingest_api_key() -> str | None:
    """The plugin's ingest key from ~/.cursor/cardinal-secrets.json — the
    same credential the status endpoint authenticates (and derives engineer
    identity from, server-side)."""
    key = _read_json_file(SECRETS_PATH).get("ingest_api_key")
    return key if isinstance(key, str) and key else None


def strict_warn_enabled() -> bool:
    return os.environ.get(STRICT_WARN_ENV) == "1"


# ---------------------------------------------------------------------------
# Verdict / ack / notify / override files
# ---------------------------------------------------------------------------

def _safe_conv(conv_id: str) -> str:
    return _SESSION_ID_SAFE.sub("_", conv_id)[:128]


def verdict_path(conv_id: str) -> Path:
    return LIMITS_DIR / f"{_safe_conv(conv_id)}.verdict.json"


def ack_path(conv_id: str) -> Path:
    return LIMITS_DIR / f"{_safe_conv(conv_id)}.ack.json"


def notify_path(conv_id: str) -> Path:
    return LIMITS_DIR / f"{_safe_conv(conv_id)}.notify.json"


def override_path(conv_id: str) -> Path:
    return LIMITS_DIR / f"{_safe_conv(conv_id)}.override.json"


def read_verdict(conv_id: str) -> dict | None:
    v = _read_json_file(verdict_path(conv_id))
    return v or None


def atomic_write_json(path: Path, obj: dict) -> None:
    """tmp + rename so the sync gate never reads a half-written verdict."""
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


def consume_notify(conv_id: str) -> str | None:
    """Read-and-delete the staged notify message for this conversation.

    Called from `postToolUse` on the first tool call following a
    beforeSubmitPrompt that staged a notify/warn context. Returns None
    when nothing is staged. The one-shot semantics are intentional: we
    surface the standing once per band change; hysteresis lives in
    ack.json.
    """
    path = notify_path(conv_id)
    data = _read_json_file(path)
    msg = data.get("message") if isinstance(data, dict) else None
    if isinstance(msg, str) and msg:
        try:
            path.unlink()
        except OSError:
            pass
        return msg
    return None


# ---------------------------------------------------------------------------
# Fetch + refresh
# ---------------------------------------------------------------------------

def fetch_status(
    status_url: str,
    api_key: str,
    conv_id: str,
    repo: str | None,
    branch: str | None,
    timeout: float = FETCH_TIMEOUT_SEC,
) -> dict | None:
    """One GET against maestro's /api/agent-limits/status. The server
    derives initiative + engineer identity itself; the client only ships
    raw git facts. Returns the parsed verdict or None on any failure.

    Note: `session_id` is kept as the query-param name for maestro API
    compatibility with the Claude/Codex plugins — Cursor's conversation
    id occupies the same role."""
    params = {"session_id": conv_id}
    if repo:
        params["repo"] = repo
    if branch:
        params["branch"] = branch
    url = status_url + ("&" if "?" in status_url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"x-cardinalhq-api-key": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        data = json.loads(body)
        return data if isinstance(data, dict) else None
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, ValueError):
        return None


def maybe_refresh_verdict(
    conv_id: str,
    repo: str | None,
    branch: str | None,
    force: bool = False,
    timeout: float = FETCH_TIMEOUT_SEC,
) -> dict | None:
    """Refresh the conversation's verdict file if its server-assigned TTL
    has lapsed (or force=True). Returns the current verdict (fresh or
    cached), or None when limits aren't configured / everything failed."""
    cfg = limits_config()
    if not cfg:
        return None

    existing = read_verdict(conv_id)
    if existing and not force:
        fetched_at = existing.get("fetched_at")
        ttl = existing.get("ttl_seconds") or DEFAULT_TTL_SEC
        if isinstance(fetched_at, (int, float)) and time.time() - fetched_at < float(ttl):
            return existing

    api_key = ingest_api_key()
    if not api_key:
        return existing

    verdict = fetch_status(cfg["status_url"], api_key, conv_id, repo, branch, timeout=timeout)
    if verdict is None:
        return existing
    verdict["fetched_at"] = time.time()
    atomic_write_json(verdict_path(conv_id), verdict)
    return verdict


# ---------------------------------------------------------------------------
# Git facts (used by the sessionStart standing fetch)
# ---------------------------------------------------------------------------

def git_facts(cwd: str) -> tuple[str | None, str | None]:
    """(repo 'org/name', branch) for cwd — best-effort, 1s per command."""

    def _git(args: list[str]) -> str | None:
        try:
            out = subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0:
            return None
        return out.stdout.strip() or None

    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    remote = _git(["remote", "get-url", "origin"])
    repo = None
    if remote:
        m = _REMOTE_URL_RE.match(remote.strip())
        if m:
            name = re.sub(r"\.git$", "", m.group(3))
            if m.group(2) and name:
                repo = f"{m.group(2)}/{name}"
    return repo, branch


# ---------------------------------------------------------------------------
# Standing summary (sessionStart additional_context + cardinal-status)
# ---------------------------------------------------------------------------

def standing_lines(verdict: dict) -> list[str]:
    """Render the evaluations into short standing lines. This is data
    formatting only — all policy COPY (headlines, recommendations, block
    reasons) is server-authored and passed through verbatim."""
    evaluations = verdict.get("evaluations")
    if not isinstance(evaluations, list) or not evaluations:
        return []
    lines: list[str] = []
    for e in evaluations:
        if not isinstance(e, dict):
            continue
        try:
            scope = e.get("scope", "?")
            window = e.get("window")
            spent = float(e.get("spent_usd", 0))
            limit = float(e.get("limit_usd", 0))
            pct = int(round(float(e.get("fraction", 0)) * 100))
            set_by = e.get("set_by") or {}
            who = "you" if set_by.get("self") else set_by.get("display_name") or set_by.get("email") or "?"
            scope_label = f"{scope} ({window})" if scope == "engineer" and window else scope
            lines.append(
                f"- {scope_label}: ${spent:.2f} of ${limit:.2f} ({pct}%) — set by {who}"
            )
        except (TypeError, ValueError):
            continue
    return lines
