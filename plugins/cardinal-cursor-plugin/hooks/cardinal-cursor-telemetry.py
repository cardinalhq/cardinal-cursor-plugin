#!/usr/bin/env python3
"""Emit Cardinal agent-session telemetry from Cursor hooks.

Cursor exposes hooks that closely mirror Claude Code's, so this file
ports the Codex plugin's telemetry hook to Cursor's event names,
payload shapes, and output schema. See docs/specs/cursor-parity.md for
the full mapping.

Failures are best-effort and silent: telemetry must not break the agent
loop. When docs are ambiguous (transcript_path contents, per-turn token
records), the v0.1.0 hook emits what it can from documented payload
fields and no-ops the rest — see the P0 spike in the parity spec.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _plugin_version  # noqa: E402


PLUGIN_VERSION = _plugin_version.plugin_version()
HOOK_TIMEOUT_SEC = 2.0

CURSOR_DIR = Path.home() / ".cursor"
STATE_PATH = CURSOR_DIR / "cardinal.json"
SECRETS_PATH = CURSOR_DIR / "cardinal-secrets.json"
TELEMETRY_DIR = CURSOR_DIR / "cardinal" / "telemetry"
PLAN_STAMP_PATH = TELEMETRY_DIR / "plan.json"

# Env-gated raw-payload dump for shape capture (mirrors the Codex
# plugin's P5 affordance). Off by default; writes nothing unless
# CARDINAL_CURSOR_DEBUG_PAYLOADS=1. Used for the P0 spike to inspect
# afterAgentResponse / afterAgentThought / transcript payloads.
DEBUG_PAYLOADS_ENV = "CARDINAL_CURSOR_DEBUG_PAYLOADS"
DEBUG_DIR = TELEMETRY_DIR / "debug"

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

REMOTE_URL_RE = re.compile(r"(?:git@|https?://)([^:/]+)[:/]([^/]+)/(.+?)(?:\.git)?/?$")
EXIT_CODE_RE = re.compile(r"(?:exit(?:ed)?|status)[ :]+(-?\d+)", re.IGNORECASE)
SESSION_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "trunk"})

# Noise words that appear between `worktree-` and the real name in
# EnterWorktree-style branches. Kept in lockstep with the Claude/Codex
# plugins' worktree strippers and conductor's normalizeInitiativeName.
WORKTREE_NOISE = frozenset({
    "fix", "feat", "bug", "bugfix", "issue", "issues", "pr",
})
NUMERIC_SEGMENT_RE = re.compile(r"^\d+$")
PREFIX_TO_TYPE = {
    "feat": "feature",
    "feature": "feature",
    "perf": "feature",
    "fix": "bugfix",
    "bugfix": "bugfix",
    "refactor": "refactor",
    "cleanup": "refactor",
    "infra": "infra",
    "chore": "infra",
    "test": "infra",
    "tests": "infra",
    "ci": "infra",
    "build": "infra",
    "deps": "infra",
    "docs": "infra",
    "doc": "infra",
    "research": "research",
    "spike": "research",
}


# ---------------------------------------------------------------------------
# Pricing — kept for symmetry with the Codex plugin. Cursor commonly runs
# non-OpenAI models; those land at cost_usd=None until P5 extends the
# table (see docs/specs/cursor-parity.md accepted asymmetries).
# ---------------------------------------------------------------------------
MODEL_PRICING_USD_PER_M: dict[str, dict[str, float]] = {
    "gpt-5":         {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5-codex":   {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5-mini":    {"input": 0.25, "cached_input": 0.025, "output":  2.00},
    "gpt-5-nano":    {"input": 0.05, "cached_input": 0.005, "output":  0.40},
    "o3":            {"input": 2.00, "cached_input": 0.500, "output":  8.00},
    "o3-mini":       {"input": 1.10, "cached_input": 0.550, "output":  4.40},
    "o4-mini":       {"input": 1.10, "cached_input": 0.275, "output":  4.40},
}


def price_for_model(model: str | None) -> dict[str, float] | None:
    if not model:
        return None
    if model in MODEL_PRICING_USD_PER_M:
        return MODEL_PRICING_USD_PER_M[model]
    match = ""
    for key in MODEL_PRICING_USD_PER_M:
        if model.startswith(key) and len(key) > len(match):
            match = key
    return MODEL_PRICING_USD_PER_M.get(match) if match else None


def compute_cost_usd(model: str | None, usage: dict[str, Any]) -> float | None:
    price = price_for_model(model)
    if price is None:
        return None
    input_total = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    non_cached_input = max(0, input_total - cached)
    cost = (
        non_cached_input * price["input"]
        + cached          * price["cached_input"]
        + output          * price["output"]
    ) / 1_000_000.0
    return round(cost, 6)


# ---------------------------------------------------------------------------
# Common utilities
# ---------------------------------------------------------------------------

def silent_exit() -> None:
    sys.exit(0)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def safe_conv(conv_id: str) -> str:
    return SESSION_SAFE_RE.sub("_", conv_id)[:128]


def progress_path(conv_id: str) -> Path:
    return TELEMETRY_DIR / f"{safe_conv(conv_id)}.json"


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def kv(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def parse_ts_ns(raw: Any, fallback_ns: int) -> int:
    if isinstance(raw, str) and raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1_000_000_000)
        except ValueError:
            return fallback_ns
    return fallback_ns


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


def load_connection() -> tuple[dict[str, Any], dict[str, Any]] | None:
    state = read_json(STATE_PATH)
    secrets = read_json(SECRETS_PATH)
    endpoint = state.get("ingest_endpoint")
    api_key = secrets.get("ingest_api_key")
    if not endpoint or not api_key:
        return None
    return state, secrets


def resource_attrs(state: dict[str, Any]) -> dict[str, str]:
    return {
        "service.name": "cursor",
        "agent.runtime": "cursor",
        "deployment.environment": str(state.get("deployment_environment") or "unknown"),
        "user.email": str(state.get("user_email") or "unknown"),
        "cardinal.org": str(state.get("org_slug") or state.get("org_id") or "unknown"),
        "cardinal.plugin_version": PLUGIN_VERSION,
    }


def emit_records(records: list[dict[str, Any]]) -> None:
    if not records:
        return
    conn = load_connection()
    if not conn:
        return
    state, secrets = conn
    endpoint = str(state.get("ingest_endpoint")).rstrip("/")
    api_header = str(secrets.get("ingest_api_header") or "x-cardinalhq-api-key")
    api_key = str(secrets.get("ingest_api_key"))

    body = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [kv(k, v) for k, v in resource_attrs(state).items()],
                },
                "scopeLogs": [
                    {
                        "scope": {
                            "name": "cardinal-cursor-plugin",
                            "version": PLUGIN_VERSION,
                        },
                        "logRecords": records,
                    }
                ],
            }
        ]
    }
    req = urllib.request.Request(
        endpoint + "/v1/logs",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            api_header: api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HOOK_TIMEOUT_SEC):
            pass
    except (urllib.error.URLError, OSError, TimeoutError):
        pass


def log_record(event_name: str, attrs: dict[str, Any], ts_ns: int) -> dict[str, Any]:
    all_attrs = {"event_name": event_name, **attrs}
    return {
        "timeUnixNano": str(ts_ns),
        "observedTimeUnixNano": str(ts_ns),
        "severityNumber": 9,
        "severityText": "INFO",
        "body": {"stringValue": event_name},
        "attributes": [kv(k, v) for k, v in all_attrs.items() if v is not None and v != ""],
    }


# ---------------------------------------------------------------------------
# Git + initiative resolution (verbatim port from Codex)
# ---------------------------------------------------------------------------

def git(args: list[str], cwd: str) -> str | None:
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


def canonical_repo(remote_url: str | None) -> str | None:
    if not remote_url:
        return None
    m = REMOTE_URL_RE.match(remote_url.strip())
    if not m:
        return None
    name = re.sub(r"\.git$", "", m.group(3))
    return f"{m.group(2)}/{name}" if m.group(2) and name else None


def strip_worktree_noise(name: str) -> str:
    """worktree-fix-1018-github-app-repo-picker → github-app-repo-picker."""
    if not name.startswith("worktree-"):
        return name
    segs = name.split("-")
    i = 1
    while i < len(segs) and (
        segs[i] in WORKTREE_NOISE or NUMERIC_SEGMENT_RE.match(segs[i])
    ):
        i += 1
    if i < len(segs):
        return "-".join(segs[i:])
    return name


def resolve_initiative(branch: str | None) -> tuple[str | None, str]:
    if not branch or branch == "HEAD":
        return None, "research"
    if branch in PROTECTED_BRANCHES:
        return None, "research"
    if "/" in branch:
        prefix, _, rest = branch.partition("/")
        mapped = PREFIX_TO_TYPE.get(prefix.lower())
        if mapped and rest:
            return strip_worktree_noise(rest), mapped
    return strip_worktree_noise(branch), "feature"


COMMAND_RE = re.compile(r"^\s*/([A-Za-z0-9][\w:-]*)")
COMMAND_TAG_RE = re.compile(r"<command-name>\s*/?([\w:-]+)\s*</command-name>")


def detect_command(prompt: Any) -> str | None:
    if not isinstance(prompt, str):
        return None
    m = COMMAND_RE.match(prompt)
    if m:
        return m.group(1)
    m = COMMAND_TAG_RE.search(prompt)
    if m:
        return m.group(1)
    return None


def read_plan_stamp() -> dict[str, Any]:
    """{plan_type, rate_limit_tier} from the last-seen rate_limits block,
    or {} — callers merge it into event attrs (missing keys are skipped
    by log_record's None/empty filter). The stamp file is populated by
    the future transcript-parsing path (see P0 spike); for now, an
    empty stamp is the norm on Cursor."""
    blob = read_json(PLAN_STAMP_PATH)
    out: dict[str, Any] = {}
    for key in ("plan_type", "rate_limit_tier"):
        v = blob.get(key)
        if isinstance(v, str) and v:
            out[key] = v
    return out


def dump_debug_payload(event: str, payload: dict[str, Any]) -> None:
    """Env-gated raw hook-payload dump for the P0 spike. No-op unless
    CARDINAL_CURSOR_DEBUG_PAYLOADS=1."""
    if os.environ.get(DEBUG_PAYLOADS_ENV) != "1":
        return
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path = DEBUG_DIR / f"{event}-{time.time_ns()}.json"
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def _limits():
    try:
        import _limits_common as lc
        return lc
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Bash classifier (verbatim port from Codex) — used on postToolUse when
# the tool call is a shell execution.
# ---------------------------------------------------------------------------
BASH_CLASS_RANK = (
    "file-write",
    "git-write",
    "pkg",
    "network",
    "build",
    "test",
    "git-read",
    "file-read",
    "other",
)

BASH_CMD_CLASS = {
    "pytest": "test", "tox": "test", "jest": "test", "vitest": "test",
    "rspec": "test", "phpunit": "test",
    "make": "build", "cmake": "build", "tsc": "build", "gradle": "build",
    "mvn": "build", "gcc": "build", "clang": "build", "webpack": "build",
    "pip": "pkg", "pip3": "pkg", "brew": "pkg", "gem": "pkg",
    "apt": "pkg", "apt-get": "pkg", "yum": "pkg", "dnf": "pkg",
    "apk": "pkg", "poetry": "pkg", "uv": "pkg",
    "ls": "file-read", "cat": "file-read", "find": "file-read",
    "grep": "file-read", "rg": "file-read", "head": "file-read",
    "tail": "file-read", "wc": "file-read", "du": "file-read",
    "df": "file-read", "stat": "file-read", "file": "file-read",
    "tree": "file-read", "which": "file-read", "pwd": "file-read",
    "less": "file-read", "more": "file-read", "diff": "file-read",
    "awk": "file-read", "echo": "file-read", "sort": "file-read",
    "uniq": "file-read", "cut": "file-read", "jq": "file-read",
    "rm": "file-write", "mv": "file-write", "cp": "file-write",
    "mkdir": "file-write", "rmdir": "file-write", "chmod": "file-write",
    "chown": "file-write", "touch": "file-write", "ln": "file-write",
    "sed": "file-write", "tee": "file-write", "truncate": "file-write",
    "dd": "file-write", "tar": "file-write", "unzip": "file-write",
    "zip": "file-write",
    "curl": "network", "wget": "network", "gh": "network",
    "ssh": "network", "scp": "network", "rsync": "network",
    "nc": "network", "ping": "network", "dig": "network",
    "host": "network", "nslookup": "network",
}

GIT_READ_SUBS = {
    "status", "log", "diff", "show", "blame", "shortlog", "reflog",
    "describe", "rev-parse", "ls-files", "ls-remote", "ls-tree",
    "cat-file", "grep",
}
BASH_MULTIPLEX_CLASS = {
    "git": ({s: "git-read" for s in GIT_READ_SUBS}, "git-write"),
    "go": (
        {"test": "test", "vet": "test",
         "build": "build", "run": "build", "generate": "build",
         "get": "pkg", "install": "pkg", "mod": "pkg"},
        "other",
    ),
    "cargo": (
        {"test": "test", "bench": "test",
         "build": "build", "check": "build", "run": "build",
         "clippy": "build",
         "add": "pkg", "install": "pkg", "update": "pkg",
         "remove": "pkg"},
        "other",
    ),
    "npm": (
        {"test": "test", "run": "build", "exec": "build"},
        "pkg",
    ),
    "pnpm": (
        {"test": "test", "run": "build", "exec": "build"},
        "pkg",
    ),
    "yarn": (
        {"test": "test", "run": "build"},
        "pkg",
    ),
    "bun": (
        {"test": "test", "run": "build", "build": "build"},
        "pkg",
    ),
}


def classify_bash_command(command: str) -> tuple[str, bool] | None:
    for sep in ("&&", "||", ";", "|", "\n"):
        command = command.replace(sep, "\x00")
    classes: set[str] = set()
    for segment in command.split("\x00"):
        words = segment.split()
        while words and ("=" in words[0] or words[0] == "sudo"):
            words.pop(0)
        if not words:
            continue
        cmd = words[0].rsplit("/", 1)[-1]
        mux = BASH_MULTIPLEX_CLASS.get(cmd)
        if mux is not None:
            sub_map, default = mux
            sub = words[1] if len(words) > 1 else ""
            classes.add(sub_map.get(sub, default))
        else:
            classes.add(BASH_CMD_CLASS.get(cmd, "other"))
    if not classes:
        return None
    winner = min(classes, key=BASH_CLASS_RANK.index)
    return winner, len(classes) > 1


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
    tools follow the mcp__ prefix. Target hint applies to shell tools
    (the executed command), letting the caller feed it to
    classify_bash_command."""
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
# Spend-limits gate (beforeSubmitPrompt) — the three-tier resolution
# from docs/specs/cursor-parity.md Divergence E.
# ---------------------------------------------------------------------------

def limits_gate_output(conv_id: str) -> dict[str, Any] | None:
    """Cursor sync gate. Returns the beforeSubmitPrompt output JSON
    (`{"continue": false, "user_message": ...}` for a block, `None`
    otherwise). Notify/warn context is STAGED as
    `<conv>.notify.json` for the next postToolUse to surface — the
    Cursor beforeSubmitPrompt output schema has no `additional_context`
    slot.

    Severity → channel mapping:
      block                       → {continue:false, user_message}
      warn + CARDINAL_CURSOR_STRICT_WARN=1 → escalate to block (server
                                    user_message copies through)
      warn (default) / notify     → stage `<conv>.notify.json`;
                                    postToolUse surfaces it once as
                                    additional_context.
    Warn/notify obey band hysteresis (only stage when the band RISES);
    a block is enforced every turn while in force.
    """
    lc = _limits()
    if lc is None:
        return None
    verdict = lc.read_verdict(conv_id)
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

    def _block_body(source_decision: str) -> dict[str, Any]:
        reason = (
            verdict.get("block_reason")
            or verdict.get("user_message")
            or "A Cardinal spend limit for this work has been reached."
        )
        if source_decision == "warn":
            reason = (
                verdict.get("user_message")
                or f"[warn escalated to block via {lc.STRICT_WARN_ENV}]\n{reason}"
            )
        return {"continue": False, "user_message": reason}

    if decision == "block" and age <= lc.BLOCK_MAX_AGE_SEC:
        if not lc.override_path(conv_id).exists():
            return _block_body("block")
        decision = "warn"  # overridden: keep the human-visible standing

    if band <= 0 or age > lc.WARN_MAX_AGE_SEC:
        return None

    if decision == "warn" and lc.strict_warn_enabled():
        return _block_body("warn")

    # Non-blocking band — stage a notify message for postToolUse.
    ack = lc._read_json_file(lc.ack_path(conv_id))
    try:
        last_band = int(ack.get("band") or 0)
    except (TypeError, ValueError):
        last_band = 0
    if band <= last_band:
        return None

    parts: list[str] = []
    agent_context = verdict.get("agent_context")
    if isinstance(agent_context, str) and agent_context:
        parts.append(agent_context)
    user_message = verdict.get("user_message")
    if decision == "warn" and isinstance(user_message, str) and user_message:
        parts.append(user_message)
    if parts:
        lc.atomic_write_json(
            lc.notify_path(conv_id), {"message": "\n\n".join(parts), "band": band, "staged_at": time.time()}
        )
        lc.atomic_write_json(
            lc.ack_path(conv_id), {"band": band, "surfaced_at": time.time()}
        )
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
        emit_records([log_record("cardinal.git_state", attrs, time.time_ns())])

    try:
        lc = _limits()
        if lc is not None:
            lc.maybe_refresh_verdict(conv_id=conv_id, repo=repo, branch=branch)
    except Exception:
        pass


def _load_progress(conv_id: str) -> dict[str, Any]:
    progress = read_json(progress_path(conv_id))
    return {
        "user_turn_seq": int(progress.get("user_turn_seq") or 0),
        "turn_seq": int(progress.get("turn_seq") or 0),
        "tool_seq": int(progress.get("tool_seq") or 0),
        "last_prompt_generation": progress.get("last_prompt_generation"),
    }


def _save_progress(conv_id: str, state: dict[str, Any]) -> None:
    atomic_write_json(progress_path(conv_id), {
        **state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def _tick_turn(conv_id: str, generation_id: Any, state: dict[str, Any]) -> None:
    """Cursor doesn't expose a `user_message` transcript boundary, so
    we advance turn counters when we first see a new generation_id.
    This runs on postToolUse (the first tool of a turn), giving us
    (user_turn_seq, turn_seq, tool_seq) that totally-orders the tool
    stream across hook firings."""
    gen = str(generation_id) if generation_id is not None else None
    if gen and gen != state.get("last_prompt_generation"):
        state["user_turn_seq"] += 1
        state["turn_seq"] = 0
        state["tool_seq"] = 0
        state["last_prompt_generation"] = gen


def handle_post_tool_use(payload: dict[str, Any]) -> None:
    """Emit cardinal.turn_tool + tool_result from one payload; piggyback
    any staged notify message as `additional_context` output (once per
    band per turn)."""
    dump_debug_payload("postToolUse", payload)
    conv_id = conv_id_from_payload(payload)
    if not conv_id:
        return
    state = _load_progress(conv_id)
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

    emit_records(records)
    state["tool_seq"] += 1
    _save_progress(conv_id, state)

    # Piggyback pending notify/warn context onto the hook output. This
    # is the Cursor plugin's substitute for Claude's inline
    # systemMessage on the submit hook — see divergence E.
    try:
        lc = _limits()
        if lc is not None:
            msg = lc.consume_notify(conv_id)
            if msg:
                sys.stdout.write(json.dumps({"additional_context": msg}))
                sys.stdout.flush()
    except Exception:
        pass


def handle_pre_compact(payload: dict[str, Any]) -> None:
    """No-op placeholder for parity with the Codex plugin. Kept so future
    turn-boundary needs have a wired handler without needing a
    re-connect."""
    dump_debug_payload("preCompact", payload)


def handle_stop(payload: dict[str, Any]) -> None:
    """Best-effort transcript sweep. Cursor's transcript_path format is
    not documented (see P0 spike in cursor-parity.md); v0.1.0 only
    captures the raw payload when the debug env var is set. When the
    spike identifies token/rate-limit records, the token-emission logic
    from the Codex plugin ports here.
    """
    dump_debug_payload("stop", payload)


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
    payload. No total_tokens field is documented on Cursor, so v0.1.0
    emits duration / message / tool-call counts; token totals join once
    the P0 spike identifies a source (may be inside
    agent_transcript_path)."""
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
    emit_records([log_record("cardinal.subagent_usage", attrs, time.time_ns())])


# ---------------------------------------------------------------------------
# sessionStart: initiative-convention prompt + one-shot budget standing
# ---------------------------------------------------------------------------

CONVENTION_PROMPT = (
    "You are running inside a Cardinal-instrumented Cursor session. "
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
    "on main/master/develop/"
    "trunk are treated as research/scoping work — when intent "
    "crystallises into a deliverable, cut a typed branch using this "
    "convention. Off-convention branches get a stable name but "
    "default to type 'feature', so the convention is the way to "
    "ensure correct classification."
)


def _is_git_repo(cwd: str) -> bool:
    return git(["rev-parse", "--is-inside-work-tree"], cwd) == "true"


def _budget_standing(conv_id: str | None, cwd: str) -> str | None:
    """One synchronous limits fetch at session start (short timeout,
    fail open). Warm-writes the verdict file the per-turn sync gate
    reads. No-op when the backend doesn't advertise the limits
    protocol."""
    if not conv_id:
        return None
    lc = _limits()
    if lc is None or not lc.limits_config():
        return None
    repo, branch = lc.git_facts(cwd)
    verdict = lc.maybe_refresh_verdict(
        conv_id=conv_id, repo=repo, branch=branch, force=True, timeout=1.5
    )
    if not verdict:
        return None
    lines = lc.standing_lines(verdict)
    if not lines:
        return None
    parts = ["Cardinal spend budgets apply to this session:"]
    parts.extend(lines)
    user_message = verdict.get("user_message")
    if isinstance(user_message, str) and user_message:
        parts.append(user_message)
    parts.append(
        "Work economically as budgets tighten; budget standing updates "
        "arrive automatically as the session proceeds."
    )
    return "\n".join(parts)


def handle_session_start(payload: dict[str, Any]) -> None:
    cwd = cwd_from_payload(payload)
    if not _is_git_repo(cwd):
        return
    context = CONVENTION_PROMPT
    try:
        standing = _budget_standing(conv_id_from_payload(payload), cwd)
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
