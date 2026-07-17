"""Spend-limits delivery: verdict fetch/refresh, sync gate, standing lines.

The limits feature splits across hook events so the turn-critical path
never touches the network:

  prompt-time gate   — sync, file I/O only → hook JSON verdict.
  post-telemetry     — TTL-driven verdict refresh (network, short timeout).
  session start      — one synchronous forced fetch (short timeout, fail
                       open) so budget standing is in context from turn one.

File layout under <agent-home>/cardinal/limits/ — single-writer ownership:

  <session>.verdict.json   written by the refresh; server response plus a
                           fetched_at stamp.
  <session>.ack.json       written by the gate; last band surfaced
                           (hysteresis state).
  <session>.override.json  presence downgrades a block to warn-tier.

Everything is best-effort: any failure returns None / does nothing. A
missing verdict means "allow" — fail open is the contract. (The omnigent
adapter gets fail-closed semantics from omnigent's own policy engine, not
from this module.)
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal

from .paths import AgentPaths, atomic_write_json_compact, read_json

FETCH_TIMEOUT_SEC = 2.0
# Default refresh cadence when the server response carried no ttl_seconds.
DEFAULT_TTL_SEC = 120
# Gate-side staleness: a warn/notify verdict older than this is ignored
# (fail open). Block verdicts stay honored longer — spend only grows, and
# the refresh runs every turn anyway.
WARN_MAX_AGE_SEC = 10 * 60
BLOCK_MAX_AGE_SEC = 60 * 60


def limits_config(paths: AgentPaths) -> dict | None:
    """The limits block cardinal-connect persisted from the device-flow
    bundle. None = server doesn't speak the protocol / not connected —
    every limits path is a no-op then (zero overhead for older backends)."""
    limits = paths.read_state().get("limits")
    if not isinstance(limits, dict):
        return None
    url = limits.get("status_url")
    if not url or not limits.get("enabled", True):
        return None
    return {"status_url": url}


def ingest_api_key(paths: AgentPaths) -> str | None:
    """The plugin's ingest key — the same credential the status endpoint
    authenticates (and derives engineer identity from, server-side)."""
    key = paths.read_secrets().get("ingest_api_key")
    return key if isinstance(key, str) and key else None


def read_verdict(paths: AgentPaths, session_id: str) -> dict | None:
    v = read_json(paths.verdict_path(session_id))
    return v or None


def fetch_status(
    status_url: str,
    api_key: str,
    session_id: str,
    repo: str | None,
    branch: str | None,
    timeout: float = FETCH_TIMEOUT_SEC,
) -> dict | None:
    """One GET against maestro's /api/agent-limits/status. The server
    derives initiative + engineer identity itself; the client only ships
    raw git facts. Returns the parsed verdict or None on any failure."""
    params = {"session_id": session_id}
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
    paths: AgentPaths,
    session_id: str,
    repo: str | None,
    branch: str | None,
    force: bool = False,
    timeout: float = FETCH_TIMEOUT_SEC,
    api_key: str | None = None,
) -> dict | None:
    """Refresh the session's verdict file if its server-assigned TTL has
    lapsed (or force=True). Returns the current verdict (fresh or cached),
    or None when limits aren't configured / everything failed.

    `api_key` overrides the default secrets-file sourcing — the Claude
    adapter reads its credential from Claude Code's OTel settings, and a
    server-side consumer supplies its own (core 0.2.0 gap #2)."""
    cfg = limits_config(paths)
    if not cfg:
        return None

    existing = read_verdict(paths, session_id)
    if existing and not force:
        fetched_at = existing.get("fetched_at")
        ttl = existing.get("ttl_seconds") or DEFAULT_TTL_SEC
        if isinstance(fetched_at, (int, float)) and time.time() - fetched_at < float(ttl):
            return existing

    if api_key is None:
        api_key = ingest_api_key(paths)
    if not api_key:
        return existing

    verdict = fetch_status(cfg["status_url"], api_key, session_id, repo, branch, timeout=timeout)
    if verdict is None:
        return existing
    verdict["fetched_at"] = time.time()
    atomic_write_json_compact(paths.verdict_path(session_id), verdict)
    return verdict


@dataclass(frozen=True)
class GateDecision:
    """The gate's POLICY outcome, channel-agnostic (core 0.2.0 gap #1).

    Adapters render this into their hook's output schema:
    hookSpecificOutput JSON (claude/codex/gemini — see gate_output),
    `{continue: false, user_message}` + notify staging (cursor), or a
    PolicyResult (omnigent). Renderers that surface a warn/notify band
    must call ack_band() afterward — the decision itself never writes
    hysteresis state.
    """

    tier: Literal["block", "warn", "notify"]  # after override downgrade
    band: int
    # The tier's "why" copy for renderers whose channel has only a reason
    # slot (omnigent PolicyResponse). block: server block_reason /
    # fallback copy. warn/notify: block_reason when an override
    # downgraded a live block (the why must survive the downgrade), else
    # user_message / agent_context. gate_output() reads it on block only,
    # so CLI hook bytes are unaffected.
    reason: str | None
    agent_context: str | None
    user_message: str | None
    is_new_band: bool           # hysteresis: band rose vs last ack


def gate_decision(paths: AgentPaths, session_id: str) -> GateDecision | None:
    """The sync half of the spend-limits gate. File I/O only — never
    touches the network. Returns None to fail open (no verdict, band 0,
    or verdict stale).

    Severity semantics (the server decides severity; we route it):
      block  → enforced every turn while in force; an override file
               downgrades it to warn-tier surfacing.
      warn   → agent_context + user_message, band hysteresis.
      notify → agent_context only (model economizes), band hysteresis.
    """
    verdict = read_verdict(paths, session_id)
    if not verdict:
        return None

    decision = verdict.get("decision")
    try:
        band = int(verdict.get("band") or 0)
    except (TypeError, ValueError):
        band = 0
    fetched_at = verdict.get("fetched_at")
    age = (
        time.time() - fetched_at
        if isinstance(fetched_at, (int, float))
        else float("inf")
    )

    agent_context = verdict.get("agent_context")
    agent_context = agent_context if isinstance(agent_context, str) and agent_context else None
    user_message = verdict.get("user_message")
    user_message = user_message if isinstance(user_message, str) and user_message else None

    block_reason = verdict.get("block_reason")
    block_reason = block_reason if isinstance(block_reason, str) and block_reason else None

    downgraded = False
    if decision == "block" and age <= BLOCK_MAX_AGE_SEC:
        if not paths.override_path(session_id).exists():
            reason = (
                block_reason
                or user_message
                or "A Cardinal spend limit for this work has been reached."
            )
            return GateDecision(
                tier="block", band=band, reason=reason,
                agent_context=agent_context, user_message=user_message,
                is_new_band=True,
            )
        decision = "warn"  # overridden: keep the human-visible standing
        downgraded = True

    if band <= 0 or age > WARN_MAX_AGE_SEC:
        return None

    ack = read_json(paths.ack_path(session_id))
    try:
        last_band = int(ack.get("band") or 0)
    except (TypeError, ValueError):
        last_band = 0

    return GateDecision(
        tier="warn" if decision == "warn" else "notify",
        band=band,
        # An override-downgraded block keeps its block_reason; ordinary
        # warn/notify carry the standing copy.
        reason=(block_reason if downgraded else None)
        or user_message
        or agent_context,
        agent_context=agent_context,
        user_message=user_message,
        is_new_band=band > last_band,
    )


def ack_band(paths: AgentPaths, session_id: str, band: int) -> None:
    """Record that `band` was surfaced to the user/agent — the hysteresis
    write. Renderers call this after actually delivering a warn/notify."""
    atomic_write_json_compact(
        paths.ack_path(session_id), {"band": band, "surfaced_at": time.time()}
    )


def gate_output(
    paths: AgentPaths,
    session_id: str,
    *,
    hook_event_name: str,
) -> dict[str, Any] | None:
    """hookSpecificOutput renderer over gate_decision() — the channel
    shape shared by claude/codex/gemini prompt-time hooks. Returns the
    hook-output JSON to print, or None (fail open).

    `hook_event_name` is the agent's prompt-time event (UserPromptSubmit
    for Claude/Codex, BeforeAgent for Gemini); Cursor's divergent output
    schema renders GateDecision itself and does not call this function.
    """
    d = gate_decision(paths, session_id)
    if d is None:
        return None
    if d.tier == "block":
        return {"decision": "block", "reason": d.reason}
    if not d.is_new_band:
        return None

    out: dict[str, Any] = {}
    if d.agent_context:
        out["hookSpecificOutput"] = {
            "hookEventName": hook_event_name,
            "additionalContext": d.agent_context,
        }
    if d.tier == "warn" and d.user_message:
        out["systemMessage"] = d.user_message
    if not out:
        return None
    ack_band(paths, session_id, d.band)
    return out


# ---------------------------------------------------------------------------
# Staged-notify channel (core 0.2.0 gap #5 — Cursor Divergence E, and any
# adapter whose prompt-time hook lacks a non-blocking message slot)
# ---------------------------------------------------------------------------

def stage_notify(paths: AgentPaths, session_id: str, message: str, band: int) -> None:
    """Stage a standing message for delivery on the next available
    channel (e.g. Cursor's postToolUse additional_context)."""
    atomic_write_json_compact(
        paths.notify_path(session_id),
        {"message": message, "band": band, "staged_at": time.time()},
    )


def consume_notify(paths: AgentPaths, session_id: str) -> str | None:
    """One-shot read-and-delete of a staged notify message."""
    path = paths.notify_path(session_id)
    blob = read_json(path)
    message = blob.get("message")
    if not isinstance(message, str) or not message:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    return message


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
