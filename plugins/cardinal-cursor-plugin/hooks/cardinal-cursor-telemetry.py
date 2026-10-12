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
  * the preCompact context-window plan_usage slice (Divergence K);
  * local evidence capture on postToolUse and postToolUseFailure: every
    tool call (built-in or MCP, except Cardinal's own gateway) goes through
    the shared generic pipeline (cardinal_core.evidence_capture) into the
    spool ~/.cardinal/evidence, and its `[evidence:ev_…]` id is handed back
    via `additional_context`, so a storyboard can cite it as *captured*
    evidence (scripts/cardinal-evidence promotes it). Nothing is sent over
    the network;
  * the Investigation session (cardinal_core.investigation_agent):
    sessionStart bootstraps it, starts the detached poller and puts the
    session guidance in `additional_context`; postToolUse /
    postToolUseFailure deliver staged items, the inbox and re-surfaced
    challenges as `additional_context`; stop ends the turn (owner-input
    flush, sync check) and STAGES what it would deliver: Cursor's
    `followup_message` is a user turn, so it is never used (R1 of
    docs/plans/cross-agent-parity.md);
  * owner input on beforeSubmitPrompt (cardinal_core.owner_input.capture,
    detached), only after the user consented in their own terminal
    (`cardinal-connect --owner-input on`): Cursor has no user-visible,
    non-blocking channel for a per-session disclosure. Never blocks;
  * storyboard authoring (cardinal_core.storyboard_authoring): the preview
    render and hero upload in a matcher-scoped postToolUse entry with a
    long timeout (`--role authoring`), the evidence token on every tool, the
    edit lookup on afterFileEdit and the discovery re-look on
    beforeSubmitPrompt (both staged for the next postToolUse, the hooks that
    compute them have no context channel).

There is NO turn_usage / api_request emission on Cursor — the product
never exposes per-model-call token counts (parity spec gap D). Failures
are best-effort and silent: telemetry must not break the agent loop.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

START = time.monotonic()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _decision_cli  # noqa: E402
import _plugin_version  # noqa: E402
from cardinal_core import decisions, evidence, limits, otlp, session  # noqa: E402
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

# Upper bound on the `gh pr view` call made on the synchronous
# beforeSubmitPrompt path (cache miss only; hits are file reads).
PR_RESOLVE_TIMEOUT_SEC = 1.5

# The decision-capture CLI the sessionStart context tells the agent to run.
DECISION_CLI = Path(__file__).resolve().parent.parent / "scripts" / "cardinal-decision"
# The evidence CLI a capture's context line points the agent at.
EVIDENCE_CLI = Path(__file__).resolve().parent.parent / "scripts" / "cardinal-evidence"

# postToolUse does only local work; its network sends run in a detached
# `--background <spool>` child. Test-only: run that job inline instead.
BACKGROUND_INLINE_ENV = "CARDINAL_CURSOR_BACKGROUND_INLINE"
BACKGROUND_EMIT_TIMEOUT_SEC = 3.0

EXIT_CODE_RE = re.compile(r"(?:exit(?:ed)?|status)[ :]+(-?\d+)", re.IGNORECASE)

# Cardinal's own MCP server as cardinal-connect registers it
# (mcpServers.cardinal). Its gateway mints a *witnessed* receipt for every
# call, so evidence capture skips it (the only tool it skips).
CARDINAL_MCP_SERVERS = ("cardinal",)


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
    records: list[dict[str, Any]],
    payload: dict[str, Any] | None = None,
    timeout: float = otlp.DEFAULT_TIMEOUT_SEC,
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
        timeout=timeout,
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
# Evidence capture (postToolUse / postToolUseFailure) — the Cursor side of the
# generic evidence pipeline (cardinal_core.evidence_capture.capture_call).
# EVERY tool call is captured (built-in tools, any MCP server, tools Cursor
# adds later); the only skip is Cardinal's own gateway, whose calls already
# carry witnessed receipts. Cursor names MCP tools `mcp__<server>__<tool>`.
# ---------------------------------------------------------------------------

def _decode_json_container(value: Any) -> Any:
    """Cursor documents postToolUse `tool_output` (and, on some builds,
    `tool_input`) as a JSON-encoded string. Decode it when it holds a JSON
    object or array, e.g. an MCP `{content, structuredContent}` result, so
    the spool keeps its structure; any other value is returned as is (a
    plain string is the result text)."""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in ("{", "["):
            try:
                decoded = json.loads(text)
            except ValueError:
                return value
            if isinstance(decoded, (dict, list)):
                return decoded
    return value


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = payload.get(k)
        if v is not None:
            return v
    return None


def failure_text(payload: dict[str, Any]) -> str | None:
    """The error of a postToolUseFailure payload. Cursor documents the
    event but not (yet, as observed) its exact fields: the first non-empty
    string among the likely spellings, else the tool output."""
    for k in ("error", "error_message", "errorMessage", "failure", "message"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            m = v.get("message")
            if isinstance(m, str) and m.strip():
                return m
    out = _first(payload, "tool_output", "toolOutput")
    if isinstance(out, str) and out.strip():
        return out
    return None


def evidence_call(payload: dict[str, Any], failed: bool = False):
    """The cardinal_core.evidence_capture.ToolCall of one postToolUse /
    postToolUseFailure payload, or None (no tool name; a failure without
    an error). Never raises."""
    try:
        from cardinal_core import evidence_capture as cap

        raw_name = _first(payload, "tool_name", "toolName")
        if not isinstance(raw_name, str) or not raw_name:
            return None
        source, tool = cap.classify_mcp_name(raw_name, "cursor", CARDINAL_MCP_SERVERS)
        tool_input = _decode_json_container(_first(payload, "tool_input", "toolInput"))
        error = failure_text(payload) if failed else None
        if failed and error is None:
            return None
        response = None if failed else _decode_json_container(_first(payload, "tool_output", "toolOutput"))
        tuid = _first(payload, "tool_use_id", "toolUseId", "tool_call_id", "toolCallId")
        return cap.ToolCall(
            runtime="cursor",
            tool_name=raw_name,
            source=source,
            tool=tool,
            tool_input=tool_input,
            response=response,
            error=error,
            session_id=conv_id_from_payload(payload),
            tool_use_id=tuid if isinstance(tuid, str) else None,
            cwd=cwd_from_payload(payload),
            client=evidence.client_string("cursor", _first(payload, "cursor_version", "cursorVersion")),
        )
    except Exception:
        return None


def capture_evidence(payload: dict[str, Any], failed: bool = False, call: Any = None) -> str | None:
    """Record one tool call (any tool) in the local evidence spool and
    return the context line for the agent (`[evidence:ev_...]`, or
    `[evidence:ev_... withheld: ...]` for a call that touched something
    sensitive); None for Cardinal's own tools, when the user opted out
    (CARDINAL_EVIDENCE_CAPTURE=0 or ~/.cardinal/evidence/disabled), with
    CARDINAL_EVIDENCE_CONTEXT=0, or on any failure. `call`: the payload's
    evidence_call when the caller already built it. Local file work only;
    never raises."""
    try:
        from cardinal_core import evidence_capture as cap

        if call is None:
            call = evidence_call(payload, failed)
        if call is None:
            return None
        # Never silent: a call the pipeline cannot finish in time is kept as a
        # withheld stub (capture_call_guarded).
        got = cap.capture_call_guarded(call, Path.home(), promote_cmd=shlex.quote(str(EVIDENCE_CLI)))
        return got.line if got is not None else None
    except BaseException:
        return None


def failure_line(call: Any, *, succeeded: bool | None = None) -> str | None:
    """investigation_failures.observe for one tool call: a throttled line
    when a shell call that writes to the Investigation could not reach
    Cardinal (the sandbox's network denial). Core keys the shell tools by
    runtime (investigation_failures.SHELL_TOOLS). Local only; never raises."""
    try:
        if call is None:
            return None
        from cardinal_core import investigation_failures

        return investigation_failures.observe(Path.home(), call.session_id, call.tool_input, call.response,
                                              call.error, runtime="cursor", tool_name=call.tool_name,
                                              succeeded=succeeded)
    except Exception:
        return None


def handle_post_tool_use_failure(payload: dict[str, Any]) -> None:
    """A failed tool call is evidence too (a failing test, a 403): captured
    with status "error", its id returned as additional_context, with the
    Investigation's deliveries for this tool boundary (boundary_contexts)."""
    dump_debug_payload("postToolUseFailure", payload)
    call = evidence_call(payload, failed=True)
    lines = [x for x in (failure_line(call), capture_evidence(payload, failed=True, call=call)) if x]
    conv_id = conv_id_from_payload(payload)
    if conv_id:
        lines.extend(boundary_contexts(payload, conv_id))
    if lines:
        sys.stdout.write(json.dumps({"additional_context": "\n\n".join(lines)}))
        sys.stdout.flush()


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

def resolve_pr(
    cwd: str, repo: str | None, branch: str | None
) -> tuple[int | None, str | None]:
    """(number, url) of the branch's PR via core `decisions.resolve_pr`
    (`gh pr view`, cached per repo+branch), or (None, None). Cursor's
    beforeSubmitPrompt is synchronous, so the `gh` call is bounded by
    PR_RESOLVE_TIMEOUT_SEC; any failure leaves the keys absent and never
    fails the git_state record."""
    try:
        return decisions.resolve_pr(
            cwd, repo, branch, decisions.cache_dir(PATHS.runtime_dir),
            timeout=PR_RESOLVE_TIMEOUT_SEC,
        )
    except Exception:
        return None, None


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

    # Owner input (only with the user's connect-time consent) and the
    # discovery re-look: neither waits on the network here, and neither
    # ever blocks the prompt or prints anything.
    capture_owner_input(payload, conv_id, cwd)
    spawn_relook(conv_id, cwd)

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
        pr_number, pr_url = resolve_pr(cwd, repo, branch)
        attrs["cardinal_pr_number"] = pr_number
        attrs["cardinal_pr_url"] = pr_url
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


# ---------------------------------------------------------------------------
# Decision capture (postToolUse). `cardinal-decision record` runs inside
# Cursor's agent sandbox, so it only validates and prints a marker line;
# this hook (outside the sandbox) gates, writes the ledger, resolves the
# PR, and emits the one cardinal.decision event.
# ---------------------------------------------------------------------------

def _tool_output_texts(tool_output: Any) -> list[str]:
    """Candidate stdout texts. Cursor documents postToolUse `tool_output`
    as a JSON-stringified result (`{"exitCode":0,"stdout":"..."}`); older
    payloads carry a dict or plain text."""
    texts: list[str] = []
    parsed: Any = tool_output
    if isinstance(tool_output, str):
        texts.append(tool_output)
        try:
            parsed = json.loads(tool_output)
        except ValueError:
            parsed = None
    if isinstance(parsed, dict):
        for key in ("stdout", "output", "text"):
            value = parsed.get(key)
            if isinstance(value, str):
                texts.append(value)
    return texts


_OPERATOR_CHARS = ";&|()\n"
_OPERATOR_SET = frozenset(_OPERATOR_CHARS)
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON_INTERPRETER_RE = re.compile(r"^python(\d+(\.\d+)*)?$")
_ENV_FLAGS_WITH_VALUE = frozenset({"-u", "--unset", "-C", "--chdir"})
_MENTIONS_RECORD_RE = re.compile(r"cardinal-decision[\"']?\s+record\b")


def _simple_commands(command: str) -> list[list[str]]:
    """Shell-aware split of `command` into the words of each simple
    command. `;`, `&`, `|`, `(`, `)` and newlines separate commands;
    quoting and escapes follow POSIX shlex; backslash-newline continues a
    line. Raises ValueError on unbalanced quotes."""
    text = command.replace("\\\r\n", " ").replace("\\\n", " ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=_OPERATOR_CHARS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    commands: list[list[str]] = []
    words: list[str] = []
    for token in lexer:
        if token and set(token) <= _OPERATOR_SET:
            if words:
                commands.append(words)
                words = []
            continue
        words.append(token)
    if words:
        commands.append(words)
    return commands


def _record_argv(words: list[str]) -> list[str] | None:
    """argv (starting at `record`) when this simple command runs
    `cardinal-decision record`, directly or as `python3 [flags] <path>`,
    optionally behind `VAR=x` assignments and `env [flags] VAR=x`."""
    i, n = 0, len(words)
    while i < n:
        word = words[i]
        if _ASSIGNMENT_RE.match(word):
            i += 1
        elif os.path.basename(word) == "env":
            i += 1
            while i < n and words[i].startswith("-"):
                i += 2 if words[i] in _ENV_FLAGS_WITH_VALUE else 1
        else:
            break
    if i < n and _PYTHON_INTERPRETER_RE.match(os.path.basename(words[i])):
        i += 1
        while i < n and words[i].startswith("-") and words[i] != "-":
            flag = words[i]
            if flag.startswith("--"):
                i += 1
            elif flag[1] in "XW":
                i += 1 if len(flag) > 2 else 2
            elif "c" in flag[1:] or "m" in flag[1:]:
                return None  # python -c / -m: not running the script
            else:
                i += 1
    if i + 1 < n and os.path.basename(words[i]) == "cardinal-decision" and words[i + 1] == "record":
        return words[i + 1:]
    return None


def decision_invocations(command: str) -> tuple[list[list[str]], str | None]:
    """(argv per `cardinal-decision record` run in `command`, parse error).
    Mentions in arguments (echo, printf, grep, cat of saved output) are not
    invocations. The error is set only when the command clearly tries to
    run `record` but cannot be tokenized, so the agent can be told."""
    if "cardinal-decision" not in command:
        return [], None
    try:
        commands = _simple_commands(command)
    except ValueError as err:
        if _MENTIONS_RECORD_RE.search(command):
            return [], f"could not parse the shell command ({err})"
        return [], None
    return [argv for argv in (_record_argv(words) for words in commands) if argv], None


def confirmation_markers(tool_output: Any) -> list[dict[str, Any]]:
    """Every well-formed marker line in the tool output, in order."""
    out: list[dict[str, Any]] = []
    for text in _tool_output_texts(tool_output):
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith(_decision_cli.MARKER_PREFIX):
                continue
            try:
                obj = json.loads(line[len(_decision_cli.MARKER_PREFIX):])
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("v") == 1:
                out.append(obj)
    return out


def _marker_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _marker_list(value: Any, cap: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)][:cap]


def record_decision(
    conv_id: str, marker: dict[str, Any], payload: dict[str, Any]
) -> tuple[str, dict[str, Any] | None]:
    """Local half of recording one confirmed decision: validate, assign the
    final id against the ledger, write the ledger. Returns the line for the
    agent and, when connected, the background job that resolves clusters
    + PR and emits the event (network work never runs in the hook)."""
    cwd = _marker_str(marker.get("cwd"))
    if not cwd or not os.path.isdir(cwd):
        cwd = cwd_from_payload(payload)
    repo_root = git(["rev-parse", "--show-toplevel"], cwd)
    head_sha = branch = repo = None
    if repo_root:
        head_sha = git(["rev-parse", "HEAD"], cwd)
        branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
        repo = canonical_repo(git(["remote", "get-url", "origin"], cwd))

    ledger = decisions.read_ledger(PATHS.runtime_dir, conv_id)
    by = marker.get("by")
    try:
        anchors = [
            decisions.parse_anchor(spec, repo_root, cwd)
            for spec in _marker_list(marker.get("anchor"), decisions.MAX_ANCHORS)
        ]
        decision = decisions.build_decision(
            choice=_marker_str(marker.get("choice")),
            question=_marker_str(marker.get("question")),
            rationale=_marker_str(marker.get("why")),
            decided_by=by if by in decisions.DECIDED_BY else "agent",
            alternatives=_marker_list(marker.get("alt"), decisions.MAX_ALTERNATIVES),
            follows_from=_marker_list(marker.get("follows"), decisions.MAX_LINKS),
            refines=_marker_list(marker.get("refines"), decisions.MAX_LINKS),
            supersedes=_marker_list(marker.get("supersedes"), decisions.MAX_LINKS),
            anchors=anchors,
            decision_id=_marker_str(marker.get("id")),
            existing=ledger,
        )
    except decisions.DecisionError as err:
        return f"Cardinal did NOT record the decision: {err}", None

    known = {entry["id"] for entry in ledger}
    unknown = [link["to"] for link in decision["links"] if link["to"] not in known]
    decisions.record_in_ledger(PATHS.runtime_dir, conv_id, decision)

    connected = otlp.connection_from_paths(PATHS) is not None
    job = None
    if connected:
        job = {
            "session_id": conv_id,
            "ts_ns": time.time_ns(),
            "decision": decision,
            "cwd": cwd,
            "repo_root": repo_root,
            "head_sha": head_sha,
            "repo": repo,
            "branch": branch,
        }
    lines = [f"Cardinal recorded decision {decision['id']}: {decision['choice']}"]
    if unknown:
        lines.append(
            f"Note: no earlier decision in this session has id {', '.join(unknown)}; "
            "the link was kept as given."
        )
    if not connected:
        lines.append("Cardinal telemetry isn't connected, so it was only saved locally.")
    return "\n".join(lines), job


def record_submitted_decisions(
    conv_id: str, command: str, tool_output: Any, payload: dict[str, Any]
) -> tuple[list[str], list[dict[str, Any]]]:
    """(additional_context lines, background decision jobs) for one tool
    call. The decision is derived from the argv of each real
    `cardinal-decision record` run in `command`; a marker line in the
    output only confirms that run (it must equal the argv-derived marker),
    so spoofed markers are ignored wherever they appear. Once the command
    runs `record`, the agent always gets an answer: anything that stops
    recording is reported as NOT recorded, with a short reason."""
    invocations, parse_error = decision_invocations(command)
    if parse_error:
        return [f"Cardinal did NOT record the decision: {parse_error}."], []
    if not invocations:
        return [], []
    try:
        enabled = decisions.is_enabled(PATHS.runtime_dir, os.environ.get(decisions.ENABLE_ENV))
    except Exception as err:
        return [f"Cardinal did NOT record the decision: {_short_error(err)}"], []
    if not enabled:
        return [
            "Cardinal decision capture is off, so this decision was not recorded. "
            "Only the user can turn it on (`cardinal-decision on` in their own terminal)."
        ], []

    unused = confirmation_markers(tool_output)
    contexts: list[str] = []
    jobs: list[dict[str, Any]] = []
    for argv in invocations:
        try:
            expected = _decision_cli.expected_marker(argv)
        except (_decision_cli.ArgvError, decisions.DecisionError) as err:
            contexts.append(
                f"Cardinal did NOT record the decision: invalid cardinal-decision arguments "
                f"({_short_text(err)})."
            )
            continue
        label = (expected.get("choice") or "?")[:80]
        wanted = _decision_cli.comparable(expected)
        match = next((m for m in unused if _decision_cli.comparable(m) == wanted), None)
        if match is None:
            contexts.append(
                f'Cardinal did NOT record the decision "{label}": the command output has no '
                "matching confirmation line from cardinal-decision (did the command fail, or "
                "was its output cut off?)."
            )
            continue
        unused.remove(match)
        try:
            line, job = record_decision(conv_id, match, payload)
        except Exception as err:
            contexts.append(f'Cardinal did NOT record the decision "{label}": {_short_error(err)}')
            continue
        contexts.append(line)
        if job:
            jobs.append(job)
    return contexts, jobs


def _short_text(err: BaseException) -> str:
    return " ".join(str(err).split())[:160] or type(err).__name__


def _short_error(err: BaseException) -> str:
    text = " ".join(str(err).split())
    return f"{type(err).__name__}: {text[:160]}" if text else type(err).__name__


# ---------------------------------------------------------------------------
# Background work (postToolUse). Cursor waits for the hook within its
# timeout and the docs don't say partial stdout survives a kill, so the
# hook does only local work and hands every network send — turn_tool /
# tool_result, decision clusters + `gh` PR lookup + emit — to a detached
# child (own session, /dev/null stdio) via an atomically written 0600
# spool file. Same pattern as the Gemini adapter.
# ---------------------------------------------------------------------------

def spawn_background(job: dict[str, Any]) -> None:
    tmp = None
    try:
        spool_dir = PATHS.runtime_dir / "spool"
        spool_dir.mkdir(parents=True, exist_ok=True)
        base = f"{job.get('kind')}-{os.getpid()}-{time.time_ns()}"
        tmp = spool_dir / f".{base}.tmp"
        path = spool_dir / f"{base}.json"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(job, fh, default=str)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        return
    if os.environ.get(BACKGROUND_INLINE_ENV) == "1":
        run_background_job(path)
        return
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--background", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass


def run_background_job(path: Path) -> None:
    try:
        job = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if isinstance(job, dict) and job.get("kind") == "post_tool_use":
        post_tool_use_background(job)
    elif isinstance(job, dict) and job.get("kind") == "relook":
        relook_background(job)


def post_tool_use_background(job: dict[str, Any]) -> None:
    connection = otlp.connection_from_paths(PATHS)
    if connection is None:
        return
    resource = job.get("resource")
    if not isinstance(resource, dict):
        resource = resource_attrs(PATHS.read_state())

    def send(records: list[dict[str, Any]]) -> None:
        otlp.emit_records(
            records, connection, resource,
            scope_name=SCOPE_NAME, scope_version=PLUGIN_VERSION,
            timeout=BACKGROUND_EMIT_TIMEOUT_SEC,
        )

    records = job.get("records")
    if isinstance(records, list) and records:
        send(records)
    for item in job.get("decisions") or []:
        try:
            decision = item["decision"]
            cache = decisions.cache_dir(PATHS.runtime_dir)
            clusters, scheme = decisions.code_clusters(
                decision["anchors"], item.get("repo_root"), item.get("head_sha"), cache,
            )
            pr_number, pr_url = decisions.resolve_pr(
                item.get("cwd") or os.getcwd(), item.get("repo"), item.get("branch"), cache,
            )
            attrs = decisions.decision_attributes(
                session_id=item["session_id"],
                decision=decision,
                code_clusters=clusters,
                cluster_scheme=scheme,
                repo=item.get("repo"),
                branch=item.get("branch"),
                head_sha=item.get("head_sha"),
                pr_number=pr_number,
                pr_url=pr_url,
            )
            send([log_record(decisions.DECISION_EVENT, attrs, int(item.get("ts_ns") or time.time_ns()))])
        except Exception:
            continue


def handle_post_tool_use(payload: dict[str, Any]) -> None:
    """Emit cardinal.turn_tool + tool_result from one payload; piggyback
    any staged notify message as `additional_context` output (once per
    band per turn). The tool call (any tool) is first captured in the
    local evidence spool and its id prepended to that output."""
    dump_debug_payload("postToolUse", payload)
    call = evidence_call(payload)
    failure = failure_line(call)
    evidence_line = capture_evidence(payload, call=call)
    # Cardinal's storyboard__preview / __publish: the authoring entry
    # answers for this call (Cursor keeps the LAST response of a field), so
    # this one says nothing and consumes nothing (staged items, the inbox,
    # a limits notice wait for the next tool call).
    owned = authoring_entry_owns(payload)
    conv_id = conv_id_from_payload(payload)
    if not conv_id:
        line = "\n\n".join(x for x in (failure, evidence_line) if x)
        if line and not owned:
            sys.stdout.write(json.dumps({"additional_context": line}))
            sys.stdout.flush()
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

    state["tool_seq"] += 1
    session.save_progress(PATHS, conv_id, state)

    contexts: list[str] = [x for x in (failure, evidence_line) if x]
    # Piggyback pending notify/warn context onto the hook output. This
    # is the Cursor adapter's substitute for Claude's inline
    # systemMessage on the submit hook — see Divergence E.
    try:
        msg = None if owned else limits.consume_notify(PATHS, conv_id)
        if msg:
            contexts.append(msg)
    except Exception:
        pass
    # Decisions submitted by a sandboxed `cardinal-decision record` call:
    # this hook is their only recorder (local work here, emit in the child).
    decision_jobs: list[dict[str, Any]] = []
    try:
        command = tool_input.get("command") or tool_input.get("cmd") or ""
        decision_contexts, decision_jobs = record_submitted_decisions(
            conv_id, str(command), tool_output, payload
        )
        contexts.extend(decision_contexts)
    except Exception as err:
        contexts.append(f"Cardinal did NOT record the decision: {_short_error(err)}")
    observe_cardinal(payload, conv_id)
    if not owned:
        contexts.extend(boundary_contexts(payload, conv_id))
    if contexts and not owned:
        sys.stdout.write(json.dumps({"additional_context": "\n\n".join(contexts)}))
        sys.stdout.flush()

    # Every network send (turn_tool/tool_result, decision clusters + PR +
    # emit) leaves the hook for a detached child.
    try:
        if otlp.connection_from_paths(PATHS) is not None:
            spawn_background({
                "kind": "post_tool_use",
                "resource": resource_attrs(PATHS.read_state(), payload),
                "records": records,
                "decisions": decision_jobs,
            })
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


# The stop entry's timeout is 10 s (cardinal-connect); the turn's end
# (sync check, owner-input flush) ends by this much after the hook started.
STOP_BUDGET_SEC = 7.0


def handle_stop(payload: dict[str, Any]) -> None:
    """The turn's end of an Investigation session
    (investigation_agent.turn_end): the inbox, one bounded sync check when
    the poller is stale, an open directive or the decision reminders, the
    owner-input outbox flush and the owner-input turn boundary (the
    repeat check needs it). Cursor may not block here (R1: `followup_message`
    is submitted as the next USER message, so advisory text would become
    owner input): turn_end STAGES what it would deliver, for the next
    postToolUse or sessionStart, and this hook PRINTS NOTHING, ever.

    Telemetry: Cursor's transcript format carries no token / rate-limit
    records (parity spec gap D), so there is no transcript sweep."""
    dump_debug_payload("stop", payload)
    conv_id = conv_id_from_payload(payload)
    if not conv_id:
        return
    try:
        from cardinal_core import investigation_agent

        # Never a re-entry from a Cardinal block (there is none), so
        # stop_hook_active is False; the Delivery's turn_end_block is None
        # on Cursor (investigation_turn.TURN_END_BLOCK) and is not printed.
        investigation_agent.turn_end(storyboard_wiring(), conv_id, stop_hook_active=False,
                                     deadline=START + STOP_BUDGET_SEC, cwd=cwd_from_payload(payload))
    except Exception:
        pass


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
        # Cross-adapter contract key; best-effort — Cursor's documented
        # subagentStop payload has no model field (gap D adjacent).
        "model": payload.get("model") or payload.get("modelName") or payload.get("model_name"),
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


def decision_context(conv_id: str) -> str | None:
    """Decision-capture instructions for the conversation's initial system
    context, or None when capture is off (the default).

    Cursor's only documented surface that puts hook output in front of
    the model at conversation start is sessionStart `additional_context`
    ("Additional context to add to the conversation's initial system
    context", cursor.com/docs/agent/hooks). beforeSubmitPrompt's output
    schema is `{continue, user_message}` only, so the per-prompt ledger
    refresh Claude gets on UserPromptSubmit is not available; the ledger
    shown here is the one at session start, and the postToolUse hook
    reports each recorded id back as additional_context."""
    if not decisions.is_enabled(PATHS.runtime_dir, os.environ.get(decisions.ENABLE_ENV)):
        return None
    entries = decisions.read_ledger(PATHS.runtime_dir, conv_id)
    return (
        "Cardinal decision capture is on for this session. When you make a choice that "
        "constrains later work (picking between approaches, settling an open question, or "
        "the user deciding something), record it right away with one terminal command:\n"
        f"python3 \"{DECISION_CLI}\" record --choice '<the option chosen, 2-7 words>' "
        "--question '<what had to be settled>' --why '<one sentence>' "
        "[--alt '<rejected option>']... [--by user] [--anchor <path>[::Symbol]]... "
        "[--follows|--refines|--supersedes <id>]\n"
        "Wrap every value in single quotes so the shell doesn't expand $ or backticks; "
        "if the hook sees different text than the command printed, it refuses to record. "
        "Run it from the workspace root. The command only checks the arguments and prints "
        "a line that Cardinal's hook records once the command finishes, so it needs no "
        "network or file access and works in the sandbox. The hook then tells you the "
        "recorded id. If the sandbox refuses to run it, ask to run it outside the sandbox: "
        "it only prints and changes nothing. Do not run `cardinal-decision on`, `off`, or "
        "`status`: those are for the user in their own terminal.\n"
        "Record choices, not progress, findings, or tool calls. Use --by user when the user "
        "made the call. Anchor the files or symbols the decision governs. Link a decision to "
        "an earlier one when it builds on, narrows, or replaces it.\n"
        "Decisions so far this session:\n"
        f"{decisions.render_ledger(entries)}"
    )


# ---------------------------------------------------------------------------
# Storyboard associations (cardinal_core.storyboard_agent): the session id +
# discovery block at sessionStart, and the files the agent edited
# (afterFileEdit). Cursor's beforeMCPExecution answers with a permission only
# (allow / deny / ask) and cannot rewrite the call's input, so there is no
# automatic context stamping here: the sessionStart text names
# `cardinal-storyboard context` for the agent to pass as `context`.
# afterFileEdit's shape is Cursor's documented one (file_path absolute,
# edits [{old_string, new_string}]); no payload was captured from a live
# Cursor (tests/test_cursor_storyboard.py, "from docs, unverified").
# ---------------------------------------------------------------------------

STORYBOARD_CLI = Path(__file__).resolve().parent.parent / "scripts" / "cardinal-storyboard"


def storyboard_wiring():
    from cardinal_core import storyboard_agent

    return storyboard_agent.Wiring("cursor", PATHS, PLUGIN_VERSION, cli=str(STORYBOARD_CLI))


# afterFileEdit's entry has 5 s; the edit lookup's one request ends by this.
EDIT_LOOKUP_BUDGET_SEC = 1.5


def handle_after_file_edit(payload: dict[str, Any]) -> None:
    """Record the edited file in storyboard_files (`context.paths`), then
    the edit lookup (storyboard_authoring.edit_lookup: the storyboards
    about that file's directory, the first edit there this session; one
    request, bounded). afterFileEdit has no documented output, so the block
    is STAGED and surfaces at the next postToolUse. No output."""
    dump_debug_payload("afterFileEdit", payload)
    path = payload.get("file_path") or payload.get("filePath")
    if not isinstance(path, str) or not path:
        return
    from cardinal_core import storyboard_agent

    conv_id = conv_id_from_payload(payload)
    cwd = cwd_from_payload(payload)
    wiring = storyboard_wiring()
    storyboard_agent.record_edits(wiring, conv_id, [path], cwd)
    if not conv_id:
        return
    try:
        from cardinal_core import storyboard_authoring as sa

        block = sa.edit_lookup(PATHS, "cursor", conv_id, path, cwd, connection=wiring.connection,
                               client=wiring.client, deadline=START + EDIT_LOOKUP_BUDGET_SEC)
        if block:
            stage_for_next_boundary(conv_id, block)
    except Exception:
        pass


def handle_session_start(payload: dict[str, Any]) -> None:
    started = time.monotonic()
    cwd = cwd_from_payload(payload)
    conv_id = conv_id_from_payload(payload)
    parts: list[str] = []
    if is_git_repo(cwd):
        context = CONVENTION_PROMPT
        try:
            standing = session.budget_standing(PATHS, conv_id, cwd)
            if standing:
                context = f"{CONVENTION_PROMPT}\n\n{standing}"
        except Exception:
            pass
        parts.append(context)
    if conv_id:
        try:
            decision = decision_context(conv_id)
            if decision:
                parts.append(decision)
        except Exception:
            pass
    try:
        # The Investigation (bootstrap: one request, its own 3 s bound), its
        # detached poller, what an earlier turn of this conversation staged,
        # then the session id line (only without an Investigation: the
        # guidance names it) + the storyboards that may relate to this
        # checkout, all inside the hook's 10 s. sessionStart is
        # fire-and-forget on Cursor (the agent does not wait for it).
        from cardinal_core import investigation_agent, storyboard_agent

        wiring = storyboard_wiring()
        live = investigation_agent.start(wiring, conv_id, None, cwd=cwd) if conv_id else None
        if conv_id:
            investigation_agent.spawn_poller(wiring, conv_id, str(INVESTIGATION_POLLER))
        if live:
            parts.append(live)
        if conv_id:
            parts.extend(staged_contexts(wiring, conv_id))
        storyboard = storyboard_agent.session_start_text(
            wiring, cwd, conv_id, include_session_line=not bool(live),
            deadline=min(started + 8.0, time.monotonic() + 2.0))
        if storyboard:
            parts.append(storyboard)
    except Exception:
        pass
    try:
        from cardinal_core import storyboard_authoring as sa

        for line in (sa.stray_key_warning(PATHS, "cursor", sid=conv_id),
                     sa.connect_hint(PATHS, "cursor")):
            if line:
                parts.append(line)
    except Exception:
        pass
    try:
        # Checks preview (opt-in, `scripts/cardinal-checks on|off`): make the
        # installed skill match the setting (it takes effect at the next
        # session: Cursor has no skill reload or skill-tool deny) and, while on
        # and connected, the routing rule. Local only.
        from cardinal_core import checks_install
        from cardinal_core import storyboard_authoring as sa

        _, rule = checks_install.session_start(
            "cursor", CHECKS_PLUGIN_ROOT, connected=sa.is_connected(PATHS, "cursor"))
        if rule:
            parts.append(rule)
    except Exception:
        pass
    if not parts:
        return
    sys.stdout.write(json.dumps({"additional_context": "\n\n".join(parts)}))
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Investigation sessions (cardinal_core.investigation_agent): bootstrap and
# guidance at sessionStart, deliveries at each tool boundary, the turn's end
# at stop. Cursor's context channels are sessionStart and postToolUse /
# postToolUseFailure `additional_context` only (beforeSubmitPrompt answers
# {continue, user_message}; stop's followup_message is a user turn;
# afterFileEdit has no output), so whatever is produced elsewhere is STAGED
# (investigation_turn.stage, <sid>.staged.json) and delivered at the next
# postToolUse / postToolUseFailure, or at a later sessionStart.
# ---------------------------------------------------------------------------

INVESTIGATION_POLLER = Path(__file__).resolve().parent / "investigation-poller.py"
# The plugin root the Checks preview template (checks/skill/) ships in.
CHECKS_PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# Payload keys that would name a subagent. Cursor documents none on tool
# hooks (subagentStart carries subagent_id); kept so a build that adds one
# keeps staged items and the inbox for the main thread.
SUBAGENT_KEYS = ("agent_id", "agentId", "subagent_id", "subagentId")


def is_subagent_payload(payload: dict[str, Any]) -> bool:
    return any(isinstance(payload.get(k), str) and payload.get(k) for k in SUBAGENT_KEYS)


def stage_for_next_boundary(conv_id: str, text: str) -> None:
    """Keep `text` for this conversation's next context-capable boundary
    (postToolUse, postToolUseFailure, sessionStart). Never raises."""
    try:
        from cardinal_core import investigation_turn

        investigation_turn.stage(Path.home(), conv_id, text)
    except Exception:
        pass


def staged_contexts(wiring: Any, conv_id: str) -> list[str]:
    """What was staged for this conversation, oldest first:
    investigation_agent.prompt (a session bound on this connection's org),
    else the plugin's own staged items (edit lookup, discovery re-look) of a
    conversation without an Investigation. Never raises."""
    try:
        from cardinal_core import investigation_agent, investigation_turn

        out = list(investigation_agent.prompt(wiring, conv_id).agent_context)
        out.extend(investigation_turn.consume_staged(Path.home(), conv_id))
        return out
    except Exception:
        return []


def boundary_contexts(payload: dict[str, Any], conv_id: str) -> list[str]:
    """A tool boundary (postToolUse / postToolUseFailure): keep the poller
    running, then investigation_agent.deliver (staged items, the inbox, one
    re-surfaced challenge or directive) and whatever else is staged. A
    subagent's tool call gets nothing and consumes nothing. Local only (no
    network). Never raises."""
    out: list[str] = []
    try:
        from cardinal_core import investigation_agent

        wiring = storyboard_wiring()
        investigation_agent.spawn_poller(wiring, conv_id, str(INVESTIGATION_POLLER))
        subagent = is_subagent_payload(payload)
        investigation_agent.deliver(wiring, conv_id, out.append, is_subagent=subagent)
        if not subagent:
            out.extend(staged_contexts(wiring, conv_id))
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Owner input (beforeSubmitPrompt): cardinal_core.owner_input.capture, the
# gate every runtime shares. Cursor is in owner_input.CONSENT_REQUIRED: it has
# no user-visible, non-blocking message per session, so nothing is captured
# until the user consented in their own terminal (`cardinal-connect
# --owner-input on`, which prints owner_input.consent_text in full). The
# kill switch (CARDINAL_OWNER_INPUT=0, ~/.cardinal/owner-input.json
# {"cursor": "off"}) wins over consent. Detached: the turn is taken under the
# session lock here and a detached process posts it, so the prompt never
# waits on the network. Nothing is printed and no prompt is ever blocked.
# ---------------------------------------------------------------------------

def capture_owner_input(payload: dict[str, Any], conv_id: str, cwd: str) -> str:
    """The capture status (a debug code, never the prompt). Never raises."""
    try:
        home = Path.home()
        sessions = home / ".cardinal" / "investigations" / "sessions"
        if "/" in conv_id or not (sessions / f"{conv_id}.json").exists():
            return "unbound"  # no import, no network for an unbound conversation
        from cardinal_core import owner_input

        wiring = storyboard_wiring()
        agent_id = next((payload.get(k) for k in SUBAGENT_KEYS if payload.get(k)), None)
        got = owner_input.capture(home, conv_id, payload.get("prompt"), runtime="cursor",
                                  conn=wiring.connection(), client=wiring.client, environ=wiring.environ,
                                  agent_id=agent_id if isinstance(agent_id, str) else None,
                                  deadline=START + owner_input.BUDGET, detached=True, cwd=cwd)
        # got.disclosure is never set on Cursor (consent stands in for it);
        # there is no channel to show it on beforeSubmitPrompt anyway.
        return got.status
    except Exception:
        return "error"


# ---------------------------------------------------------------------------
# Discovery re-look (beforeSubmitPrompt): when the branch or HEAD moved since
# this conversation's last look, the storyboards that may relate to the
# checkout (storyboard_authoring.relook_if_moved). Up to two requests, so it
# runs in the detached --background child and its block is STAGED.
# ---------------------------------------------------------------------------

RELOOK_BUDGET_SEC = 2.0


def spawn_relook(conv_id: str, cwd: str) -> None:
    try:
        from cardinal_core import storyboard_authoring as sa

        if sa.discovery_disabled(PATHS, "cursor") or not sa.is_connected(PATHS, "cursor"):
            return
        spawn_background({"kind": "relook", "session_id": conv_id, "cwd": cwd})
    except Exception:
        pass


def relook_background(job: dict[str, Any]) -> None:
    sid, cwd = job.get("session_id"), job.get("cwd")
    if not isinstance(sid, str) or not sid:
        return
    from cardinal_core import storyboard_authoring as sa

    wiring = storyboard_wiring()
    block = sa.relook_if_moved(PATHS, "cursor", sid, cwd, connection=wiring.connection, client=wiring.client,
                               deadline=time.monotonic() + RELOOK_BUDGET_SEC)
    if block:
        stage_for_next_boundary(sid, block)


# ---------------------------------------------------------------------------
# Which MCP server a tool call went to. Cursor's docs give the matcher
# spelling `MCP:<tool_name>` and no postToolUse field naming the server
# (cursor.com/docs/agent/hooks; github.com/cursor/cookbook/issues/50,
# reproduced on CLI 2026.09.28); the adapter's fixtures use
# `mcp__<server>__<tool>`. Neither shape is verified on a live Cursor. A
# name of the first shape carries its server; for any other shape, Cardinal
# is the server only when afterMCPExecution (which documents
# mcp_server_name) recorded this exact call (same tool, same input) for
# Cardinal's own `cardinal` entry. Another server's tool of the same name is
# never taken for Cardinal's: the preview renderer and the token store act
# on what the result names, with the Cardinal key.
# ---------------------------------------------------------------------------

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MCP_SERVER_KEYS = ("mcp_server_name", "mcpServerName")
MCP_CALLS_MAX = 32
MCP_CALL_TTL_SEC = 600.0
# How long a postToolUse for a Cardinal storyboard tool waits for
# afterMCPExecution's record (the order of the two is undocumented).
MCP_SERVER_WAIT_SEC = 1.5


def mcp_calls_path(conv_id: str) -> Path:
    return PATHS.runtime_dir / "mcp-calls" / f"{conv_id}.json"


def bare_mcp_tool(name: str) -> str:
    return name[len("MCP:"):] if name.startswith("MCP:") else name


def mcp_input_key(value: Any) -> str:
    """sha256 of the call's input as canonical JSON (a JSON string is
    decoded first: MCP hooks pass it as a string, postToolUse as either)."""
    try:
        canon = json.dumps(_decode_json_container(value), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _read_mcp_calls(conv_id: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(mcp_calls_path(conv_id).read_text())
    except (OSError, ValueError):
        return []
    now = time.time()
    return [e for e in data if isinstance(e, dict) and isinstance(e.get("at"), (int, float))
            and now - e["at"] < MCP_CALL_TTL_SEC] if isinstance(data, list) else []


def handle_after_mcp_execution(payload: dict[str, Any]) -> None:
    """Record that Cardinal's own MCP server ran this call (tool, input),
    for postToolUse to tell Cardinal's tools from another server's. Local
    only, 0600, bounded; no output (Cursor discards it)."""
    dump_debug_payload("afterMCPExecution", payload)
    conv_id = conv_id_from_payload(payload)
    name = _first(payload, "tool_name", "toolName")
    server = _first(payload, *MCP_SERVER_KEYS)
    if server not in CARDINAL_MCP_SERVERS or not isinstance(name, str) or not name:
        return
    if not conv_id or not SESSION_ID_RE.match(conv_id):
        return
    entries = _read_mcp_calls(conv_id)
    entries.append({"tool": bare_mcp_tool(name), "input": mcp_input_key(_first(payload, "tool_input", "toolInput")),
                    "server": server, "at": time.time()})
    path = mcp_calls_path(conv_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        from cardinal_core.paths import atomic_write_secret

        atomic_write_secret(path, json.dumps(entries[-MCP_CALLS_MAX:]))
    except Exception:
        pass


def resolve_mcp_name(payload: dict[str, Any], conv_id: str | None, wait: float = 0.0) -> tuple[Any, str | None]:
    """(tool name, server) to give storyboard_authoring: an
    `mcp__<server>__<tool>` name as is (it names its server); a name with a
    server field on the payload, or one afterMCPExecution recorded for
    Cardinal (waiting up to `wait` seconds for that record), as
    `MCP:<tool>` with that server; anything else as is with no server (not
    Cardinal's). Never raises."""
    raw = _first(payload, "tool_name", "toolName")
    if not isinstance(raw, str) or not raw or raw.startswith("mcp__"):
        return raw, None
    bare = bare_mcp_tool(raw)
    if not bare:
        return raw, None
    server = _first(payload, *MCP_SERVER_KEYS)
    if isinstance(server, str) and server:
        return f"MCP:{bare}", server
    if not conv_id or not SESSION_ID_RE.match(conv_id):
        return raw, None
    key = mcp_input_key(_first(payload, "tool_input", "toolInput"))
    give_up = time.monotonic() + max(0.0, wait)
    while True:
        for e in reversed(_read_mcp_calls(conv_id)):
            if e.get("tool") == bare and e.get("input") == key and e.get("server") in CARDINAL_MCP_SERVERS:
                return f"MCP:{bare}", e["server"]
        if time.monotonic() >= give_up:
            return raw, None
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# Storyboard authoring (cardinal_core.storyboard_authoring). The preview
# render (Chromium: ~10 s for a short storyboard, up to the core's 135 s
# budget) and the hero upload run in their own postToolUse entry:
# `--role authoring`, matcher AUTHORING_MATCHER, timeout
# storyboard_authoring.PREVIEW_HOOK_TIMEOUT_S (150 s). Cursor documents
# per-entry `timeout` (seconds, no maximum stated) and `matcher` on
# postToolUse (cursor.com/docs/agent/hooks). Cursor keeps the last response
# of a field when several hooks answer, so for those two tools the main
# entry stays silent (authoring_entry_owns).
# ---------------------------------------------------------------------------

AUTHORING_ROLE = "authoring"
# Matched by Cursor against `MCP:<tool_name>` (docs) or whatever it names the
# tool; written to match anywhere, whether Cursor searches or full-matches.
AUTHORING_MATCHER = ".*storyboard__(preview|publish).*"
AUTHORING_TOOL_RE = re.compile(r"(?:^|__|:)storyboard__(preview|publish)$")
AUTHORING_NEEDLE = f"--role {AUTHORING_ROLE} # cardinal-cursor-plugin"


def authoring_registered(payload: dict[str, Any]) -> bool:
    """Whether the user's or the project's hooks.json has the authoring
    postToolUse entry (an install from before it has only the main one)."""
    files = [PATHS.home / "hooks.json"]
    roots = payload.get("workspace_roots") or payload.get("workspaceRoots")
    for root in (roots if isinstance(roots, list) else [])[:4]:
        if isinstance(root, str) and root:
            files.append(Path(root) / ".cursor" / "hooks.json")
    for f in files:
        try:
            entries = json.loads(f.read_text()).get("hooks", {}).get("postToolUse")
        except (OSError, ValueError, AttributeError):
            continue
        if any(isinstance(e, dict) and AUTHORING_NEEDLE in str(e.get("command") or "")
               for e in (entries if isinstance(entries, list) else [])):
            return True
    return False


def authoring_entry_owns(payload: dict[str, Any]) -> bool:
    """A storyboard__preview / storyboard__publish call that the registered
    authoring entry answers for."""
    raw = _first(payload, "tool_name", "toolName")
    return isinstance(raw, str) and bool(AUTHORING_TOOL_RE.search(raw)) and authoring_registered(payload)


def observe_cardinal(payload: dict[str, Any], conv_id: str) -> None:
    """Store the evidence token of Cardinal's storyboard__create / add_act
    / preview (storyboard_authoring.observe_cardinal_result). Local only;
    never raises."""
    try:
        from cardinal_core import storyboard_authoring as sa

        raw = _first(payload, "tool_name", "toolName")
        wait = MCP_SERVER_WAIT_SEC if isinstance(raw, str) and bare_mcp_tool(raw) in sa.TOKEN_TOOLS else 0.0
        name, server = resolve_mcp_name(payload, conv_id, wait=wait)
        sa.observe_cardinal_result(PATHS, "cursor", name, _decode_json_container(_first(payload, "tool_output", "toolOutput")),
                                   session_id=conv_id, server=server)
    except Exception:
        pass


def handle_post_tool_use_authoring(payload: dict[str, Any]) -> None:
    """postToolUse `--role authoring`: after Cardinal's storyboard__preview,
    render the scenes, cover and link-preview mock locally (on_preview);
    after its storyboard__publish, upload the hero render (on_publish). The
    text goes out as additional_context. Anything else (another server's
    tool of the same name, a matcher Cursor did not apply): nothing."""
    dump_debug_payload("postToolUse-authoring", payload)
    raw = _first(payload, "tool_name", "toolName")
    if not isinstance(raw, str) or not AUTHORING_TOOL_RE.search(raw):
        return
    conv_id = conv_id_from_payload(payload)
    from cardinal_core import storyboard_authoring as sa

    name, server = resolve_mcp_name(payload, conv_id, wait=MCP_SERVER_WAIT_SEC)
    result = _decode_json_container(_first(payload, "tool_output", "toolOutput"))
    text = None
    if sa.is_cardinal_tool("cursor", name, "storyboard__preview", server=server):
        tool_input = _decode_json_container(_first(payload, "tool_input", "toolInput"))
        text = sa.on_preview(PATHS, "cursor", result, session_id=conv_id,
                             tool_input=tool_input if isinstance(tool_input, dict) else None, started=START)
    elif sa.is_cardinal_tool("cursor", name, "storyboard__publish", server=server):
        wiring = storyboard_wiring()
        text = sa.on_publish(PATHS, "cursor", result, connection=wiring.connection, client=wiring.client)
    else:
        return
    contexts = [text] if text else []
    if conv_id:
        # This entry answers for the call, so it carries the boundary's
        # deliveries too (the main entry left them).
        contexts.extend(boundary_contexts(payload, conv_id))
    if contexts:
        sys.stdout.write(json.dumps({"additional_context": "\n\n".join(contexts)}))
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
    "postToolUseFailure": handle_post_tool_use_failure,
    "afterFileEdit": handle_after_file_edit,
    "preCompact": handle_pre_compact,
    "stop": handle_stop,
    "subagentStop": handle_subagent_stop,
    "afterAgentResponse": handle_after_agent_response,
    "afterAgentThought": handle_after_agent_thought,
    "afterMCPExecution": handle_after_mcp_execution,
}
# `--role <name>`: a second entry for the same event (Cursor runs every
# matching entry), with its own matcher and timeout.
ROLE_HANDLERS = {
    ("postToolUse", AUTHORING_ROLE): handle_post_tool_use_authoring,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=False, default=None)
    parser.add_argument("--role", default=None)
    parser.add_argument("--background", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.background is not None:
        try:
            run_background_job(Path(args.background))
        except Exception:
            pass
        silent_exit()

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    event = args.event or payload.get("hook_event_name") or payload.get("hookEventName")
    if args.role is not None:
        handler = ROLE_HANDLERS.get((event, args.role)) if isinstance(event, str) else None
    else:
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
