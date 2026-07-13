#!/usr/bin/env python3
"""Emit Cardinal agent-session telemetry from Cursor hooks.

Monorepo adapter over `cardinal_core` (vendored next to this file by
build/vendor.py). Core owns the algorithms — initiative resolution, bash
classification, OTLP record building/emission, limits primitives, session
counters, the convention prompt. This adapter owns everything Cursor-
specific:

  * camelCase payload spellings (conversationId / toolName / durationMs /
    modelId / modelParams / cursorVersion, …) with snake_case fallbacks;
  * `workspace_roots` → cwd resolution;
  * cursor.model / cursor.model_id / cursor.model_params / cursor.version
    resource stamping (parity spec Divergence L);
  * the Divergence-E limits gate: Cursor's beforeSubmitPrompt output
    schema is `{continue, user_message}` only — warn/notify context is
    STAGED in `<conv>.notify.json` and surfaced on the next postToolUse
    via `additional_context`, with opt-in CARDINAL_CURSOR_STRICT_WARN=1
    escalation of warn to block;
  * length-only turn_thought / turn_response emission (Divergence J);
  * the preCompact context-window plan_usage slice (Divergence K).

There is NO turn_usage / api_request emission on Cursor — the product
never exposes per-model-call token counts (parity spec gap D). Failures
are best-effort and silent: telemetry must not break the agent loop.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _plugin_version  # noqa: E402
from cardinal_core import limits, otlp, session  # noqa: E402
from cardinal_core.bashclass import classify_bash_command  # noqa: E402,F401
from cardinal_core.initiative import (  # noqa: E402,F401
    canonical_repo,
    detect_command,
    git,
    is_git_repo,
    resolve_initiative,
    strip_worktree_noise,
)
from cardinal_core.paths import AgentPaths  # noqa: E402

PLUGIN_VERSION = _plugin_version.plugin_version()
SCOPE_NAME = "cardinal-cursor-plugin"

PATHS = AgentPaths(home=Path.home() / ".cursor")

# Opt-in escalation: with this env var set, a warn-band verdict becomes a
# hard block instead of a deferred notify (Divergence E — the only way to
# surface a warn-band message ON the submit path is via block copy, so
# users who want that behaviour opt in explicitly).
STRICT_WARN_ENV = "CARDINAL_CURSOR_STRICT_WARN"

# Env-gated raw-payload dump for shape capture. Off by default; writes
# nothing unless CARDINAL_CURSOR_DEBUG_PAYLOADS=1. Retained for post-hoc
# payload verification and future schema evolution.
DEBUG_PAYLOADS_ENV = "CARDINAL_CURSOR_DEBUG_PAYLOADS"

# Cursor tool-call inputs — allowlisted file-path keys that surface as
# `target` on cardinal.turn_tool. Only path-shaped inputs cross this
# boundary (parity with Claude / Codex TARGET_KEYS).
TARGET_KEYS = {
    "read_file": "path",
    "edit_file": "path",
    "write_file": "path",
    "create_file": "path",
    "delete_file": "path",
    # Claude-shaped fallbacks in case Cursor accepts these tool names
    # via MCP or a custom mode.
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}

EXIT_CODE_RE = re.compile(r"(?:exit(?:ed)?|status)[ :]+(-?\d+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Common utilities
# ---------------------------------------------------------------------------

def silent_exit() -> None:
    sys.exit(0)


def conv_id_from_payload(payload: dict[str, Any]) -> str | None:
    """Cursor payloads use `conversation_id`; Claude/Codex call it
    `session_id`. Fall back to both for defensive parity."""
    for key in ("conversation_id", "conversationId", "session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def cwd_from_payload(payload: dict[str, Any]) -> str:
    """Cursor exposes `workspace_roots` (list). Claude/Codex expose
    `cwd`. Prefer the first workspace root, fall back to `cwd`, then
    process cwd — every hook needs SOME path for git resolution."""
    roots = payload.get("workspace_roots") or payload.get("workspaceRoots")
    if isinstance(roots, list) and roots:
        first = roots[0]
        if isinstance(first, str) and first:
            return first
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    return os.environ.get("CURSOR_PROJECT_DIR") or os.getcwd()


def resource_attrs(
    state: dict[str, Any], payload: dict[str, Any] | None = None
) -> dict[str, str]:
    """Base OTel resource attributes for every emitted record (core), plus
    per-event Cursor identity stamped from the hook payload's base fields
    when supplied (Divergence L). Cursor documents `model`, `model_id`,
    `model_params`, and `cursor_version` on every hook payload; surfacing
    them on the resource lets downstream slice by model / Cursor build
    without touching each event handler."""
    attrs: dict[str, str] = otlp.resource_attrs(
        service_name="cursor",
        agent_runtime="cursor",
        deployment_environment=state.get("deployment_environment"),
        user_email=state.get("user_email"),
        org=state.get("org_slug") or state.get("org_id"),
        plugin_version=PLUGIN_VERSION,
    )
    if isinstance(payload, dict):
        model = payload.get("model")
        if isinstance(model, str) and model:
            attrs["cursor.model"] = model
        model_id = payload.get("model_id") or payload.get("modelId")
        if isinstance(model_id, str) and model_id:
            attrs["cursor.model_id"] = model_id
        model_params = payload.get("model_params")
        if model_params is None:
            model_params = payload.get("modelParams")
        if isinstance(model_params, (dict, list)):
            try:
                attrs["cursor.model_params"] = json.dumps(model_params, separators=(",", ":"))
            except (TypeError, ValueError):
                pass
        elif isinstance(model_params, str) and model_params:
            attrs["cursor.model_params"] = model_params
        version = payload.get("cursor_version") or payload.get("cursorVersion")
        if isinstance(version, str) and version:
            attrs["cursor.version"] = version
    return attrs


def emit_records(
    records: list[dict[str, Any]], payload: dict[str, Any] | None = None
) -> None:
    if not records:
        return
    connection = otlp.connection_from_paths(PATHS)
    if connection is None:
        return
    otlp.emit_records(
        records,
        connection,
        resource_attrs(PATHS.read_state(), payload),
        scope_name=SCOPE_NAME,
        scope_version=PLUGIN_VERSION,
    )


def log_record(event_name: str, attrs: dict[str, Any], ts_ns: int) -> dict[str, Any]:
    return otlp.log_record(event_name, attrs, ts_ns)


def read_plan_stamp() -> dict[str, Any]:
    """{plan_type, rate_limit_tier} from the last-seen rate_limits block,
    or {} — an empty stamp is the norm on Cursor (no transcript token
    records exist to populate it; see parity spec gap D)."""
    return session.read_plan_stamp(PATHS)


def dump_debug_payload(event: str, payload: dict[str, Any]) -> None:
    """Env-gated raw hook-payload dump. No-op unless
    CARDINAL_CURSOR_DEBUG_PAYLOADS=1."""
    if os.environ.get(DEBUG_PAYLOADS_ENV) != "1":
        return
    try:
        PATHS.debug_dir.mkdir(parents=True, exist_ok=True)
        path = PATHS.debug_dir / f"{event}-{time.time_ns()}.json"
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    except (OSError, TypeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Cursor tool normalization
# ---------------------------------------------------------------------------

def _mcp_split(name: str) -> tuple[str, str] | None:
    """Cursor MCP tool names typically arrive as `mcp__<server>__<tool>`
    (matching the Claude/Codex convention). Return (server, tool) when
    the shape matches, else None."""
    if not name.startswith("mcp__"):
        return None
    parts = name.split("__")
    if len(parts) < 3:
        return None
    return parts[1], "__".join(parts[2:])


def normalize_tool_name(
    raw_name: str, tool_input: dict[str, Any]
) -> tuple[str, dict[str, Any], str | None]:
    """(display_name, extra_params, target_hint). Cursor's shell tool
    name is generally `run_terminal_cmd` / `run_shell_command`; MCP
    tools follow the mcp__ prefix."""
    if raw_name in {"run_terminal_cmd", "run_shell_command", "shell", "terminal"}:
        cmd = str(tool_input.get("command") or tool_input.get("cmd") or "")
        return "Bash", {"full_command": cmd, "bash_command": cmd.split(" ", 1)[0] if cmd else ""}, None
    mcp = _mcp_split(raw_name)
    if mcp is not None:
        return "mcp_tool", {"mcp_server_name": mcp[0], "mcp_tool_name": mcp[1]}, None
    return raw_name, {}, None


def output_success(tool_output: Any) -> str:
    """Cursor `postToolUse` includes the tool's output. When it looks
    like a shell result we scrape an exit code; otherwise assume
    success. Missing output → success (fail-open on ambiguity)."""
    if tool_output is None:
        return "true"
    if isinstance(tool_output, dict):
        for key in ("exit_code", "exitCode", "status", "returncode"):
            v = tool_output.get(key)
            if isinstance(v, (int, str)):
                try:
                    return "true" if int(v) == 0 else "false"
                except (TypeError, ValueError):
                    pass
        text = tool_output.get("stdout") or tool_output.get("output") or tool_output.get("text")
    else:
        text = tool_output
    if not isinstance(text, str):
        return "true"
    m = EXIT_CODE_RE.search(text)
    if not m:
        return "true"
    return "true" if m.group(1) == "0" else "false"


# ---------------------------------------------------------------------------
# Spend-limits gate (beforeSubmitPrompt) — the three-tier resolution from
# docs/specs/cursor-parity.md Divergence E. Core 0.2.0's
# limits.gate_decision() owns the policy walk (block age check, override
# downgrade, band hysteresis); this adapter only renders the decision
# into Cursor's channels: `{continue:false, user_message}` on the submit
# path for a block (or a strict-warn escalation), and core's
# staged-notify channel (`<conv>.notify.json`, limits.stage_notify /
# consume_notify) for warn/notify, surfaced on the next postToolUse.
# ---------------------------------------------------------------------------

def strict_warn_enabled() -> bool:
    return os.environ.get(STRICT_WARN_ENV) == "1"


def limits_gate_output(conv_id: str) -> dict[str, Any] | None:
    """Cursor renderer over core `limits.gate_decision()`. Returns the
    beforeSubmitPrompt output JSON (`{"continue": false, "user_message":
    ...}` for a block, `None` otherwise). Notify/warn context is STAGED
    via `limits.stage_notify()` for the next postToolUse to surface —
    the Cursor beforeSubmitPrompt output schema has no
    `additional_context` slot.

    Severity → channel mapping:
      block                       → {continue:false, user_message}
      warn + CARDINAL_CURSOR_STRICT_WARN=1 → escalate to block (server
                                    user_message copies through)
      warn (default) / notify     → stage `<conv>.notify.json`;
                                    postToolUse surfaces it once as
                                    additional_context.
    Warn/notify obey band hysteresis (only stage when the band RISES);
    a block — including a strict-warn escalation — is enforced every
    turn while in force. ack_band() is written only when a warn/notify
    is actually staged, matching core's "renderers ack what they
    surface" contract.
    """
    d = limits.gate_decision(PATHS, conv_id)
    if d is None:
        return None

    if d.tier == "block":
        return {"continue": False, "user_message": d.reason}

    if d.tier == "warn" and strict_warn_enabled():
        reason = d.user_message
        if not reason:
            # Preserve the pre-0.2.0 escalation copy exactly: fall back
            # to the verdict's block_reason (GateDecision carries reason
            # only for block tier), then the stock limit-reached line.
            verdict = limits.read_verdict(PATHS, conv_id) or {}
            fallback = (
                verdict.get("block_reason")
                or "A Cardinal spend limit for this work has been reached."
            )
            reason = f"[warn escalated to block via {STRICT_WARN_ENV}]\n{fallback}"
        return {"continue": False, "user_message": reason}

    if not d.is_new_band:
        return None

    parts: list[str] = []
    if d.agent_context:
        parts.append(d.agent_context)
    if d.tier == "warn" and d.user_message:
        parts.append(d.user_message)
    if parts:
        limits.stage_notify(PATHS, conv_id, "\n\n".join(parts), d.band)
        limits.ack_band(PATHS, conv_id, d.band)
    return None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def handle_before_submit_prompt(payload: dict[str, Any]) -> None:
    """Sync gate → git_state emit → async verdict refresh."""
    conv_id = conv_id_from_payload(payload)
    if not conv_id:
        return
    cwd = cwd_from_payload(payload)

    try:
        gate_out = limits_gate_output(conv_id)
        if gate_out:
            sys.stdout.write(json.dumps(gate_out))
            sys.stdout.flush()
            # A block is terminal — the turn never reaches the model,
            # so downstream git_state / verdict refresh have no session
            # context to attach to. Bail cleanly.
            return
    except Exception:
        pass

    branch = None
    repo = None
    remote_url = None
    head_sha = git(["rev-parse", "HEAD"], cwd)
    if head_sha:
        branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
        remote_url = git(["remote", "get-url", "origin"], cwd)
        repo = canonical_repo(remote_url)
        initiative_name, initiative_type = resolve_initiative(branch)
        attrs: dict[str, Any] = {
            "session_id": conv_id,
            "cardinal_cwd": cwd,
            "cardinal_head_sha": head_sha,
            "cardinal_branch": branch,
            "cardinal_repo": repo,
            "cardinal_remote_url": remote_url,
            "cardinal_initiative_name": initiative_name,
            "cardinal_initiative_type": initiative_type,
            "cardinal_command": detect_command(payload.get("prompt") or payload.get("message")),
            **read_plan_stamp(),
        }
        emit_records([log_record("cardinal.git_state", attrs, time.time_ns())], payload)

    try:
        limits.maybe_refresh_verdict(PATHS, session_id=conv_id, repo=repo, branch=branch)
    except Exception:
        pass


def _tick_turn(conv_id: str, generation_id: Any, state: dict[str, Any]) -> None:
    """Cursor doesn't expose a `user_message` transcript boundary, so we
    advance turn counters when we first see a new generation_id. This
    runs on postToolUse (the first tool of a turn), giving us
    (user_turn_seq, turn_seq, tool_seq) that totally-orders the tool
    stream across hook firings."""
    gen = str(generation_id) if generation_id is not None else None
    if gen and gen != state.get("last_prompt_generation"):
        session.begin_user_turn(state)
        state["last_prompt_generation"] = gen


def handle_post_tool_use(payload: dict[str, Any]) -> None:
    """Emit cardinal.turn_tool + tool_result from one payload; piggyback
    any staged notify message as `additional_context` output (once per
    band per turn)."""
    dump_debug_payload("postToolUse", payload)
    conv_id = conv_id_from_payload(payload)
    if not conv_id:
        return
    state = session.load_progress(PATHS, conv_id)
    _tick_turn(conv_id, payload.get("generation_id"), state)

    raw_name = str(payload.get("tool_name") or payload.get("toolName") or "")
    tool_input_raw = payload.get("tool_input") or payload.get("toolInput") or {}
    tool_input = tool_input_raw if isinstance(tool_input_raw, dict) else {}
    tool_output = payload.get("tool_output") or payload.get("toolOutput")

    display_name, extra, _ = normalize_tool_name(raw_name, tool_input)

    target = None
    key = TARGET_KEYS.get(display_name) or TARGET_KEYS.get(raw_name)
    if key:
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            target = v

    plan_stamp = read_plan_stamp()
    now_ns = time.time_ns()
    records: list[dict[str, Any]] = []
    turn_tool_attrs: dict[str, Any] = {
        "session_id": conv_id,
        "ts": now_ns,
        "user_turn_seq": state["user_turn_seq"],
        "turn_seq": state["turn_seq"],
        "tool_seq": state["tool_seq"],
        "tool_name": display_name,
        "target": target,
        **plan_stamp,
    }
    if display_name == "mcp_tool":
        # Preserve the raw mcp__server__tool name as the harvester's
        # strongest clustering signal (parity with Codex).
        turn_tool_attrs["tool_name"] = raw_name
        turn_tool_attrs["mcp_server_name"] = extra.get("mcp_server_name")
        turn_tool_attrs["mcp_tool_name"] = extra.get("mcp_tool_name")
    elif display_name == "Bash":
        classified = classify_bash_command(str(extra.get("full_command") or ""))
        if classified is not None:
            bash_class, bash_multi = classified
            turn_tool_attrs["bash_class"] = bash_class
            if bash_multi:
                turn_tool_attrs["bash_multi"] = True
    records.append(log_record("cardinal.turn_tool", turn_tool_attrs, now_ns))

    tool_result_attrs: dict[str, Any] = {
        "session_id": conv_id,
        "agent_runtime": "cursor",
        "tool_name": display_name,
        "success": output_success(tool_output),
        "tool_input": json.dumps(tool_input, separators=(",", ":")) if tool_input else None,
    }
    records.append(log_record("tool_result", tool_result_attrs, now_ns + 1))

    emit_records(records, payload)
    state["tool_seq"] += 1
    session.save_progress(PATHS, conv_id, state)

    # Piggyback pending notify/warn context onto the hook output. This
    # is the Cursor adapter's substitute for Claude's inline
    # systemMessage on the submit hook — see Divergence E.
    try:
        msg = limits.consume_notify(PATHS, conv_id)
        if msg:
            sys.stdout.write(json.dumps({"additional_context": msg}))
            sys.stdout.flush()
    except Exception:
        pass


def handle_pre_compact(payload: dict[str, Any]) -> None:
    """Emit `cardinal.plan_usage` (context slice) from Cursor's
    documented preCompact payload: `trigger`, `context_usage_percent`,
    `context_tokens`, `context_window_size`, `message_count`,
    `messages_to_compact`, `is_first_compaction`. This is not the same
    'plan_usage' as the Claude/Codex per-model-call token slice — it's
    a context-window slice on the same event name; downstream
    disambiguates on the presence of `plan.compact_trigger`."""
    dump_debug_payload("preCompact", payload)
    conv_id = conv_id_from_payload(payload)
    if not conv_id:
        return
    attrs: dict[str, Any] = {
        "session_id": conv_id,
        "plan.context_tokens": payload.get("context_tokens"),
        "plan.context_window": payload.get("context_window_size"),
        "plan.context_pct": payload.get("context_usage_percent"),
        "plan.compact_trigger": payload.get("trigger"),
        "plan.messages_to_compact": payload.get("messages_to_compact"),
        "plan.is_first_compaction": payload.get("is_first_compaction"),
        **read_plan_stamp(),
    }
    emit_records([log_record("cardinal.plan_usage", attrs, time.time_ns())], payload)


def handle_stop(payload: dict[str, Any]) -> None:
    """Best-effort transcript sweep. Cursor's transcript format carries
    no token / rate-limit records (parity spec gap D — Cursor product
    gap), so this stays a debug-capture no-op until Cursor exposes
    usage on a hook payload or the transcript."""
    dump_debug_payload("stop", payload)


def handle_after_agent_response(payload: dict[str, Any]) -> None:
    """Emit `cardinal.turn_response` with the response text length.
    We intentionally do NOT emit `text` itself — it can be large and
    may contain sensitive user code/content. Debug-capture is retained
    as a side channel under CARDINAL_CURSOR_DEBUG_PAYLOADS=1 for
    post-hoc verification."""
    dump_debug_payload("afterAgentResponse", payload)
    conv_id = conv_id_from_payload(payload)
    if not conv_id:
        return
    text = payload.get("text")
    text_len = len(text) if isinstance(text, str) else None
    attrs: dict[str, Any] = {
        "session_id": conv_id,
        "response.text_len": text_len,
        **read_plan_stamp(),
    }
    emit_records([log_record("cardinal.turn_response", attrs, time.time_ns())], payload)


def handle_after_agent_thought(payload: dict[str, Any]) -> None:
    """Emit `cardinal.turn_thought` with the thought duration and text
    length. We intentionally do NOT emit `text` — it is the model's
    thinking and can be large and potentially sensitive. Debug-capture
    remains under CARDINAL_CURSOR_DEBUG_PAYLOADS=1."""
    dump_debug_payload("afterAgentThought", payload)
    conv_id = conv_id_from_payload(payload)
    if not conv_id:
        return
    text = payload.get("text")
    text_len = len(text) if isinstance(text, str) else None
    attrs: dict[str, Any] = {
        "session_id": conv_id,
        "thought.duration_ms": payload.get("duration_ms") or payload.get("durationMs"),
        "thought.text_len": text_len,
        **read_plan_stamp(),
    }
    emit_records([log_record("cardinal.turn_thought", attrs, time.time_ns())], payload)


def subagent_description_from_payload(payload: dict[str, Any]) -> str | None:
    """Documented Cursor subagentStop payload carries `task`,
    `description`, and `summary`. Prefer `description` (the free-text
    request), fall back to `task`, then `summary`. Cap at 160 chars for
    the same reason the Claude plugin does — the harvester doesn't need
    more."""
    for key in ("description", "task", "summary"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:160]
    return None


def handle_subagent_stop(payload: dict[str, Any]) -> None:
    """Emit cardinal.subagent_usage from Cursor's documented subagentStop
    payload. No total_tokens field is documented on Cursor (gap D), so
    only duration / message / tool-call counts are emitted."""
    dump_debug_payload("subagentStop", payload)
    conv_id = conv_id_from_payload(payload)
    if not conv_id:
        return
    attrs = {
        "session_id": conv_id,
        "agent_runtime": "cursor",
        "subagent_type": payload.get("subagent_type") or payload.get("subagentType"),
        "subagent_description": subagent_description_from_payload(payload),
        "subagent_status": payload.get("status"),
        "duration_ms": payload.get("duration_ms") or payload.get("durationMs"),
        "message_count": payload.get("message_count") or payload.get("messageCount"),
        "tool_call_count": payload.get("tool_call_count") or payload.get("toolCallCount"),
        "loop_count": payload.get("loop_count") or payload.get("loopCount"),
        **read_plan_stamp(),
    }
    emit_records([log_record("cardinal.subagent_usage", attrs, time.time_ns())], payload)


# ---------------------------------------------------------------------------
# sessionStart: initiative-convention prompt + one-shot budget standing
# ---------------------------------------------------------------------------

CONVENTION_PROMPT = session.convention_prompt("Cursor")


def handle_session_start(payload: dict[str, Any]) -> None:
    cwd = cwd_from_payload(payload)
    if not is_git_repo(cwd):
        return
    context = CONVENTION_PROMPT
    try:
        standing = session.budget_standing(PATHS, conv_id_from_payload(payload), cwd)
        if standing:
            context = f"{CONVENTION_PROMPT}\n\n{standing}"
    except Exception:
        pass
    sys.stdout.write(json.dumps({"additional_context": context}))
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# Cursor invokes each hook process with the event name available via
# the hook_event_name field in the stdin payload. The --event CLI flag
# is kept for parity with the Codex plugin's hooks.json shape and for
# ease of local testing.
HANDLERS = {
    "sessionStart": handle_session_start,
    "beforeSubmitPrompt": handle_before_submit_prompt,
    "postToolUse": handle_post_tool_use,
    "preCompact": handle_pre_compact,
    "stop": handle_stop,
    "subagentStop": handle_subagent_stop,
    "afterAgentResponse": handle_after_agent_response,
    "afterAgentThought": handle_after_agent_thought,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=False, default=None)
    args = parser.parse_args()

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    event = args.event or payload.get("hook_event_name") or payload.get("hookEventName")
    handler = HANDLERS.get(event) if isinstance(event, str) else None
    if handler is None:
        silent_exit()

    try:
        handler(payload)
    except Exception:
        pass
    silent_exit()


if __name__ == "__main__":
    main()
