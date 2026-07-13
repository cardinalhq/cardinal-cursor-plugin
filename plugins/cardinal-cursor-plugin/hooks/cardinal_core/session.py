"""Session-scoped shared behavior: the initiative-convention prompt, the
budget-standing context block, per-session sequence counters, and the
plan stamp.

Sequence-counter semantics (Claude parity, identical in every adapter):
  user_turn_seq — session-monotonic ordinal, advances on each user turn.
  turn_seq      — model-call index WITHIN the current user turn.
  tool_seq      — tool-call index within the current model call.
(user_turn_seq, turn_seq, tool_seq) totally orders a session's tool
stream across hook firings.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from . import limits
from .initiative import git_facts
from .paths import AgentPaths, atomic_write_json, read_json

# plan_usage cadence: the first snapshot of a session is unthrottled; later
# ones emit at most every 10 minutes so heavy users produce ~7 usage
# events/day, not one per model call.
PLAN_USAGE_TTL_SEC = 10 * 60


def convention_prompt(agent_label: str) -> str:
    """The initiative-convention SessionStart context, parameterized by the
    agent's display name ("Codex session", "Gemini CLI session", …).
    Worded to steer branch creation, not to demand renames of existing
    branches."""
    return (
        f"You are running inside a Cardinal-instrumented {agent_label} session. "
        "Cardinal attributes agent spend to 'initiatives' — "
        "one branch = one initiative. When you create a new branch for "
        "work in this session, follow the convention:\n\n"
        "  <type-prefix>/<kebab-name>\n\n"
        "  type-prefix  ∈ {feat, fix, refactor, infra, chore, research, spike}\n"
        "  kebab-name   = lowercase, 1–4 dash-separated segments\n\n"
        "Examples:\n"
        "  feat/outcomes-observability    → name 'outcomes-observability', type 'feature'\n"
        "  fix/login-crash                → name 'login-crash',            type 'bugfix'\n"
        "  refactor/auth-token-rotation   → name 'auth-token-rotation',    type 'refactor'\n"
        "  research/data-pipeline-spike   → name 'data-pipeline-spike',    type 'research'\n\n"
        "Prefix aliases: 'feature' = 'feat', 'bugfix' = 'fix', 'chore' = "
        "'infra', 'spike' = 'research'. Other conventional prefixes are "
        "also recognized: 'perf' → feature; 'cleanup' → refactor; 'test', "
        "'tests', 'ci', 'build', 'deps', 'docs', 'doc' → infra. Sessions "
        "on main/master/develop/trunk are treated as research/scoping work — "
        "when intent crystallises into a deliverable, cut a typed branch "
        "using this convention. Off-convention branches get a stable name "
        "but default to type 'feature', so the convention is the way to "
        "ensure correct classification."
    )


def budget_standing(
    paths: AgentPaths,
    session_id: str | None,
    cwd: str,
    api_key: str | None = None,
) -> str | None:
    """One synchronous limits fetch at session start (short timeout, fail
    open) so the budget is part of the session's standing context from
    turn one. Also warm-writes the verdict file the per-turn sync gate
    reads. No-op when the backend doesn't advertise the limits protocol.
    `api_key` passes through to maybe_refresh_verdict for adapters whose
    credential lives outside the secrets file (core 0.2.0 gap #2)."""
    if not session_id:
        return None
    if not limits.limits_config(paths):
        return None
    repo, branch = git_facts(cwd)
    verdict = limits.maybe_refresh_verdict(
        paths, session_id=session_id, repo=repo, branch=branch, force=True,
        timeout=1.5, api_key=api_key,
    )
    if not verdict:
        return None
    lines = limits.standing_lines(verdict)
    if not lines:
        return None
    parts = ["Cardinal spend budgets apply to this session:"]
    parts.extend(lines)
    # Server-authored copy rides through verbatim — when a threshold is
    # already crossed at session start, lead with the server's message.
    user_message = verdict.get("user_message")
    if isinstance(user_message, str) and user_message:
        parts.append(user_message)
    parts.append(
        "Work economically as budgets tighten; budget standing updates "
        "arrive automatically as the session proceeds."
    )
    return "\n".join(parts)


def read_plan_stamp(paths: AgentPaths) -> dict[str, Any]:
    """{plan_type, rate_limit_tier} from the last-seen plan facts, or {} —
    callers merge it into event attrs (missing keys are skipped by
    log_record's None/empty filter)."""
    blob = read_json(paths.plan_stamp_path)
    out: dict[str, Any] = {}
    for key in ("plan_type", "rate_limit_tier"):
        v = blob.get(key)
        if isinstance(v, str) and v:
            out[key] = v
    return out


def write_plan_stamp(paths: AgentPaths, stamp: dict[str, Any]) -> None:
    atomic_write_json(paths.plan_stamp_path, {
        **stamp,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def load_progress(paths: AgentPaths, session_id: str) -> dict[str, Any]:
    """Per-session mutable progress: sequence counters, plan-emission
    throttle state, and any adapter extras (e.g. codex's last_line cursor —
    adapters read/write extra keys through the same dict)."""
    p = read_json(paths.progress_path(session_id))
    return {
        **p,
        "user_turn_seq": int(p.get("user_turn_seq") or 0),
        "turn_seq": int(p.get("turn_seq") or 0),
        "tool_seq": int(p.get("tool_seq") or 0),
        "plan_stamp": p.get("plan_stamp") if isinstance(p.get("plan_stamp"), dict) else read_plan_stamp(paths),
    }


def save_progress(paths: AgentPaths, session_id: str, state: dict[str, Any]) -> None:
    atomic_write_json(paths.progress_path(session_id), {
        **state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def begin_user_turn(state: dict[str, Any]) -> None:
    """Turn boundary: the session-monotonic ordinal advances; per-turn
    counters restart."""
    state["user_turn_seq"] = int(state.get("user_turn_seq") or 0) + 1
    state["turn_seq"] = 0
    state["tool_seq"] = 0


def end_model_call(state: dict[str, Any]) -> None:
    state["turn_seq"] = int(state.get("turn_seq") or 0) + 1
    state["tool_seq"] = 0


def plan_usage_throttled(state: dict[str, Any], now_s: float | None = None) -> bool:
    """True when a plan_usage snapshot should be SUPPRESSED. First snapshot
    of the session is unthrottled (anchors the Δ math)."""
    last_emit = state.get("plan_usage_emitted_at")
    if not isinstance(last_emit, (int, float)):
        return False
    return (now_s if now_s is not None else time.time()) - last_emit < PLAN_USAGE_TTL_SEC
