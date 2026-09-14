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
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _decision_cli  # noqa: E402
import _plugin_version  # noqa: E402
from cardinal_core import decisions, limits, otlp, session  # noqa: E402
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

# postToolUse does only local work; its network sends run in a detached
# `--background <spool>` child. Test-only: run that job inline instead.
BACKGROUND_INLINE_ENV = "CARDINAL_CURSOR_BACKGROUND_INLINE"
BACKGROUND_EMIT_TIMEOUT_SEC = 3.0

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

    state["tool_seq"] += 1
    session.save_progress(PATHS, conv_id, state)

    contexts: list[str] = []
    # Piggyback pending notify/warn context onto the hook output. This
    # is the Cursor adapter's substitute for Claude's inline
    # systemMessage on the submit hook — see Divergence E.
    try:
        msg = limits.consume_notify(PATHS, conv_id)
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
    if contexts:
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


def handle_session_start(payload: dict[str, Any]) -> None:
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
    if not parts:
        return
    sys.stdout.write(json.dumps({"additional_context": "\n\n".join(parts)}))
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
