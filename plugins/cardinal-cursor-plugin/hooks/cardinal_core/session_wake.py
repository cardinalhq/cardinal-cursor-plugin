"""Owner-woken sessions: resume a stopped, sandboxed agent session when its
owner posts a decision addressed to it (docs/specs/resume-and-wake.md §5).

Why: an Investigation outlives the session that did its work, and an owner
decision posted from the web reaches a session only at its own hook points.
A stopped session receives nothing until the owner types into that window.
The waker closes that gap for the one case the owner authorized explicitly,
and for nothing else.

Authorization (§5.1). Arming IS the decision: an `owner.decided` event the
owner posted on the Investigation page from their signed-in web session,
addressed to the session (`to_session_id`). maestro accepts that route only
from an OIDC web session of the investigation's author (proof C-1: the
plugin's API key, the only Cardinal credential an agent holds, is refused).
Nothing is armed from inside a session. The machine opt-in is the owner
running `cardinal-wake enable` (a per-user launchd agent); `--launch` also
allows unattended launch for runtimes that passed their gates (§6), else the
waker only notifies.

The daemon itself (B1). `enable` vets what launchd will run: the interpreter
is this Python or /usr/bin/python3, resolved outside $HOME, git work trees
under $HOME, approved roots and temp dirs, and runs with -I; the launch
binaries (and node, when one is a node script) resolve outside git work
trees under $HOME, approved roots and temp dirs; PATH (the plist's and every
woken child's) is the system dirs plus those binaries' directories, never the
shell's; each file's path, size, mtime and inode are recorded. Every launch
re-checks them (binaries_problem: unchanged, still outside those places and
outside the session's working directory), else a notification.

One pass (run_once; launchd runs `cardinal-wake run --once` every
RUN_INTERVAL seconds from the stable copy in ~/.cardinal/wake/lib/):
  1. settle every in-flight ledger entry: a launch the waker was interrupted
     in (its child recovered by argv, else FAILED, never retried), a child
     that exited (what it ran is verified, then ACKNOWLEDGED with the
     session's disposition, else FAILED), a child past WAKE_TIMEOUT
     (TIMED_OUT: left running, the owner notified, still judged when it
     exits). Runs whether or not the opt-in is on.
  2. with the opt-in on, every candidate session: a binding touched within
     CANDIDATE_WINDOW, is_author true, the owner_decision capability on (a
     ~/.cardinal binding decides alone; a relocated one only for sessions
     with no Codex or Claude records). Its events are read straight from
     Cardinal (read-investigation-events with the plugin's key, that
     runtime's first, the binding's org), never from the local inbox, so a
     local file cannot fake a decision. A decision e wakes sid only when
     (§5.2): verified_owner_decision(e) (server-stamped fields);
     e.to_session_id == sid (a broadcast never wakes); e is still
     deliverable() to sid; e.seq of its investigation was never handled for
     sid (no ledger entry and above the never-pruned high-water mark, B3);
     created_at within DECISION_MAX_AGE (older or undated: notified once);
     the caps hold (SESSION_DAILY_CAP claims per session and
     GLOBAL_DAILY_CAP across sessions per rolling day); the session is not
     live; the session is eligible. Live or ineligible: NOTIFIED (a
     notification, never a launch, never reconsidered).

Codex eligibility (§5.3), ALL of: a rollout
~/.codex/sessions/**/rollout-*-<sid>.jsonl (the Codex sandbox cannot write
there); its session_meta (the one whose id is sid) names a CLI originator
(CODEX_ORIGINATORS, an allow-list) and a CLI source (not a subagent); EVERY
turn_context ran workspace-write or read-only, never with network, and could
write only inside the working directory (turn_policy_problem:
sandbox_policy.writable_roots, the file-system entries of
file_system_sandbox_policy / permission_profile, permission_profile.network);
the session id is a UUID; cwd comes from session_meta.cwd (never the
binding), realpath'd before every check and for the launch, strictly inside
$HOME and neither inside nor above HOME_PROTECTED (B2), a git work tree, with
no Codex project config from cwd up to the work-tree root (proof C-8); no
Codex config (~/.codex/config.toml, /etc/codex/*.toml) widens workspace-write
(sandbox_workspace_write, permissions, writable_roots, network_access); and
the MCP servers and plugins can all be named on the command line
(codex_mcp_overrides). Checked again before every launch.

Other runtimes are notify-only. Each is recognized by its own records
(RECORDS, a glob): a Claude transcript (gate C-6 not demonstrated; Remote
Control is the Claude path); a Gemini CLI chat (its launch is built and off,
GEMINI_LAUNCH_PROVEN); Cursor's records (best effort). Records of none or of
several runtimes: unknown, still notified. Notifications name the runtime and
the investigation (its id and the connection's host, never a link the
binding carries). The only runtimes that may launch are LAUNCHERS, an
explicit allow-list (one Launcher per runtime); adding one is that entry,
after its gates are demonstrated. Rollouts are streamed line by line,
bounded (MAX_ROLLOUT_BYTES, MAX_LINE); only session_meta, turn_context and
MCP tool-call records are parsed; nothing of their contents is logged.

Codex launch (config launch true, no ~/.cardinal/wake/disabled/codex):
  <codex> exec -s <the session's sandbox> -c approval_policy="never" -c web_search="disabled"
    -c mcp_servers.cardinal.tools.<each of the two>.approval_mode="approve"
    -c mcp_servers.cardinal.enabled_tools=[<read/post investigation events>]
    -c mcp_servers.<every other server>.enabled=false
    -c plugins."<every non-Cardinal plugin>".enabled=false
    --disable <each of CODEX_DISABLED_FEATURES>
    resume <sid> "<WAKE_PROMPT>"
pinned by golden tests, in the realpath'd cwd; never --profile,
--dangerously-*, --ignore-* or --skip-git-repo-check. Codex starts MCP
servers outside its sandbox and exposes app connectors, web search, image
generation and browser tools that config.toml does not name, so the woken
run gets only the Cardinal server's CARDINAL_WAKE_TOOLS (pre-approved:
approval_policy=never refuses an unapproved MCP call) and those features
off (live proof H1). The same flags are validated first with `<codex>
<flags> features list` (codex_lockdown_problem: exit 0, every feature
listed, not removed, off; cached per binary identity), else a notification:
never a partial lockdown. Environment: the waker's, without
CARDINAL_*, CODEX_HOME or a parent session id, PATH the vetted one.
Detached, output to a 0600 per-wake log (the newest WAKE_LOGS_KEEP kept; a
finished one over MAX_WAKE_LOG keeps its tail). After it exits, everything it
added to the rollout must stay in bounds: each turn the pinned mode, no
network, writes only inside cwd; each tool call in CODEX_ALLOWED_CALLS
(exec, wait, request_user_input_async, clock.sleep, collaboration.*, the two
Cardinal tools). Otherwise (or with no turn recorded) the waker writes
~/.cardinal/wake/disabled/codex with the reason, notifies, and launches no
Codex session until `cardinal-wake reenable codex`. No fallback to other
flags. (A call made from inside Codex's code-mode `exec` tool is not
visible in the rollout; the -c restriction is what bounds it.)

Gemini CLI launch (§5.3 Gemini; built, shipped off until GEMINI_LAUNCH_PROVEN):
  <python> -I allowlist_proxy.py --log <log> --port 8877 <hosts> --
    /usr/bin/sandbox-exec -D TARGET_DIR=<cwd> -D TMP_DIR=<private> -D HOME_DIR
      -D CACHE_DIR -D INCLUDE_DIR_0..4=/dev/null -f <cardinal's restrictive-proxied profile>
    <gemini> --resume <ident> --approval-mode yolo --skip-trust -p "<WAKE_PROMPT>"
Prevention, not detection (G2): the proxy owns port 8877 exclusively (G3)
before it starts the command, and the sandbox wraps the whole Gemini process
from its first instruction; Gemini's own -s is not passed (a seatbelt cannot
be nested) and SANDBOX=sandbox-exec is set, as Gemini sets it inside its own
sandbox, so it never starts another. The profile denies reading .env files
and the OAuth credentials, so only a Gemini API key works (passed in the
environment). Eligible only in an approved root, every recorded turn
sandboxed (agent-writable: a hint), no Gemini configuration that changes the
sandbox (fail closed), port 8877 free, no other Gemini wake in flight. A
started tree that does not show the sandbox within SPAWN_VERIFY_WINDOW is
killed and Gemini launch disabled (defense in depth; a quick exit is judged
after the run).

Lifecycle (§5.4), ledger ~/.cardinal/wake/ledger/<sid>.json keyed by
investigation_id:seq, with a per-investigation high-water mark that is never
pruned; every read-modify-write holds an flock on <sid>.lock, and every
transition is written before the action it describes:
  DECISION_SEEN -> CLAIMED -> RESUMING [-> TIMED_OUT] [-> ACK_RELAYED] -> ACKNOWLEDGED | FAILED; NOTIFIED.
ACK_RELAYED: after the child exited, the waker posted the acknowledgment the
agent wrote to $TMPDIR/ACK_FILE (relay_ack). A claim is refused while the
session has an entry in flight (CLAIMED, RESUMING, TIMED_OUT, ACK_RELAYED).
The claim (with the pre-launch snapshot), the spawn and the RESUMING write
happen under one lock; a CLAIMED entry found later means the waker died
mid-launch: a live process whose argv names the session and this decision's
wake prompt is recovered as the child, else FAILED (never retried, never
"interrupted" while a child runs). A spawn error (no child ever existed) is
retried at most SPAWN_RETRIES times. A child that started is never retried.
A decision posted during a run is delivered inside it by the hooks (Gemini;
a woken Codex run has none); one still unacknowledged after the run (and
after LIVE_WINDOW) is considered again and counts against the caps. The caps' check and the claim
also hold ~/.cardinal/wake/ledger/.global.lock (after the session's lock).

Liveness: live when ANY of: the poller status anchor pid is alive and
started before the status file was written; a process's argv contains the
session id; the rollout or transcript, or the binding's .active file,
changed within LIVE_WINDOW; a child the waker started is alive. A signal
that cannot be read counts as live. Gap (L5): a Codex TUI started without
the session id in its argv (`codex`, then picking the session) and idle for
more than LIVE_WINDOW with no poller running reads as stopped; the waker
then resumes the session beside it. Close the window before posting.

Reporting: a ledger history line per transition and, for NOTIFIED,
RESUMING, TIMED_OUT, ACKNOWLEDGED and FAILED, a local notification (never
the decision's text). Success means the session acknowledged the decision,
with its disposition (accepted / declined / noted / conflicts), by the
author's own principal naming the session (never a grantee, never
granted_by); an agent holding the author's key could still post one, so it
reports what the session said. Launching a process is never success. A
`session.wake` server event is a follow-up (§8).

Standard library only; Python 3.9. Every external effect (clock, process
table, process start times, launcher, notifier, Cardinal reads, launchctl)
is injectable for tests; nothing here starts a real agent unless the
default launcher is used by `run`.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, List, Mapping, Optional, Tuple

from . import investigation_events as ie
from . import investigation_poller as poller
from . import investigation_state_sync as sync
from . import paths
from .owner_input import wake_prompt

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------

CANDIDATE_WINDOW = 7 * 86400.0   # a binding untouched this long is never woken
RECENT_WINDOW = 86400.0          # bindings touched this recently are read every pass ...
OLD_CHECK_EVERY = 300.0          # ... older candidates at most this often
LIVE_WINDOW = 120.0
WAKE_TIMEOUT = 2 * 3600.0
SESSION_DAILY_CAP = 3
GLOBAL_DAILY_CAP = 10
DAY = 86400.0
SPAWN_RETRIES = 2
ACK_CHECK_GRACE = 3600.0         # an exited child whose acknowledgment cannot be read: FAILED after this
RUN_INTERVAL = 45                # launchd StartInterval (seconds)
LEDGER_KEEP = 30 * 86400.0       # terminal entries kept this long (a repeat of their seq never wakes)
LEDGER_MAX_ENTRIES = 200
READ_TIMEOUT = 10.0
READ_BUDGET = 25.0
MAX_ROLLOUT_BYTES = 512 * 1024 * 1024
MAX_LINE = 16 * 1024 * 1024
MAX_LAUNCHD_LOG = 1024 * 1024
DECISION_MAX_AGE = 7 * 86400.0   # an owner decision older than this (created_at) never launches: notified once
WAKE_LOGS_KEEP = 50              # per-wake logs kept (newest); older ones are deleted
MAX_WAKE_LOG = 10 * 1024 * 1024  # a finished wake's log larger than this keeps only its last WAKE_LOG_TAIL bytes
WAKE_LOG_TAIL = 1024 * 1024

PLIST_LABEL = "io.cardinalhq.wake"

# The daemon's own executables (enable resolves and vets them; every run
# checks them again). PATH for the daemon and every woken child: these
# system directories plus the directories of the vetted interpreter and the
# launcher binaries (and node, when one is a node script), never the shell's.
SYSTEM_PATH_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
UNSAFE_TEMP_DIRS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")
# Directories under $HOME a woken Codex session's working directory may be
# neither inside nor above (its workspace-write sandbox could write them).
HOME_PROTECTED = (".cardinal", ".codex", ".claude", ".gemini", ".cursor", "Library", ".ssh", ".config", ".gnupg",
                  ".aws", ".docker", ".kube", ".local", ".zsh", ".oh-my-zsh", ".bash_it", ".npm", ".pyenv",
                  ".nvm", ".cargo", ".rustup", "Library/LaunchAgents")
# Files under $HOME whose real location (through symlinks) a woken
# working directory may not contain: shell and git setup run later,
# unsandboxed (review 3 #3; HOME_PROTECTED's directories are resolved too).
HOME_PROTECTED_FILES = (".zshrc", ".zprofile", ".zshenv", ".zlogin", ".zlogout", ".bashrc", ".bash_profile",
                        ".bash_login", ".profile", ".gitconfig", ".git-credentials", ".netrc", ".npmrc")
# Codex config that would widen a woken session's workspace-write sandbox or
# its network (any of these keys, in any config Codex loads, refuses the launch).
CODEX_CONFIG_FILES = ("/etc/codex/config.toml", "/etc/codex/managed_config.toml")
# Keys may be bare, "basic-quoted" or 'literal-quoted' (L-a); the launch also
# pins network_access / writable_roots on the command line (CODEX_SANDBOX_PINS).
_CODEX_WIDENING_KEYS = r"(?:sandbox_workspace_write|permissions|permission_profiles?|default_permissions)"
_CODEX_WIDENING_RE = re.compile(
    r"^\s*(?:\[\s*\[?\s*[\"']?" + _CODEX_WIDENING_KEYS + r"[\"']?\s*[.\]]"
    r"|\[.*\bsandbox_workspace_write\b"
    r"|[\"']?" + _CODEX_WIDENING_KEYS + r"[\"']?\s*[.=]"
    r"|.*[\"']?\b(?:writable_roots|network_access)\b[\"']?\s*=)")
# The only MCP tool a woken Codex run may call: the Cardinal server's read of
# its Investigation's events. (The gateway's post takes only cues, questions
# and challenges, never an acknowledgment, so it is not offered: the
# acknowledgment is relayed by the waker from ACK_FILE, see read_ack_file.)
# Every other server is disabled and every non-Cardinal plugin is disabled
# for the launch (codex_mcp_overrides); a call outside this list in the
# rollout disables Codex launch (verify_after).
CARDINAL_MCP_SERVER = "cardinal"
CARDINAL_PLUGIN_PREFIX = "cardinal-codex-plugin@"
CARDINAL_WAKE_TOOLS = ("storyboard__read_investigation_events",)
# The ack relay (live N1): with hooks off and no network in the sandbox the
# woken agent cannot acknowledge itself, so it writes its answer to
# $TMPDIR/ACK_FILE (the wake's private 0700 dir); after the child exits the
# waker reads it (no symlink, a regular file, at most MAX_ACK_FILE bytes,
# strict schema, ack_of == the launched seq) and posts that one
# acknowledgment as the session, with the plugin's own connection.
ACK_FILE = "cardinal-wake-ack.json"
MAX_ACK_FILE = 8192
RELAY_DISPOSITIONS = ("accepted", "declined", "noted")
# The lockdown of a woken Codex run beyond MCP (live proof H1, codex-cli
# 0.162.1): Codex exposes ~90 tools that are not in config.toml (ChatGPT app
# connectors, web search, image generation, the browser and computer-use
# REPLs, plugin installs), which per-server / per-plugin -c switches do not
# remove. These features are disabled (--disable, before `resume`) and web
# search is off. code_mode_host is NEVER disabled: every tool, the shell and
# MCP included, runs through functions.exec in this build. multi_agent does
# not remove the collaboration.* tools; a spawned sub-agent inherits the same
# sandbox and tool list (H1), so they are allowed.
#
# hooks (N1): Codex hooks run OUTSIDE the sandbox with cwd = the workspace (a
# user hook like `make`, or a hook launcher resolving into a dev checkout, is
# workspace code run unsandboxed), so a woken run has none. The decision is
# not delivered by a hook: the wake prompt names the investigation and the
# read tool. No evidence capture, telemetry, owner-input capture or Stop
# reminders during a woken run.
CODEX_DISABLED_FEATURES = ("apps", "plugins", "remote_plugin", "image_generation", "computer_use", "browser_use",
                           "browser_use_external", "in_app_browser", "multi_agent", "tool_suggest",
                           "skill_mcp_dependency_install", "hooks", "daemon_auto_start")
CODEX_WEB_SEARCH_OFF = 'web_search="disabled"'
# Review 3 #1 (live C-20, codex-cli 0.162.1): a config `notify = [...]`
# program runs OUTSIDE the sandbox at every turn end, with the turn's JSON
# (agent-influenced) as an argument, even with hooks disabled; `-c notify=[]`
# after it overrides it (proven live: the probe file is not created). Always
# passed, as the last -c, so it is the value that wins.
CODEX_NOTIFY_OFF = "notify=[]"
# Pinned on the command line whatever the config says (L-a, L-b): no
# network, no extra writable roots, never the shared /tmp; $TMPDIR stays
# writable and is this wake's private 0700 dir under ~/.cardinal/wake/tmp.
# Codex 0.162.1 silently accepts unknown keys here, so the post-run check
# reads them back from the woken turns' sandbox_policy (it records
# network_access, writable_roots, exclude_slash_tmp, exclude_tmpdir_env_var).
CODEX_SANDBOX_PINS = ("sandbox_workspace_write.network_access=false", "sandbox_workspace_write.writable_roots=[]",
                      "sandbox_workspace_write.exclude_slash_tmp=true",
                      "sandbox_workspace_write.exclude_tmpdir_env_var=false")
# N4: the features left ON under the full lockdown on the vetted codex-cli
# 0.162.1 (live proof H1: with these on, the woken run's tools were exactly
# the allow-listed set). A feature on under the lockdown that is not in this
# set (a new default in another Codex) refuses the launch until reviewed.
CODEX_TESTED_ON = frozenset((
    "api_key_model_discovery", "auth_elicitation", "browser_annotation_api", "browser_use_full_cdp_access",
    "code_mode_host", "collaboration_modes", "compaction_image_budget", "content_item_kinds",
    "enable_request_compression", "fast_mode", "goals", "guardian_approval", "guardian_reuse_parent_compaction",
    "in_app_chat", "in_app_dictation", "in_app_local_automation", "in_app_updates", "in_app_voice",
    "instant_interrupt", "item_ids", "mentions_v2", "plugin_sharing", "realtime_conversation", "resize_all_images",
    "shell_snapshot", "shell_tool", "skill_search", "sleep_tool", "sqlite", "steer", "system_proxy_fallback",
    "terminal_resize_reflow", "tool_call_mcp_elicitation", "tool_search_always_defer_mcp_tools", "tui_app_server",
    "ultrafast_mode", "unbounded_connection_retries", "unified_exec", "unified_exec_tty", "unified_exec_zsh_fork",
    "view_image", "workspace_dependencies", "worktrees", "write_stdin_approval"))
# N2: version-manager shims pick the real binary from files in the cwd
# (.tool-versions, .nvmrc, package.json "volta"), which an agent can write;
# none of the daemon's executables may resolve through them.
SHIM_DIRS = (".asdf", ".local/share/mise", ".mise", ".nodenv", ".volta", ".nvm", ".pyenv", ".rbenv", ".fnm",
             ".local/share/fnm", ".proto", ".local/share/aquaproj-aqua")
SHIM_BINARIES = frozenset(("volta-shim", "mise", "asdf", "proto-shim", "rtx"))
# Mach-O (32/64-bit, both byte orders; universal) and ELF (Linux, where the
# tests run; macOS cannot execute it, so it gains nothing there).
_NATIVE_MAGICS = frozenset((b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
                            b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\x7fELF"))
# N3: the only variables a woken run (and the codex probe) inherits; the rest
# (NODE_OPTIONS, NODE_PATH, DYLD_*, *_BASE_URL, proxies, CARDINAL_*...) never.
CHILD_ENV_KEEP = ("USER", "LOGNAME", "LANG", "SHELL")
CODEX_AUTH_ENV = ("OPENAI_API_KEY",)
# The only tool calls a woken run's rollout may hold (response_item calls:
# (type, namespace, name); namespace None also matches "functions"). Calls
# made inside functions.exec (apply_patch, exec_command, the Cardinal MCP
# tools...) are not rollout calls; the lockdown is what bounds them.
CODEX_ALLOWED_CALLS = frozenset(
    [("custom_tool_call", None, "exec"), ("function_call", None, "exec"), ("function_call", None, "wait"),
     ("function_call", None, "request_user_input_async"), ("function_call", "clock", "sleep")]
    + [("function_call", "collaboration", t) for t in ("followup_task", "interrupt_agent", "list_agents",
                                                       "send_message", "spawn_agent", "wait_agent")]
    + [("function_call", "mcp__" + CARDINAL_MCP_SERVER, t) for t in CARDINAL_WAKE_TOOLS])
CODEX_PROBE_TIMEOUT = 30.0
_TOML_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")   # fullmatch
_TOML_QUOTED_ID_RE = re.compile(r"[A-Za-z0-9_.@:/ -]{1,128}")   # fullmatch (the inside of a "quoted" key)

DECISION_SEEN = "DECISION_SEEN"
CLAIMED = "CLAIMED"
RESUMING = "RESUMING"
TIMED_OUT = "TIMED_OUT"          # still running past WAKE_TIMEOUT: left running, settled when it exits
ACK_RELAYED = "ACK_RELAYED"      # the child exited; the waker posted the acknowledgment it wrote (ACK_FILE)
ACKNOWLEDGED = "ACKNOWLEDGED"
FAILED = "FAILED"
NOTIFIED = "NOTIFIED"
IN_FLIGHT = (CLAIMED, RESUMING, TIMED_OUT, ACK_RELAYED)
TERMINAL = (ACKNOWLEDGED, FAILED, NOTIFIED)
NOTIFY_STATES = (NOTIFIED, RESUMING, TIMED_OUT, ACKNOWLEDGED, FAILED)

CODEX = "codex"
CLAUDE = "claude"
CURSOR = "cursor"
GEMINI = "gemini"
UNKNOWN = "unknown"
RUNTIMES = (CODEX, CLAUDE, CURSOR, GEMINI)   # every adapter with the Investigation layer (all notify)
LABELS = {CODEX: "Codex", CLAUDE: "Claude Code", CURSOR: "Cursor", GEMINI: "Gemini CLI", UNKNOWN: "Unknown-runtime"}
# Why a runtime without a launcher (LAUNCHERS, below) is notify-only.
NOTIFY_ONLY_REASONS = {
    CLAUDE: "Claude Code sessions are notify-only: a resumed claude -p runs with your own settings, so its "
            "sandbox is not guaranteed",
    CURSOR: "Cursor sessions are notify-only: no unattended resume has been shown to pass the wake gates",
    # Codex launch is built and shipped off (CODEX_LAUNCH_PROVEN): its lockdown and sandbox gates
    # passed, but the survivor check (gate 3) could not be verified live in this session (§6 C-23,
    # C-24). An owner turns it on by flipping the constant in a reviewed change.
    CODEX: "Codex sessions are notify-only: unattended launch is pending live verification of survivor "
           "detection (the TMPDIR writable-root probe against a real Codex seatbelt and an env-cleared "
           "survivor; see docs/specs/resume-and-wake.md §6 C-23/C-24)",
    # Proof C-7 (2026-10-12, gemini-cli 0.63.0): its sandbox wraps the whole
    # CLI, hooks included, and every bundled macOS profile allows network.
    GEMINI: "Gemini CLI sessions are notify-only until its sandboxed launch (restrictive-proxied seatbelt behind "
            "an allow-list proxy) passes its live proofs",
}
SANDBOX_MODES = ("workspace-write", "read-only")
# Originators of Codex's own CLI (codex exec, the TUI). An allow-list: the
# desktop app ("Codex Desktop") and anything unrecognized are notify-only.
CODEX_ORIGINATORS = frozenset({"codex_exec", "codex_cli_rs"})
CODEX_SOURCES = frozenset({"exec", "cli"})
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")  # fullmatch
APPROVAL_NEVER = 'approval_policy="never"'
_LINK_RE = re.compile(r"https://[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?/[A-Za-z0-9._~/%?=&#:+-]{0,300}")  # fullmatch

PS = "/bin/ps"
OSASCRIPT = "/usr/bin/osascript"
LAUNCHCTL = "/bin/launchctl"

NOTIFY_ONLY_TEXT = (
    "Notify-only (a notification, never a launch): Claude Code sessions (a resumed `claude -p` runs with your "
    "own settings, so its sandbox cannot be guaranteed; use `claude --remote-control` instead), Cursor and "
    "Gemini CLI sessions, sessions whose runtime is not recognized, Codex desktop app sessions, any session that "
    "ever ran danger-full-access or with network access, and a session whose window is still open.")
LAUNCH_TEXT = (
    "Can launch: Codex CLI sessions (codex / codex exec) that only ever ran -s workspace-write or -s read-only, "
    "resumed in that same sandbox with approvals off (`codex exec -s <mode> -c approval_policy=\"never\" "
    "resume <session>`).")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def wake_dir(home: Path) -> Path:
    return home / ".cardinal" / "wake"


def config_path(home: Path) -> Path:
    return wake_dir(home) / "config.json"


def ledger_dir(home: Path) -> Path:
    return wake_dir(home) / "ledger"


def ledger_path(home: Path, sid: str) -> Path:
    if not ie.valid_session(sid):
        raise ValueError(f"not a session id: {sid!r}")
    return ledger_dir(home) / f"{sid}.json"


def disabled_path(home: Path, runtime: str) -> Path:
    if runtime not in RUNTIMES:
        raise ValueError(f"not a runtime: {runtime!r}")
    return wake_dir(home) / "disabled" / runtime


def logs_dir(home: Path) -> Path:
    return wake_dir(home) / "logs"


def lib_dir(home: Path) -> Path:
    return wake_dir(home) / "lib"


def poll_state_path(home: Path) -> Path:
    return wake_dir(home) / "poll.json"


def plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return path


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


@contextlib.contextmanager
def _locked(path: Path, wait: float) -> Iterator[bool]:
    """flock on `path` (created 0600); yields whether it was acquired within
    `wait` seconds (investigation_events.locked's pattern)."""
    if fcntl is None:  # pragma: no cover
        yield True
        return
    _private_dir(path.parent)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    got = False
    try:
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        yield got
    finally:
        if got:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def session_lock(home: Path, sid: str, wait: float = 5.0):
    ledger_path(home, sid)  # validates sid
    return _locked(ledger_dir(home) / f"{sid}.lock", wait)


def global_lock(home: Path, wait: float = 10.0):
    return _locked(ledger_dir(home) / ".global.lock", wait)


# ---------------------------------------------------------------------------
# Config, disabled runtimes
# ---------------------------------------------------------------------------

def read_config(home: Path) -> dict:
    c = _read_json(config_path(home))
    roots = c.get("roots") if isinstance(c.get("roots"), list) else []
    python, path_env = c.get("python"), c.get("path_env")
    binaries = c.get("binaries") if isinstance(c.get("binaries"), dict) else {}
    out = {"enabled": c.get("enabled") is True, "launch": c.get("launch") is True,
           "enabled_at": c.get("enabled_at"),
           "relay_notes": c.get("relay_notes") is True,
           "python": python if isinstance(python, str) and os.path.isabs(python) else None,
           "path_env": path_env if isinstance(path_env, str) and path_env else None,
           "binaries": {k: v for k, v in binaries.items() if isinstance(k, str) and os.path.isabs(k)
                        and isinstance(v, list)},
           "node_path": c.get("node_path") if isinstance(c.get("node_path"), str) else None,
           "versions": c.get("versions") if isinstance(c.get("versions"), dict) else {},
           "roots": [r for r in roots if isinstance(r, str) and os.path.isabs(r)]}
    for launcher in LAUNCHERS.values():   # <runtime>_path: an absolute executable, or None
        exe = c.get(launcher.executable)
        out[launcher.executable] = exe if isinstance(exe, str) and os.path.isabs(exe) else None
    return out


def write_config(home: Path, config: dict) -> None:
    _private_dir(wake_dir(home))
    ie.write_json(config_path(home), config)


def disabled_reason(home: Path, runtime: str) -> Optional[str]:
    """Why launching `runtime` is disabled (its marker's reason), or None."""
    p = disabled_path(home, runtime)
    if not p.exists() and not p.is_symlink():
        return None
    reason = _read_json(p).get("reason")
    return reason if isinstance(reason, str) and reason else "disabled (unreadable marker)"


def disable_runtime(home: Path, runtime: str, reason: str, now: float) -> None:
    p = disabled_path(home, runtime)
    _private_dir(p.parent)
    ie.write_json(p, {"reason": reason, "at": _iso(now)})


# ---------------------------------------------------------------------------
# The daemon's executables (B1: never from an agent-writable place)
# ---------------------------------------------------------------------------

def temp_dirs() -> Tuple[str, ...]:
    """Directories any sandboxed agent may write (temp): no executable the
    daemon runs may live there."""
    import tempfile
    return tuple(dict.fromkeys(list(UNSAFE_TEMP_DIRS) + [os.path.realpath(tempfile.gettempdir())]))


def unsafe_location(path: str, home: Path, roots: List[str], *, allow_home: bool = True) -> Optional[str]:
    """Why the daemon must not run `path` (resolved), or None: inside a git
    work tree under $HOME (where every wakeable workspace lives: a Codex
    session's cwd must be strictly inside $HOME, approved roots are too),
    an approved root, a temp directory, or (allow_home False) anywhere under
    $HOME. A git tree outside $HOME is not refused: Homebrew's own prefix
    (/opt/homebrew) is one, and no wake ever runs an agent there."""
    real = os.path.realpath(path)
    home_real = os.path.realpath(str(home))
    git = _git_root(os.path.dirname(real))
    if git is not None and _within(os.path.realpath(str(git)), home_real):
        return f"{real} is inside a git work tree"
    for root in roots:
        if _within(real, os.path.realpath(root)):
            return f"{real} is inside the approved root {root}"
    for t in temp_dirs():
        if _within(real, t):
            return f"{real} is inside a temporary directory"
    if not allow_home and _within(real, os.path.realpath(str(home))):
        return f"{real} is inside your home directory"
    return None


def fingerprint(path: str) -> Optional[list]:
    """[size, mtime_ns, inode, ctime_ns, sha256] of the file `path` resolves
    to, or None (N5: size / mtime / inode alone survive an in-place rewrite
    with the mtime set back; ctime and the content hash do not)."""
    import hashlib
    try:
        st = os.stat(path)
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns, h.hexdigest()]


def is_native_executable(path: str) -> bool:
    """`path` is a native executable (Mach-O, thin or universal; ELF where
    tests run), never a script that picks or spawns another binary."""
    try:
        with open(path, "rb") as f:
            return f.read(4) in _NATIVE_MAGICS
    except OSError:
        return False


def shim_problem(path: str, home: Path) -> Optional[str]:
    """Why `path` (as found, or resolved) is a version-manager shim, or None."""
    home_real = os.path.realpath(str(home))
    for p in (os.path.abspath(path), os.path.realpath(path)):
        for d in SHIM_DIRS:
            if _within(p, os.path.join(home_real, d)) or _within(p, os.path.join(str(home), d)):
                return f"{p} is a version-manager shim (~/{d}), which picks its target from files in the cwd"
    if os.path.basename(os.path.realpath(path)) in SHIM_BINARIES:
        return f"{os.path.realpath(path)} is a version-manager shim"
    return None


def npm_codex_native(script: str) -> Optional[str]:
    """The Mach-O an npm install's codex.js would spawn, resolved without
    running it (…/@openai/codex/bin/codex.js -> …/vendor/<triple>/codex/codex,
    or the per-platform optional package's), or None."""
    import platform
    p = Path(os.path.realpath(script))
    if p.name != "codex.js" or p.parent.name != "bin":
        return None
    pkg = p.parent.parent
    if _read_json(pkg / "package.json").get("name") != "@openai/codex":
        return None
    triples = ["aarch64-apple-darwin", "x86_64-apple-darwin"]
    if platform.machine() != "arm64":
        triples.reverse()
    candidates = []
    for triple in triples:
        arch = "arm64" if triple.startswith("aarch64") else "x64"
        candidates += [pkg / "vendor" / triple / "codex" / "codex",
                       pkg / "node_modules" / "@openai" / f"codex-darwin-{arch}" / "vendor" / triple / "codex"
                       / "codex"]
    for c in candidates:
        if c.is_file() and is_native_executable(str(c)):
            return os.path.realpath(str(c))
    return None


def needs_node(path: str) -> bool:
    """`path` is a script run by node (`#!/usr/bin/env node` and the like)."""
    try:
        with open(path, "rb") as f:
            head = f.readline(256)
    except OSError:
        return False
    return head.startswith(b"#!") and b"node" in head


def system_python() -> Optional[str]:
    """The real interpreter behind the /usr/bin/python3 xcrun shim (`xcrun
    -f python3` with a clean environment: no DEVELOPER_DIR or SDKROOT),
    or None. The shim itself picks its target from the environment, so the
    daemon never runs it (review 3 #7)."""
    try:
        r = subprocess.run(["/usr/bin/xcrun", "-f", "python3"], capture_output=True, text=True, timeout=10,
                           check=False, env={"PATH": "/usr/bin:/bin", "HOME": "/var/empty"})
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and os.path.isabs(out) else None


def pick_python(home: Path, roots: List[str]) -> Tuple[Optional[str], str]:
    """(the interpreter the daemon runs, with -I -S) or (None, why none is
    safe): this Python if it resolves outside $HOME, git work trees,
    approved roots, temp dirs and shims, else the real interpreter behind
    /usr/bin/python3 (system_python). Never a binary in the system PATH
    directories (/usr/bin/python3 is an xcrun shim)."""
    reasons = []
    for candidate in (sys.executable, system_python()):
        if not candidate or not os.access(candidate, os.X_OK):
            continue
        real = os.path.realpath(candidate)
        why = (unsafe_location(candidate, home, roots, allow_home=False) or shim_problem(candidate, home)
               or (f"{real} is a system shim, not the interpreter" if os.path.dirname(real) in SYSTEM_PATH_DIRS
                   else None)
               or (None if is_native_executable(real)
                   else f"{candidate} is not a native (Mach-O) executable"))
        if why is None:
            return os.path.realpath(candidate), ""
        reasons.append(why)
    return None, "; ".join(reasons) or "no python3 found"


def daemon_path(*dirs: Optional[str]) -> str:
    """PATH for the daemon and its children: the system directories plus
    the given (vetted) directories, nothing from the user's shell."""
    return ":".join(dict.fromkeys([*SYSTEM_PATH_DIRS, *[d for d in dirs if d]]))


def binaries_problem(config: dict, home: Path, cwd: Optional[str] = None) -> Optional[str]:
    """Why the vetted executables (the interpreter, the launch binaries,
    node) cannot be trusted now, or None: each must still be the file enable
    vetted (same path, size, mtime, inode, ctime, sha256), outside every
    unsafe location and version-manager shim, the interpreter and codex
    Mach-O executables (never a script that picks or spawns another binary),
    and outside the woken session's working directory."""
    binaries = config.get("binaries") if isinstance(config.get("binaries"), dict) else {}
    if not binaries or not config.get("python") or not config.get("path_env"):
        return "the daemon's executables were not vetted (run cardinal-wake enable --launch again)"
    roots = config.get("roots") or []
    for path, fp in binaries.items():
        if fingerprint(path) != fp or os.path.realpath(path) != path:
            return f"{path} changed since cardinal-wake enable (run it again)"
        why = unsafe_location(path, home, roots, allow_home=(path != config.get("python"))) \
            or shim_problem(path, home)
        if why:
            return why
        native = (config.get("python"), config.get("codex_path"), config.get("node_path"))
        if path in native and not is_native_executable(path):
            return f"{path} is not a native (Mach-O) executable"
        if path == config.get("python") and os.path.dirname(path) in SYSTEM_PATH_DIRS:
            return f"{path} is a system shim, not the interpreter (run cardinal-wake enable again)"
        if cwd and _within(path, os.path.realpath(cwd)):
            return f"{path} is inside the session's working directory"
    return None


def reenable_runtime(home: Path, runtime: str) -> bool:
    try:
        disabled_path(home, runtime).unlink()
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def read_ledger(home: Path, sid: str) -> dict:
    data = _read_json(ledger_path(home, sid))
    entries = data.get("entries")
    data["session_id"] = sid
    data["entries"] = {k: v for k, v in entries.items() if isinstance(v, dict)} if isinstance(entries, dict) else {}
    hwm = data.get("hwm")
    data["hwm"] = {k: v for k, v in hwm.items() if ie.valid_investigation(k) and isinstance(v, int)
                   and not isinstance(v, bool)} if isinstance(hwm, dict) else {}
    pr = data.get("probe_retries")
    data["probe_retries"] = {k: v for k, v in pr.items() if isinstance(k, str) and isinstance(v, int)
                             and not isinstance(v, bool) and v > 0} if isinstance(pr, dict) else {}
    return data


def entry_key(investigation_id: str, seq: int) -> str:
    """A ledger entry's key: seqs are per investigation, so the key is both."""
    return f"{investigation_id}:{seq}"


def decision_seen(ledger: dict, investigation_id: str, seq: int) -> bool:
    """#seq of `investigation_id` was handled for this session: it has an
    entry, or it is at or below the session's high-water mark for that
    investigation (never pruned: a pruned entry cannot let a decision run
    again)."""
    return entry_key(investigation_id, seq) in ledger["entries"] or seq <= ledger["hwm"].get(investigation_id, 0)


def add_entry(ledger: dict, entry: dict) -> None:
    """Record a new entry and raise the investigation's high-water mark."""
    inv, seq = entry["investigation_id"], entry["seq"]
    ledger["entries"][entry_key(inv, seq)] = entry
    ledger["hwm"][inv] = max(ledger["hwm"].get(inv, 0), seq)


def write_ledger(home: Path, sid: str, ledger: dict, now: float) -> None:
    """Persist the ledger; terminal entries older than LEDGER_KEEP (or past
    LEDGER_MAX_ENTRIES) go, the high-water marks never do."""
    entries = ledger["entries"]
    for k in [k for k, e in entries.items()
              if e.get("state") in TERMINAL and now - (_num(e.get("updated_at")) or 0.0) > LEDGER_KEEP]:
        del entries[k]
    if len(entries) > LEDGER_MAX_ENTRIES:
        terminal = sorted((k for k, e in entries.items() if e.get("state") in TERMINAL),
                          key=lambda k: _num(entries[k].get("updated_at")) or 0.0)
        for k in terminal[:len(entries) - LEDGER_MAX_ENTRIES]:
            del entries[k]
    _private_dir(ledger_dir(home))
    ie.write_json(ledger_path(home, sid), ledger)


def transition(entry: dict, state: str, now: float, note: str = "") -> dict:
    """Move `entry` to `state`, appending one history line (never decision
    text: `note` is the waker's own words)."""
    entry["state"] = state
    entry["updated_at"] = now
    line = {"state": state, "at": _iso(now)}
    if note:
        line["note"] = note
        entry["reason"] = note
    entry.setdefault("history", []).append(line)
    if state in TERMINAL:
        entry["finished_at"] = now
    return entry


def ledger_sids(home: Path) -> List[str]:
    try:
        names = os.listdir(str(ledger_dir(home)))
    except OSError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json") and ie.valid_session(n[:-5]))


def _claims(ledger: dict, now: float) -> int:
    return sum(1 for e in ledger["entries"].values()
               if (_num(e.get("claimed_at")) is not None and 0 <= now - e["claimed_at"] < DAY))


def global_claims(home: Path, now: float) -> int:
    return sum(_claims(read_ledger(home, sid), now) for sid in ledger_sids(home))


def in_flight(ledger: dict) -> List[dict]:
    return [e for e in ledger["entries"].values() if e.get("state") in IN_FLIGHT]


def runtime_in_flight(home: Path, runtime: str) -> bool:
    """Any session's ledger holds an in-flight launch of `runtime`."""
    return any(e.get("runtime") == runtime for sid in ledger_sids(home) for e in in_flight(read_ledger(home, sid)))


def _last_finished(ledger: dict) -> float:
    return max((_num(e.get("finished_at")) or 0.0 for e in ledger["entries"].values()
                if e.get("child")), default=0.0)


# ---------------------------------------------------------------------------
# Processes, notifications, launch (the defaults System uses)
# ---------------------------------------------------------------------------

def process_start(pid: int) -> Optional[float]:
    """When process `pid` started (epoch seconds, 1 s resolution), or None."""
    if not isinstance(pid, int) or pid <= 1:
        return None
    try:
        out = subprocess.run([PS, "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=3,
                             check=False, env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"}).stdout.strip()
        return time.mktime(time.strptime(" ".join(out.split()), "%a %b %d %H:%M:%S %Y"))
    except (OSError, ValueError, OverflowError, subprocess.SubprocessError):
        return None


def process_table() -> Optional[List[Tuple[int, str]]]:
    """(pid, argv as one string) for every process, or None when ps fails."""
    try:
        r = subprocess.run([PS, "-A", "-ww", "-o", "pid=,args="], capture_output=True, text=True, timeout=5,
                           check=False, env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"})
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    rows = []
    for line in r.stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        with contextlib.suppress(ValueError):
            rows.append((int(pid), args))
    return rows


def process_rows() -> Optional[List[Tuple[int, int, str]]]:
    """(pid, ppid, argv as one string) for every process, or None."""
    try:
        r = subprocess.run([PS, "-A", "-ww", "-o", "pid=,ppid=,args="], capture_output=True, text=True, timeout=5,
                           check=False, env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"})
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    rows = []
    for line in r.stdout.splitlines():
        fields = line.split(None, 2)
        with contextlib.suppress(ValueError, IndexError):
            rows.append((int(fields[0]), int(fields[1]), fields[2] if len(fields) > 2 else ""))
    return rows


# Identifying a woken run's processes by the sandbox itself (review 6).
# Everything a woken Codex run starts runs in the same Seatbelt sandbox,
# which has this wake's private TMPDIR as a writable root; the process
# cannot leave the sandbox, and nothing the agent does (clearing TMPDIR,
# setsid, chdir, re-exec) removes that writable root. So a process is this
# wake's when it is sandboxed AND the sandbox allows it to write the wake's
# TMPDIR directory. macOS answers both through sandbox_check (libsystem_sandbox):
#   sandbox_check(pid, NULL, 0) -> 1 sandboxed, 0 not;
#   sandbox_check(pid, "file-write-create", SANDBOX_FILTER_PATH|NO_REPORT, dir)
#     -> 0 allowed, 1 denied. The path must EXIST and be the directory itself
#     (a missing path answers 1 for everyone), and an unsandboxed process is
#     allowed everything (hence the NULL check first). The path argument is
#     variadic: on arm64 the C ABI passes it on the stack, so ctypes must pad
#     x0..x7 with five extra register args (verified live, a/b matrix).
_MAC = None   # (check_null, check_path), or False when the calls cannot be loaded
_SANDBOX_FILTER_PATH = 1
_SANDBOX_CHECK_NO_REPORT = 0x40000000
_SANDBOX_WRITE_OP = b"file-write-create"


def _mac_calls():
    global _MAC
    if _MAC is None:
        _MAC = False
        try:
            import ctypes
            import platform
            sb = ctypes.CDLL("/usr/lib/system/libsystem_sandbox.dylib", use_errno=True)
            addr = ctypes.cast(sb.sandbox_check, ctypes.c_void_p).value
            check_null = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int)(addr)
            if platform.machine() == "arm64":
                proto = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                                         *([ctypes.c_void_p] * 5), ctypes.c_char_p)(addr)
                def check_path(pid, op, t, p):   # noqa: E306  (the first variadic arg spills to the stack)
                    return proto(pid, op, t, None, None, None, None, None, p)
            else:
                proto = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                                         ctypes.c_char_p)(addr)
                def check_path(pid, op, t, p):   # noqa: E306
                    return proto(pid, op, t, p)
            _MAC = (check_null, check_path)
        except (OSError, AttributeError, ValueError):
            _MAC = False
    return _MAC or None


def _sandbox_allows(pid: int, directory: str, check_path) -> Optional[bool]:
    """True when `pid`'s sandbox allows file-write-create in `directory`
    (which must exist), False when it denies it, None when the answer is
    neither the documented 0/1 (other than the process having exited:
    False)."""
    import ctypes
    import errno
    ctypes.set_errno(0)
    r = check_path(pid, _SANDBOX_WRITE_OP, _SANDBOX_FILTER_PATH | _SANDBOX_CHECK_NO_REPORT, directory.encode())
    e = ctypes.get_errno()
    if e == errno.ESRCH:
        return False   # the process exited: not ours
    if r == 0 and not e:
        return True
    if r == 1 and not e:
        return False
    return None


def wake_owned(pid: int, tmp_dirs: List[str], canary: str, calls) -> Optional[bool]:
    """True when `pid` is a woken run's process: it is sandboxed, its sandbox
    allows writing one of `tmp_dirs` (existing wake TMPDIR directories), AND
    its sandbox DENIES writing `canary` (the parent `~/.cardinal/wake/tmp`,
    which no Codex wake sandbox ever grants: its writable roots are the cwd
    and the TMPDIR only). The canary is what rules out an unrelated sandboxed
    process whose profile allows writing broadly under $HOME (several Apple
    agents do, review 7 F1): it is allowed on the TMPDIR too, but also on the
    canary, so it is not ours. False when it is not this wake's. None when it
    cannot be told (a sandbox_check answers neither 0 nor 1, or the process
    exited mid-check): the caller fails closed. An unsandboxed process
    (editor, terminal) answers 0 to the NULL check and is False."""
    import ctypes
    import errno
    check_null, check_path = calls
    ctypes.set_errno(0)
    boxed = check_null(pid, None, 0)
    err = ctypes.get_errno()
    if err == errno.ESRCH:
        return False
    if boxed == 0 and not err:
        return False
    if boxed != 1 or err:
        return None
    granted = False
    for d in tmp_dirs:
        allowed = _sandbox_allows(pid, d, check_path)
        if allowed is None:
            return None
        if allowed:
            granted = True
            break
    if not granted:
        return False
    denied_canary = _sandbox_allows(pid, canary, check_path)
    if denied_canary is None:
        return None
    return denied_canary is False


def _etime_seconds(text: str) -> Optional[int]:
    """`ps -o etime` ([[dd-]hh:]mm:ss) in seconds, or None."""
    m = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)", text.strip())
    if not m:
        return None
    d, h, mi, se = (int(x) if x else 0 for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + se


def sandboxed_procs(since: float, tmp_dirs: List[str], canary: str) -> Optional[List[int]]:
    """The user's own processes started at or after `since` (epoch) that are
    a woken run's by the sandbox test (wake_owned over `tmp_dirs`, the
    existing wake TMPDIR directories, with `canary` the parent
    `~/.cardinal/wake/tmp` that no wake sandbox grants). Neither the
    environment nor the process group can hide one, and an unrelated
    sandboxed process — an Apple agent whose profile allows writing broadly
    under $HOME, the owner's own interactive Codex in another repo, an
    editor — is not counted, because it is either unsandboxed or also
    allowed on the canary. None when it cannot be determined (not macOS, the
    calls cannot be loaded, the canary directory is missing, or a process
    cannot be told either way): the caller fails closed. `tmp_dirs` that do
    not exist are dropped (a missing path answers denied for everyone, so it
    could only miss, never misidentify)."""
    if sys.platform != "darwin":
        return None
    calls = _mac_calls()
    if calls is None:
        return None
    dirs = [os.path.realpath(d) for d in tmp_dirs if isinstance(d, str) and d and os.path.isdir(d)]
    if not dirs:
        return []
    # The canary must exist: a missing path answers "denied" for everyone, which would make every
    # candidate look like ours. Fail closed if it is not a directory.
    if not (isinstance(canary, str) and canary and os.path.isdir(canary)):
        return None
    canary = os.path.realpath(canary)
    try:
        r = subprocess.run([PS, "-A", "-o", "pid=,uid=,etime="], capture_output=True, text=True, timeout=10,
                           check=False, env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"})
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    now, uid, me = time.time(), os.getuid(), os.getpid()
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, puid, age = int(parts[0]), int(parts[1]), _etime_seconds(parts[2])
        if puid != uid or pid in (me, 0, 1):
            continue
        if age is None:
            return None
        if now - age < since - 2.0:
            continue
        owned = wake_owned(pid, dirs, canary, calls)
        if owned is None:
            return None
        if owned:
            out.append(pid)
    return out


def wake_tmp_root(home: Path) -> Path:
    """The parent of every wake's private TMPDIR, `~/.cardinal/wake/tmp`: the
    survivor probe's canary (no Codex wake sandbox grants writes here, only
    the per-wake subdirectory below it)."""
    return wake_dir(home) / "tmp"


def port_busy(port: int) -> bool:
    """Something on this machine accepts connections on `port` (IPv4 or IPv6
    loopback)."""
    for family, addr in ((socket.AF_INET, ("127.0.0.1", port)), (socket.AF_INET6, ("::1", port))):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex(addr) == 0:
                    return True
        except OSError:
            continue
    return False


def notify_macos(title: str, message: str) -> None:
    """A macOS notification; the text goes in as osascript arguments, never
    into the script, so nothing in it can be evaluated."""
    if sys.platform != "darwin":
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([OSASCRIPT, "-e", "on run argv", "-e",
                        "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run",
                        title, message], capture_output=True, timeout=10, check=False)


def launch_detached(argv: List[str], cwd: str, env: Mapping[str, str], log_path: Path) -> int:
    """Start `argv` in its own session, stdin /dev/null, stdout and stderr to
    `log_path` (0600). Returns the pid; raises OSError when no process could
    be created (a spawn error)."""
    _private_dir(log_path.parent)
    fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=fd, stderr=fd,
                             start_new_session=True, close_fds=True)
    finally:
        os.close(fd)
    _CHILDREN.append(p)
    return p.pid


_CHILDREN: list = []   # this process's launches: kept so they are reaped, never killed


def reap() -> None:
    """Collect exited children (a long-running `run` would otherwise keep
    them as zombies, which still answer kill(pid, 0))."""
    for p in list(_CHILDREN):
        with contextlib.suppress(OSError):
            if p.poll() is None:
                continue
        _CHILDREN.remove(p)


def claude_connection(home: Path) -> dict:
    """The Claude plugin's connection: CARDINAL_MCP_URL / CARDINAL_MCP_API_KEY
    in ~/.claude/settings.json `env` (where /cardinal:connect writes them);
    nothing after /cardinal:disconnect (~/.claude/cardinal-disconnected)."""
    from .evidence_promote import connection_for
    if (home / ".claude" / "cardinal-disconnected").exists():
        return {}
    env = _read_json(home / ".claude" / "settings.json").get("env")
    if not isinstance(env, dict):
        return {}
    try:
        return connection_for(env.get("CARDINAL_MCP_URL"), env.get("CARDINAL_MCP_API_KEY")) or {}
    except Exception:
        return {}


def agent_connection(runtime: str, home: Path) -> dict:
    """A plugin's connection from its managed files under ~/.<runtime>
    (cardinal.json mcp_url + cardinal-secrets.json mcp_api_key; Codex,
    Cursor and Gemini CLI), never the environment."""
    from .evidence_promote import agent_paths_connection
    from .paths import AgentPaths
    try:
        return agent_paths_connection(AgentPaths(home=home / f".{runtime}"))(home, {}) or {}
    except Exception:
        return {}


def runtime_connection(runtime: str, home: Path) -> dict:
    if runtime == CLAUDE:
        return claude_connection(home)
    return agent_connection(runtime, home) if runtime in RUNTIMES else {}


def fetch_events(conn: dict, investigation_id: str, sid: str, after: int, client: str,
                 opener=None) -> Tuple[list, bool]:
    """(events after `after` addressed to `sid` or to everyone, whether the
    read reached the head). Raises on a failed read."""
    events, cursor, head = ie.read_all(conn, investigation_id, after=after, to_session_id=sid, client=client,
                                       opener=opener, deadline=time.monotonic() + READ_BUDGET,
                                       timeout=READ_TIMEOUT, limit=ie.PAGE_LIMIT)
    return events, cursor >= head


def post_ack_event(conn: dict, investigation_id: str, sid: str, seq: int, disposition: str, note: Optional[str],
                   client: str, opener=None) -> dict:
    """Post the relayed acknowledgment of #seq as session `sid`, exactly as
    the plugin's `cardinal-storyboard investigation ack` does (ack_payload,
    the ack:<sid>:<seq> idempotency key, session_id, the plugin's client).
    Never conflicts_with. An idempotency_conflict (the session already
    acknowledged #seq some other way) is not an error: the acknowledgment
    exists, and the read that follows reports its disposition."""
    payload = ie.ack_payload(seq, disposition, note)
    try:
        return ie.append_event(conn, investigation_id, ie.ACKNOWLEDGED, payload, idempotency_key=ie.ack_key(sid, seq),
                               client=client, session_id=sid, producer_client=client, opener=opener)
    except sync.ServerError as e:
        if isinstance(getattr(e, "body", None), dict) and e.body.get("error") == "idempotency_conflict":
            return {"duplicate": True}
        raise


WAKE_REPORT_TIMEOUT = 10.0


def post_wake_event(conn: dict, investigation_id: str, sid: str, payload: dict, key: str, client: str,
                    opener=None) -> dict:
    """append-investigation-event of one session.wake report (conductor#2110):
    session_id = the woken session, never to_session_id; a bounded timeout."""
    return ie.append_event(conn, investigation_id, ie.SESSION_WAKE, payload, idempotency_key=key, client=client,
                           session_id=sid, producer_client=client, opener=opener, timeout=WAKE_REPORT_TIMEOUT)


def read_ack_file(tmp_dir: Any, seq: int) -> Tuple[Optional[dict], str]:
    """({"disposition", "note"?}, "") from <tmp_dir>/ACK_FILE, the answer the
    woken agent wrote, or (None, why it is not one). The directory must be
    a real directory (never a symlink); the file is opened without following
    a symlink (and without blocking on a FIFO) and must be a regular file of
    at most MAX_ACK_FILE bytes holding exactly {"ack_of": seq, "disposition":
    accepted|declined|noted, "note"?: text of at most ie.MAX_NOTE characters,
    no control characters but newline and tab}. Anything else is ignored."""

    if not (isinstance(tmp_dir, str) and os.path.isabs(tmp_dir)):
        return None, "the wake had no private temp directory"
    try:
        if not stat.S_ISDIR(os.lstat(tmp_dir).st_mode):
            return None, "the wake's temp directory is not a directory"
        fd = os.open(os.path.join(tmp_dir, ACK_FILE), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, "no acknowledgment written"
    except OSError as e:
        return None, f"the acknowledgment file cannot be opened ({type(e).__name__}; a symlink is never followed)"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "the acknowledgment file is not a regular file"
        if st.st_size > MAX_ACK_FILE:
            return None, f"the acknowledgment file is larger than {MAX_ACK_FILE} bytes"
        data = os.read(fd, MAX_ACK_FILE + 1)
    except OSError as e:
        return None, f"the acknowledgment file cannot be read ({type(e).__name__})"
    finally:
        os.close(fd)
    if len(data) > MAX_ACK_FILE:
        return None, f"the acknowledgment file is larger than {MAX_ACK_FILE} bytes"
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "the acknowledgment file is not JSON"
    if not isinstance(doc, dict) or not set(doc) <= {"ack_of", "disposition", "note"} \
            or "ack_of" not in doc or "disposition" not in doc:
        return None, "the acknowledgment file is not {ack_of, disposition, note}"
    ack_of = doc["ack_of"]
    if not isinstance(ack_of, int) or isinstance(ack_of, bool) or ack_of != seq:
        return None, f"the acknowledgment names #{str(ack_of)[:20]}, not the decision #{seq} it was woken for"
    if doc["disposition"] not in RELAY_DISPOSITIONS:
        return None, "the acknowledgment's disposition is not accepted, declined or noted"
    out = {"disposition": doc["disposition"]}
    note = doc.get("note")
    if note is not None:
        if not isinstance(note, str) or len(note) > ie.MAX_NOTE \
                or any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in note):
            return None, f"the acknowledgment's note is not text of at most {ie.MAX_NOTE} characters"
        if note.strip():
            out["note"] = note
    return out, ""


class System:
    """Every effect the waker has on the world, injectable for tests.

    now() -> epoch; alive(pid); process_start(pid) -> epoch | None;
    process_table() -> [(pid, argv)] | None; launcher(argv, cwd, env,
    log_path) -> pid (OSError: spawn error); notifier(title, message);
    connection(runtime, home) -> {origin, org, key}; fetch(conn, inv, sid,
    after, client) -> (events, complete); before_claim(sid, seq) (a test
    hook, called right before the claim's lock); environ (the child's
    environment is derived from it); out(line) (the report);
    process_rows() -> [(pid, ppid, argv)] | None; kill(pid, signal);
    sleep(seconds); port_busy(port) -> bool; run_probe(argv, path_env, cwd=)
    -> (rc, stdout); post_ack(conn, inv, sid, seq, disposition, note, client)
    (the relayed acknowledgment; raises on failure); post_wake(conn, inv,
    sid, payload, idempotency_key, client) (a session.wake report; raises);
    sandboxed_procs(since, tmp_dirs, canary) -> [pid] | None (a wake's
    surviving processes, by the sandbox TMPDIR-vs-canary test).
    """

    def __init__(self, *, now: Optional[Callable[[], float]] = None, alive: Optional[Callable[[int], bool]] = None,
                 process_start: Optional[Callable[[int], Optional[float]]] = None,
                 process_table: Optional[Callable[[], Optional[list]]] = None,
                 launcher: Optional[Callable[..., int]] = None,
                 notifier: Optional[Callable[[str, str], None]] = None,
                 connection: Optional[Callable[[str, Path], dict]] = None,
                 fetch: Optional[Callable[..., Tuple[list, bool]]] = None,
                 before_claim: Optional[Callable[[str, int], None]] = None,
                 environ: Optional[Mapping[str, str]] = None, opener=None,
                 out: Optional[Callable[[str], None]] = None,
                 process_rows: Optional[Callable[[], Optional[list]]] = None,
                 kill: Optional[Callable[[int, int], None]] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 port_busy: Optional[Callable[[int], bool]] = None,
                 run_probe: Optional[Callable[..., Tuple[int, str]]] = None,
                 post_ack: Optional[Callable[..., dict]] = None,
                 post_wake: Optional[Callable[..., dict]] = None,
                 sandboxed_procs: Optional[Callable[..., Optional[list]]] = None):
        self.sandboxed_procs = sandboxed_procs or globals()["sandboxed_procs"]
        self.post_wake = post_wake or (lambda conn, inv, sid, payload, key, client: post_wake_event(
            conn, inv, sid, payload, key, client, opener=opener))
        self.run_probe = run_probe or globals()["run_probe"]
        self.post_ack = post_ack or (lambda conn, inv, sid, seq, disposition, note, client: post_ack_event(
            conn, inv, sid, seq, disposition, note, client, opener=opener))
        self.process_rows = process_rows or globals()["process_rows"]
        self.kill = kill or os.kill
        self.sleep = sleep or time.sleep
        self.port_busy = port_busy or globals()["port_busy"]
        self.now = now or time.time
        self.alive = alive or poller.alive
        self.process_start = process_start or globals()["process_start"]
        self.process_table = process_table or globals()["process_table"]
        self.launcher = launcher or launch_detached
        self.notifier = notifier or notify_macos
        self.connection = connection or runtime_connection
        self.fetch = fetch or (lambda conn, inv, sid, after, client: fetch_events(conn, inv, sid, after, client,
                                                                                 opener=opener))
        self.before_claim = before_claim
        self.environ = os.environ if environ is None else environ
        self.out = out or (lambda line: print(line, flush=True))


# ---------------------------------------------------------------------------
# Runtime resolution and eligibility
# ---------------------------------------------------------------------------

class Runtime:
    """What runs session `sid` here and whether it may be launched.

    kind: codex | claude | unknown; eligible / reason; for an eligible Codex
    session, cwd (session_meta.cwd) and sandbox (the mode to pin: its last
    turn's); files: the rollouts or transcripts (their mtimes are a liveness
    signal)."""

    def __init__(self, kind: str, files: List[Path], eligible: bool = False, reason: str = "",
                 cwd: Optional[str] = None, sandbox: Optional[str] = None):
        self.kind, self.files, self.eligible, self.reason = kind, files, eligible, reason
        self.cwd, self.sandbox = cwd, sandbox
        self.extra: dict = {}   # launcher-specific facts (Gemini: the resume ident, the proxy's hosts)


def codex_rollouts(home: Path, sid: str) -> List[Path]:
    if not ie.valid_session(sid):
        return []
    root = home / ".codex" / "sessions"
    return sorted(Path(p) for p in glob.glob(str(root / "**" / f"rollout-*-{glob.escape(sid)}.jsonl"),
                                             recursive=True))


def claude_transcripts(home: Path, sid: str) -> List[Path]:
    if not ie.valid_session(sid):
        return []
    return sorted(Path(p) for p in glob.glob(str(home / ".claude" / "projects" / "*" / f"{glob.escape(sid)}.jsonl")))


GEMINI_HEAD = 64 * 1024   # bytes of a Gemini CLI chat file read to find its sessionId


def gemini_chats(home: Path, sid: str) -> List[Path]:
    """Gemini CLI's chat records for `sid`:
    ~/.gemini/tmp/<project>/chats/session-<time>-<sid[:8]>.json[l] whose
    head names sessionId == sid (only GEMINI_HEAD bytes are read)."""
    if not ie.valid_session(sid) or len(sid) < 8:
        return []
    pattern = str(home / ".gemini" / "tmp" / "*" / "chats" / f"session-*-{glob.escape(sid[:8])}.json*")
    want = re.compile(rb'"sessionId"\s*:\s*"' + re.escape(sid.encode()) + rb'"')
    out = []
    for p in sorted(glob.glob(pattern)):
        if not p.endswith((".json", ".jsonl")):
            continue
        with contextlib.suppress(OSError):
            with open(p, "rb") as f:
                if want.search(f.read(GEMINI_HEAD)):
                    out.append(Path(p))
    return out


def cursor_records(home: Path, sid: str) -> List[Path]:
    """Cursor's records for `sid` (best effort: Cursor documents no stable
    store): ~/.cursor/projects/*/agent-transcripts/<sid>.* and the files of
    ~/.cursor/chats/*/<sid>/. Not found: the runtime is unknown (still a
    notification)."""
    if not ie.valid_session(sid):
        return []
    s = glob.escape(sid)
    found = glob.glob(str(home / ".cursor" / "projects" / "*" / "agent-transcripts" / f"{s}.*"))
    found += glob.glob(str(home / ".cursor" / "chats" / "*" / s / "*"))
    return sorted(Path(p) for p in found if os.path.isfile(p))


# Each runtime's own records of a session (cheap: a glob; Gemini reads a
# file head). The records name the runtime and their mtimes are a liveness
# signal; nothing here makes a session launchable (LAUNCHERS does).
RECORDS = {CODEX: codex_rollouts, CLAUDE: claude_transcripts, CURSOR: cursor_records, GEMINI: gemini_chats}


def runtime_kind(home: Path, sid: str) -> Runtime:
    """The runtime by its own records. None found, or records of more than
    one runtime: unknown (notify-only)."""
    found = {r: files for r, files in ((r, find(home, sid)) for r, find in RECORDS.items()) if files}
    if len(found) > 1:
        return Runtime(UNKNOWN, [p for files in found.values() for p in files],
                       reason=f"records of {' and '.join(LABELS[r] for r in found)} carry this session id: "
                              "notify-only")
    if not found:
        return Runtime(UNKNOWN, [], reason="no Codex, Claude Code, Cursor or Gemini CLI records for this session "
                                           "on this machine: notify-only")
    (kind, files), = found.items()
    return Runtime(kind, files, reason=NOTIFY_ONLY_REASONS.get(kind, "not yet checked"))


class Unverifiable(Exception):
    """A rollout that cannot be read completely and safely."""


_RECORD_MARKERS = (b"turn_context", b"session_meta")
_MCP_MARKERS = (b"mcp__", b"mcpToolCall", b"mcp_tool_call", b'_call"')
_TURN_TYPE_RE = re.compile(rb'"type"\s*:\s*"turn_context"')


def _tool_call(rec: Any) -> Optional[Tuple[str, Optional[str], str]]:
    """(type, namespace, name) when a rollout record is a tool call the
    model made (a response_item whose payload type ends in _call: function,
    custom tool, web search, image generation, local shell...), else None.
    Namespace "functions" reads as None."""
    if not isinstance(rec, dict) or rec.get("type") != "response_item":
        return None
    p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    kind = p.get("type")
    if not (isinstance(kind, str) and kind.endswith("_call")):
        return None
    ns = p.get("namespace")
    ns = None if ns in (None, "", "functions") else str(ns)
    return kind, ns, str(p.get("name"))


def _mcp_call(rec: Any) -> Optional[Tuple[str, str]]:
    """(server, tool) when a rollout record is an MCP tool call, else None:
    a function_call in an mcp__<server> namespace, an mcp_tool_call_begin /
    _end event, or an mcpToolCall item."""
    if not isinstance(rec, dict):
        return None
    p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    ns = p.get("namespace")
    if p.get("type") == "function_call" and isinstance(ns, str) and ns.startswith("mcp__"):
        return ns[len("mcp__"):], str(p.get("name"))
    inv = p.get("invocation") if isinstance(p.get("invocation"), dict) else None
    if p.get("type") in ("mcp_tool_call_begin", "mcp_tool_call_end") and inv is not None:
        return str(inv.get("server")), str(inv.get("tool"))
    item = p.get("item") if isinstance(p.get("item"), dict) else None
    if item is not None and item.get("type") in ("mcpToolCall", "mcp_tool_call"):
        return str(item.get("server")), str(item.get("tool"))
    return None


def _records(path: Path, offset: int = 0, *, mcp: bool = False) -> Iterator[dict]:
    """The session_meta and turn_context records of a rollout from byte
    `offset` (with mcp, its MCP tool calls too: _mcp_call), streamed line by
    line (MAX_LINE per line, MAX_ROLLOUT_BYTES in all). A line that may be
    one of them but cannot be parsed (overlong, malformed) raises
    Unverifiable: every turn (and call) has to be seen."""
    st = os.lstat(str(path))
    if not os.path.isfile(str(path)) or os.path.islink(str(path)):
        raise Unverifiable("a rollout is not a regular file")
    if st.st_size - offset > MAX_ROLLOUT_BYTES:
        raise Unverifiable("a rollout is too large to verify")
    markers = _RECORD_MARKERS + (_MCP_MARKERS if mcp else ())
    total = 0
    with open(str(path), "rb") as f:
        f.seek(offset)
        while True:
            line = f.readline(MAX_LINE + 1)
            if not line:
                return
            total += len(line)
            if total > MAX_ROLLOUT_BYTES:
                raise Unverifiable("a rollout is too large to verify")
            if len(line) > MAX_LINE and not line.endswith(b"\n"):
                head = line[:4096]
                mcp_like = mcp and (b'"namespace"' in head or any(m in head for m in _MCP_MARKERS))
                if b"turn_context" in head or mcp_like:
                    raise Unverifiable("a rollout record is too large to verify")
                while not line.endswith(b"\n"):   # skip the rest of an overlong line
                    line = f.readline(MAX_LINE)
                    if not line:
                        return
                    total += len(line)
                    if total > MAX_ROLLOUT_BYTES:
                        raise Unverifiable("a rollout is too large to verify")
                continue
            if not any(m in line for m in markers):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                if _TURN_TYPE_RE.search(line):
                    raise Unverifiable("a rollout turn record is malformed")
                continue
            if isinstance(rec, dict) and (rec.get("type") in ("turn_context", "session_meta")
                                          or (mcp and (_mcp_call(rec) is not None or _tool_call(rec) is not None))):
                yield rec


def _payload(rec: dict) -> dict:
    return rec.get("payload") if isinstance(rec.get("payload"), dict) else {}


def _turn(rec: dict) -> Tuple[Any, Any]:
    """(sandbox_policy.type, network_access) of a turn_context record."""
    payload = _payload(rec)
    policy = payload.get("sandbox_policy") if isinstance(payload.get("sandbox_policy"), dict) else {}
    return policy.get("type"), policy.get("network_access")


_SPECIAL_WRITABLE = ("slash_tmp", "tmpdir")


def turn_policy_problem(payload: dict, cwd_real: str, *, strict: bool = False) -> Optional[str]:
    """Why one turn's recorded sandbox could write outside the session's
    working directory (realpath'd) or reach the network, or None. Checked:
    sandbox_policy.writable_roots; the file-system entries of
    file_system_sandbox_policy and permission_profile.file_system (every
    non-read entry a path inside cwd, or Codex's own /tmp / $TMPDIR);
    permission_profile.network (restricted only). Unknown shapes refuse.
    strict (a woken turn, under CODEX_SANDBOX_PINS): no writable root at
    all, never the shared /tmp (workspace-write must record
    exclude_slash_tmp true); $TMPDIR (the wake's private dir) only. Every
    turn must also have run in the session's working directory (review 3
    #8: workspace-write makes the turn's own cwd writable, and a turn
    without permission_profile shows no other trace of it)."""
    tcwd = payload.get("cwd")
    if not (isinstance(tcwd, str) and os.path.isabs(tcwd)) \
            or os.path.realpath(tcwd) != os.path.realpath(cwd_real):
        return "a turn ran in another working directory than the session's"
    policy = payload.get("sandbox_policy") if isinstance(payload.get("sandbox_policy"), dict) else {}
    if strict:
        if policy.get("writable_roots") not in (None, []):
            return "a woken turn had extra writable roots despite the pinned writable_roots=[]"
        if policy.get("type") == "workspace-write" and policy.get("exclude_slash_tmp") is not True:
            return "a woken turn could write the shared /tmp despite the pinned exclude_slash_tmp"
    writes: List[str] = []
    roots = policy.get("writable_roots")
    if roots is not None:
        if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
            return "a turn's writable_roots cannot be read"
        writes += roots
    profile = payload.get("permission_profile")
    if profile is not None and not isinstance(profile, dict):
        return "a turn's permission profile cannot be read"
    profile = profile or {}
    if profile.get("network") not in (None, "restricted"):
        return "a turn's permission profile allows network access"
    for fs in (payload.get("file_system_sandbox_policy"), profile.get("file_system")):
        if fs is None:
            continue
        if not isinstance(fs, dict):
            return "a turn's file-system policy cannot be read"
        kind = fs.get("kind", fs.get("type"))
        if kind not in (None, "restricted"):
            return f"a turn's file-system policy is {str(kind)[:40]!r}"
        entries = fs.get("entries", [])
        if not isinstance(entries, list):
            return "a turn's file-system policy cannot be read"
        for e in entries:
            if not isinstance(e, dict) or not isinstance(e.get("path"), dict):
                return "a turn's file-system entry cannot be read"
            if e.get("access") == "read":
                continue
            target = e["path"]
            if target.get("type") == "path" and isinstance(target.get("path"), str):
                writes.append(target["path"])
            elif target.get("type") == "special" and isinstance(target.get("value"), dict) \
                    and target["value"].get("kind") in _SPECIAL_WRITABLE:
                if strict and target["value"].get("kind") == "slash_tmp":
                    return "a woken turn could write the shared /tmp"
                continue
            else:
                return "a turn could write a location the waker does not recognize"
    for w in writes:
        if not os.path.isabs(w) or not _within(os.path.realpath(w), cwd_real):
            return "a turn could write outside the session's working directory"
    return None


def _git_root(cwd: str) -> Optional[Path]:
    """The git work tree's root containing `cwd` (the nearest directory with
    a .git entry, cwd included), or None."""
    p = Path(cwd)
    for d in (p, *p.parents):
        if os.path.lexists(str(d / ".git")):
            return d
    return None


def _git_work_tree(cwd: str) -> bool:
    return _git_root(cwd) is not None


def codex_project_config(cwd: str) -> Optional[str]:
    """Where Codex project config sits between `cwd` (realpath'd first: a
    symlinked cwd is judged where it really is) and its git work-tree root
    (both included), or None. A .codex/ holding any *.toml (config.toml can
    declare MCP servers, which a resumed `codex exec` starts OUTSIDE its
    sandbox: proof C-8) or hooks.json counts; so does a .codex that is not a
    plain directory (a symlink is never followed) or cannot be read (fail
    closed). A sandboxed agent cannot create .codex inside workspace-write;
    a cloned repo or an unsandboxed tool can."""
    cwd = os.path.realpath(cwd)
    root = _git_root(cwd)
    if root is None:
        return "no git work tree"
    d = Path(cwd)
    for current in (d, *d.parents):
        entry = current / ".codex"
        try:
            st = os.lstat(str(entry))
        except FileNotFoundError:
            st = None
        except OSError:
            return str(entry)
        if st is not None:
            if not stat.S_ISDIR(st.st_mode):
                return str(entry)
            try:
                names = os.listdir(str(entry))
            except OSError:
                return str(entry)
            if any(n.endswith(".toml") or n == "hooks.json" for n in names):
                return str(entry)
        if current == root:
            return None
    return None


def home_cwd_problem(cwd_real: str, home: Path) -> Optional[str]:
    """Why a Codex working directory (realpath) may not be woken: not
    strictly inside $HOME, or inside or above one of HOME_PROTECTED (its
    workspace-write sandbox would then write the waker's or an agent's
    config, keys or shell setup)."""
    home_real = os.path.realpath(str(home))
    if cwd_real == home_real or not _within(cwd_real, home_real):
        return "the session's working directory is not strictly inside your home directory"
    for name in HOME_PROTECTED + HOME_PROTECTED_FILES:
        protected = os.path.join(home_real, name)
        # Review 3 #3: also where it really is (~/.config -> ~/dotfiles/config, ~/.zshrc ->
        # ~/dotfiles/zshrc would otherwise make ~/dotfiles a writable workspace).
        for target in dict.fromkeys((protected, os.path.realpath(protected))):
            if _within(cwd_real, target) or _within(target, cwd_real):
                return f"the session's working directory is inside or above ~/{name} (or where it links to)"
    return None


def _codex_config_texts(home: Path) -> Tuple[List[Tuple[str, str]], Optional[str]]:
    """([(path, text)] of every Codex config file that exists: ~/.codex/
    config.toml and the system ones), or ([], why one cannot be read)."""
    out = []
    for p in [str(Path(home) / ".codex" / "config.toml")] + list(CODEX_CONFIG_FILES):
        if not os.path.lexists(p):
            continue
        try:
            if os.path.getsize(p) > MAX_CONFIG_READ:
                return [], f"{p} is too large to check"
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                out.append((p, f.read(MAX_CONFIG_READ + 1)))
        except OSError:
            return [], f"{p} cannot be read"
    return out, None


def _config_lines(text: str) -> Iterator[str]:
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            yield s


def codex_config_problem(home: Path) -> Optional[str]:
    """Why the Codex config would widen a woken workspace-write sandbox, or
    None: any sandbox_workspace_write / permissions / permission profile
    table or key, or a writable_roots / network_access key, in
    ~/.codex/config.toml, /etc/codex/config.toml or managed_config.toml."""
    texts, why = _codex_config_texts(home)
    if why:
        return why
    for path, text in texts:
        for line in _config_lines(text):
            if _CODEX_WIDENING_RE.match(line):
                return f"{path} widens the workspace-write sandbox ({line.split('=')[0].strip()[:60]})"
    return None


_MCP_HEADER_RE = re.compile(r'^\[\s*mcp_servers\s*\.\s*(?:"([^"]*)"|([A-Za-z0-9_-]+))\s*(?:\.[^\]]*)?\]$')
_PLUGIN_HEADER_RE = re.compile(r'^\[\s*plugins\s*\.\s*"([^"]+)"\s*(?:\.[^\]]*)?\]$')
_MCP_KEY_MENTION_RE = re.compile(r'''(?:^\s*\[{0,2}\s*|[.{,]\s*)["']?(?:mcp_servers|plugins)["']?\s*(?:[.=\]]|$)''')


def _toml_key(ident: str) -> Optional[str]:
    if _TOML_BARE_KEY_RE.fullmatch(ident):
        return ident
    if _TOML_QUOTED_ID_RE.fullmatch(ident):
        return f'"{ident}"'
    return None


def codex_mcp_overrides(home: Path) -> Tuple[Optional[List[str]], str]:
    """(the -c values that leave a woken run only the Cardinal server's
    CARDINAL_WAKE_TOOLS, "") or (None, why the MCP servers cannot be
    enumerated). Every [mcp_servers.<id>] but the Cardinal one gets
    enabled=false, the Cardinal one enabled_tools=CARDINAL_WAKE_TOOLS, and
    every [plugins."<id>"] but the Cardinal plugin enabled=false (a plugin
    can bring its own MCP servers). Any other way of declaring servers or
    plugins (dotted keys, inline tables, arrays of tables) refuses."""
    texts, why = _codex_config_texts(home)
    if why:
        return None, why
    servers, plugins = set(), set()
    for path, text in texts:
        table = ""
        for line in _config_lines(text):
            m = _MCP_HEADER_RE.match(line)
            if m:
                servers.add(m.group(1) if m.group(1) is not None else m.group(2))
                table = "mcp_servers"
                continue
            m = _PLUGIN_HEADER_RE.match(line)
            if m:
                plugins.add(m.group(1))
                table = "plugins"
                continue
            if line.startswith("["):
                table = line
            # Review 3 #6: mcp_servers / plugins in any key position, bare or quoted
            # (["mcp_servers".x], "mcp_servers".x.command = ..., a = { plugins = ... }), that is not
            # one of the strict headers above refuses; `plugins = <bool>` in [features] is the
            # feature flag (disabled on the command line anyway).
            if _MCP_KEY_MENTION_RE.search(line) and not (
                    table == "[features]" and re.fullmatch(r"plugins\s*=\s*(true|false)", line)):
                return None, f"{path} declares MCP servers or plugins in a form the waker cannot disable"
    out = []
    for ident in sorted(servers):
        key = _toml_key(ident)
        if key is None:
            return None, f"the MCP server id {ident[:40]!r} cannot be disabled on the command line"
        if ident == CARDINAL_MCP_SERVER:
            # Under approval_policy=never an MCP call that needs approval is refused (H1): the two
            # allowed tools are pre-approved, nothing else is reachable.
            out += [f'mcp_servers.{key}.tools.{tool}.approval_mode="approve"' for tool in CARDINAL_WAKE_TOOLS]
            out.append(f"mcp_servers.{key}.enabled_tools=" + json.dumps(list(CARDINAL_WAKE_TOOLS)))
        else:
            out.append(f"mcp_servers.{key}.enabled=false")
    for ident in sorted(plugins):
        if ident.startswith(CARDINAL_PLUGIN_PREFIX):
            continue
        key = _toml_key(ident)
        if key is None:
            return None, f"the plugin id {ident[:40]!r} cannot be disabled on the command line"
        out.append(f"plugins.{key}.enabled=false")
    return out, ""


def codex_eligibility(home: Path, sid: str, rt: Optional[Runtime] = None) -> Runtime:
    """rt (runtime_kind) with eligible / reason / cwd / sandbox / extra
    (mcp_overrides) filled in from the rollouts and the Codex config
    (§5.3)."""
    rt = rt or runtime_kind(home, sid)
    if rt.kind != CODEX:
        return rt
    rt.eligible, rt.cwd, rt.sandbox = False, None, None
    if not UUID_RE.fullmatch(sid):
        rt.reason = "the session id is not a Codex UUID: notify-only"
        return rt
    meta, turns = None, []
    try:
        for path in rt.files:
            for rec in _records(path):
                if rec["type"] == "session_meta":
                    p = _payload(rec)
                    if meta is None and p.get("id") == sid:
                        meta = p
                else:
                    turns.append(_payload(rec))
    except (OSError, Unverifiable) as e:
        rt.reason = f"{e if isinstance(e, Unverifiable) else 'a rollout could not be read'}: notify-only"
        return rt
    if meta is None:
        rt.reason = "the rollout has no session record for this session: notify-only"
        return rt
    originator, source = meta.get("originator"), meta.get("source")
    if originator not in CODEX_ORIGINATORS:
        rt.reason = ("the Codex desktop app's sessions are notify-only" if originator == "Codex Desktop"
                     else "not a Codex CLI session (unrecognized originator): notify-only")
        return rt
    if not (isinstance(source, str) and source in CODEX_SOURCES):
        rt.reason = "not a top-level Codex CLI session (a subagent or another source): notify-only"
        return rt
    if not turns:
        rt.reason = "no turn recorded a sandbox: notify-only"
        return rt
    modes = []
    for payload in turns:
        mode, network = _turn({"payload": payload})
        if mode not in SANDBOX_MODES:
            rt.reason = ("the session ran danger-full-access at least once: notify-only" if mode == "danger-full-access"
                         else "a turn ran without a recognized sandbox: notify-only")
            return rt
        if network is True:
            rt.reason = "the session ran with network access at least once: notify-only"
            return rt
        modes.append(mode)
    cwd = meta.get("cwd")
    if not (isinstance(cwd, str) and os.path.isabs(cwd) and os.path.isdir(cwd)):
        rt.reason = "the session's working directory is gone: notify-only"
        return rt
    real = os.path.realpath(cwd)   # every check and the launch use where it really is
    why = home_cwd_problem(real, home)
    if why:
        rt.reason = why + ": notify-only"
        return rt
    for payload in turns:
        why = turn_policy_problem(payload, real)
        if why:
            rt.reason = why + ": notify-only"
            return rt
    if not _git_work_tree(real):
        rt.reason = ("the session's working directory is not a git work tree (codex exec would need "
                     "--skip-git-repo-check, which is never passed): notify-only")
        return rt
    found = codex_project_config(real)
    if found:
        rt.reason = ("the project has its own Codex config (a .codex with config or hooks between the working "
                     "directory and the repository root; its MCP servers would run outside the sandbox): "
                     "notify-only")
        return rt
    why = codex_config_problem(home)
    if why:
        rt.reason = why + ": notify-only"
        return rt
    overrides, why = codex_mcp_overrides(home)
    if overrides is None:
        rt.reason = why + ": notify-only"
        return rt
    if modes[-1] == "read-only":
        # Review 6 #2 (strict): the survivor test is "the sandbox grants the wake's TMPDIR". A
        # read-only sandbox grants no writes at all, so a read-only run's processes cannot be told
        # from any other sandboxed process, and gate 3 cannot be enforced. Notify-only.
        rt.reason = "the session's last turn ran read-only, which the survivor check cannot bound: notify-only"
        return rt
    rt.eligible, rt.reason, rt.cwd, rt.sandbox = True, "eligible", real, modes[-1]
    rt.extra = {"mcp_overrides": overrides}
    return rt


def resolve(home: Path, sid: str) -> Runtime:
    """The session's runtime and, for a runtime with a launcher, whether it
    is eligible; any other runtime is notify-only."""
    rt = runtime_kind(home, sid)
    launcher = active_launcher(rt.kind)
    return launcher.eligibility(home, sid, rt, read_config(home)) if launcher else rt


def rollout_offsets(home: Path, sid: str, rt: Optional[Runtime] = None) -> dict:
    """What verify_after needs from before a launch: {offsets: {path: size}
    of the session's rollouts, cwd: the realpath the session runs in}."""
    out = {}
    for p in codex_rollouts(home, sid):
        with contextlib.suppress(OSError):
            out[str(p)] = os.lstat(str(p)).st_size
    return {"offsets": out, "cwd": rt.cwd if rt is not None else None}


def verify_after(home: Path, sid: str, snapshot: dict, pinned: str, *, require_turn: bool = True) -> Optional[str]:
    """None when everything the woken run added to the rollout (past the
    snapshot's offsets, or in a rollout that did not exist then) stayed in
    bounds: every turn_context ran `pinned` with network off and could write
    only inside the session's working directory (turn_policy_problem), every
    tool call is in CODEX_ALLOWED_CALLS (no web search, image generation,
    app connector or other MCP tool), and every MCP tool call event was a
    CARDINAL_WAKE_TOOLS call on the Cardinal server; else why not.
    require_turn: no turn at all is a failure too."""
    offsets = snapshot.get("offsets") if isinstance(snapshot.get("offsets"), dict) else snapshot
    cwd = snapshot.get("cwd") if isinstance(snapshot.get("cwd"), str) else None
    seen = 0
    try:
        for p in codex_rollouts(home, sid):
            start = offsets.get(str(p), 0) if isinstance(offsets.get(str(p), 0), int) else 0
            if os.lstat(str(p)).st_size < start:
                return "the rollout was rewritten during the woken run"
            for rec in _records(p, start, mcp=True):
                tool = _tool_call(rec)
                if tool is not None:
                    if tool not in CODEX_ALLOWED_CALLS:
                        return (f"the woken run made a tool call outside its allow-list "
                                f"({tool[0][:40]} {str(tool[1])[:40]}/{tool[2][:60]})")
                    continue
                call = _mcp_call(rec)
                if call is not None:
                    if call[0] != CARDINAL_MCP_SERVER or call[1] not in CARDINAL_WAKE_TOOLS:
                        return (f"the woken run called the MCP tool {call[0][:40]}/{call[1][:60]}, "
                                "outside its allow-list")
                    continue
                if rec["type"] != "turn_context":
                    continue
                seen += 1
                mode, network = _turn(rec)
                if mode != pinned:
                    return f"the woken run's sandbox was {mode!r}, not the pinned {pinned!r}"
                if network is True:
                    return "the woken run had network access"
                if cwd is None:
                    return "the woken run's working directory is unknown"
                why = turn_policy_problem(_payload(rec), cwd, strict=True)
                if why:
                    return f"the woken run: {why}"
    except (OSError, Unverifiable) as e:
        return f"the woken run's rollout could not be verified ({e})"
    if require_turn and not seen:
        return "the woken run recorded no turn, so its sandbox could not be confirmed"
    if cwd is not None and codex_project_config(cwd):   # review 3 #2: re-checked after the run too
        return "the repository carries Codex project config after the woken run (it would run unsandboxed next time)"
    return None


def codex_lockdown_flags(overrides: Iterable[str] = ()) -> List[str]:
    """The flags that lock a woken run down (H1), in order: approvals off,
    web search off, the MCP restriction (one -c per codex_mcp_overrides
    value), notify=[] (last) and --disable per CODEX_DISABLED_FEATURES. The same flags are
    validated beforehand with `codex ... features list` (codex_lockdown_problem)."""
    flags = ["-c", APPROVAL_NEVER, "-c", CODEX_WEB_SEARCH_OFF]
    for pin in CODEX_SANDBOX_PINS:
        flags += ["-c", pin]
    for value in overrides:
        if not (isinstance(value, str) and re.match(r"^(mcp_servers|plugins)\.", value)):
            raise ValueError(f"not an MCP restriction: {value!r}")
        flags += ["-c", value]
    flags += ["-c", CODEX_NOTIFY_OFF]   # last: it beats any notify set earlier (review 3 #1)
    for feature in CODEX_DISABLED_FEATURES:
        flags += ["--disable", feature]
    return flags


def codex_argv(codex_path: str, sandbox: str, sid: str, seq: int, overrides: Iterable[str] = (),
               investigation_id: str = "") -> List[str]:
    """The pinned launch (§5.3); nothing else is ever passed: the session's
    own sandbox and the lockdown (codex_lockdown_flags), all before
    `resume`; the prompt names the decision and its investigation."""
    if not (isinstance(codex_path, str) and os.path.isabs(codex_path)):
        raise ValueError("codex must be an absolute path")
    if sandbox not in SANDBOX_MODES:
        raise ValueError(f"not a wake sandbox: {sandbox!r}")
    if not UUID_RE.fullmatch(sid or ""):
        raise ValueError(f"not a Codex session id: {sid!r}")
    return [codex_path, "exec", "-s", sandbox, *codex_lockdown_flags(overrides), "resume", sid,
            wake_prompt(seq, investigation_id)]


def parse_features(text: str) -> dict:
    """{name: (stage, enabled)} from `codex features list` (name, a stage that
    may contain spaces, true / false per line)."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-1] in ("true", "false") and re.fullmatch(r"[a-z0-9_]{1,80}", parts[0]):
            out[parts[0]] = (" ".join(parts[1:-1]), parts[-1] == "true")
    return out


# A probe outcome that may be transient (codex could not be run this pass):
# the decision is reconsidered, not consumed (claim_and_launch, _transient_probe).
_TRANSIENT_PROBE = ("could not be run to validate the lockdown", "could not be run to list its MCP servers")
PROBE_RETRY_PASSES = 3


def _transient_probe(why: Any) -> bool:
    return isinstance(why, str) and any(s in why for s in _TRANSIENT_PROBE)


def codex_lockdown_problem(home: Path, config: dict, system: "System", rt: Optional[Runtime] = None) -> Optional[str]:
    """Why the lockdown cannot be trusted for this codex (fail closed: a
    notification, never a launch with a partial lockdown), or None. Runs
    `<codex> <lockdown flags> features list` (no session; the user's config
    is loaded and the -c keys and feature names validated): it must exit 0,
    every CODEX_DISABLED_FEATURES feature must be listed, not removed, and
    off, and every feature still on must be in CODEX_TESTED_ON (N4: a new
    default-on feature refuses until reviewed). Then `<codex> <lockdown
    flags> mcp list --json` must list no enabled server but the Cardinal one
    (this also sees cloud-managed, MDM and project layers the waker does not
    parse). Both run in the launch's working directory (rt.cwd, realpath'd),
    so they load the same project layers the launch would, and both run
    before every launch, never cached (review 3: config layers the waker
    cannot see can change between launches)."""
    codex = config.get("codex_path")
    if not codex:
        return "codex was not found when wake was enabled"
    overrides = (rt.extra.get("mcp_overrides") if rt is not None else None) or []
    try:
        flags = codex_lockdown_flags(overrides)
    except ValueError as e:
        return str(e)
    cwd = os.path.realpath(rt.cwd) if rt is not None and isinstance(rt.cwd, str) and rt.cwd else None
    if cwd is None:
        return "the launch's working directory is unknown, so the lockdown cannot be validated there"
    try:
        rc, out = system.run_probe([codex, *flags, "features", "list"], config.get("path_env"), cwd=cwd)
    except Exception as e:
        return f"codex could not be run to validate the lockdown ({type(e).__name__})"
    if rc != 0:
        return "codex rejected the lockdown flags (an unknown feature or config key): no launch"
    features = parse_features(out)
    for name in CODEX_DISABLED_FEATURES:
        got = features.get(name)
        if got is None:
            return f"codex does not know the feature {name!r}, so the lockdown would be partial: no launch"
        stage, enabled = got
        if stage == "removed" or enabled:
            return f"codex cannot disable the feature {name!r} ({stage}): no launch"
    untested = sorted(n for n, (stage, enabled) in features.items() if enabled and n not in CODEX_TESTED_ON)
    if untested:
        return ("codex has features on that the lockdown was not tested with (" + ", ".join(untested)[:200]
                + "): no launch until they are reviewed")
    try:
        rc, out = system.run_probe([codex, *flags, "mcp", "list", "--json"], config.get("path_env"), cwd=cwd)
    except Exception as e:
        return f"codex could not be run to list its MCP servers ({type(e).__name__})"
    try:
        servers = json.loads(out) if rc == 0 else None
    except ValueError:
        servers = None
    if not isinstance(servers, list) or not all(isinstance(s, dict) for s in servers):
        return "codex could not list its MCP servers under the lockdown: no launch"
    # Review 6 #6: this probe, not the config text scan, is the enforcement. codex resolves every
    # layer (user, project, managed, cloud) and every TOML form here, so a server the scan cannot
    # read off the text (an escaped or nested key, `["mcp_servers".evil]`) still shows as enabled
    # and refuses the launch. The scan (codex_mcp_overrides) only builds the -c disables and fails
    # closed on a form it cannot name; it is not the last word.
    others = sorted(str(s.get("name"))[:40] for s in servers
                    if s.get("enabled") is not False and s.get("name") != CARDINAL_MCP_SERVER)
    if others:
        return "MCP servers other than Cardinal stay enabled under the lockdown (" + ", ".join(others)[:200] + ")"
    return None


def run_probe(argv: List[str], path_env: Optional[str], cwd: Optional[str] = None) -> Tuple[int, str]:
    """Run a read-only codex command (never a session) in `cwd` (the launch's
    working directory, so it loads the same project layers); (exit code,
    stdout)."""
    r = subprocess.run(argv, capture_output=True, text=True, timeout=CODEX_PROBE_TIMEOUT, check=False,
                       stdin=subprocess.DEVNULL, env=child_env(os.environ, path_env, extra=CODEX_AUTH_ENV),
                       cwd=cwd)
    return r.returncode, r.stdout


def child_env(environ: Mapping[str, str], path_env: Optional[str] = None, *, home: Optional[Path] = None,
              tmpdir: Optional[str] = None, extra: Iterable[str] = ()) -> dict:
    """The woken child's environment, from an explicit allow-list (N3):
    HOME, USER, LOGNAME, LANG, LC_*, SHELL, TERM=dumb, PATH = the daemon's
    vetted path_env, TMPDIR = this wake's private dir, plus only the named
    `extra` variables (codex: OPENAI_API_KEY for API-key auth). Nothing else
    is inherited: no NODE_OPTIONS / NODE_PATH / DYLD_* loader variables, no
    *_BASE_URL, proxy or CA overrides, no CARDINAL_* switch, CODEX_HOME or
    parent session id."""
    env = {k: environ[k] for k in CHILD_ENV_KEEP if environ.get(k)}
    env.update({k: v for k, v in environ.items() if k.startswith("LC_") and re.fullmatch(r"LC_[A-Z]{1,20}", k)})
    env["HOME"] = str(home) if home is not None else (environ.get("HOME") or str(Path.home()))
    env["TERM"] = "dumb"
    env["PATH"] = path_env or daemon_path()
    if tmpdir:
        env["TMPDIR"] = tmpdir
    for k in extra:
        if environ.get(k):
            env[k] = environ[k]
    return env


def private_wake_dir(home: Path, log: Path) -> str:
    """A new 0700 directory ~/.cardinal/wake/tmp/<wake id>-<random> (realpath),
    this wake's own temp dir, removed when the wake settles (remove_tmpdir).
    Raises OSError."""
    import tempfile
    base = wake_tmp_root(home)
    _private_dir(wake_dir(home))
    _private_dir(base)
    d = tempfile.mkdtemp(prefix=log.stem + "-", dir=str(base))
    os.chmod(d, 0o700)
    return os.path.realpath(d)


def codex_prepare(home: Path, rt: Runtime, config: dict, log: Path, system: "System") -> Optional[str]:
    """Right before a Codex spawn: this wake's private TMPDIR (L-b,
    private_wake_dir), never the shared /tmp or the user's $TMPDIR."""
    try:
        rt.extra["tmp_dir"] = private_wake_dir(home, log)
    except OSError as e:
        return f"no private temp directory ({type(e).__name__})"
    return None


def remove_tmpdir(home: Path, path: Any) -> None:
    """Remove a finished wake's private TMPDIR (only one under ~/.cardinal/wake/tmp)."""
    if not (isinstance(path, str) and os.path.isabs(path)):
        return
    base = os.path.realpath(str(wake_tmp_root(home)))
    if os.path.dirname(os.path.realpath(path)) == base:
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Gemini CLI launch (built, shipped disabled: GEMINI_LAUNCH_PROVEN)
# ---------------------------------------------------------------------------

# The one switch: True only once every Gemini gate in §6 has passed live
# (C-9 is proven; C-10..C-14 are pending). While False the Gemini launcher is
# never used and Gemini CLI sessions are notify-only.
GEMINI_LAUNCH_PROVEN = False
GEMINI_PROFILE = "restrictive-proxied"   # seatbelt: writes to the workspace/temp/cache only, network via :8877 only
GEMINI_PROXY_PORT = 8877                 # the only outbound port the proxied profiles allow
GEMINI_API_HOST = "generativelanguage.googleapis.com"
# The hosts each supported auth type needs besides the Gemini API (gemini-cli
# 0.63.0 bundle: Code Assist and the OAuth token refresh). Anything else
# (Vertex AI, Cloud Shell, ADC) is not supported for an unattended wake.
GEMINI_AUTH_HOSTS = {
    "gemini-api-key": (),
    "oauth-personal": ("cloudcode-pa.googleapis.com", "oauth2.googleapis.com"),
}
SPAWN_VERIFY_WINDOW = 20.0
SPAWN_VERIFY_POLL = 0.5
# Environment that changes Gemini CLI's sandbox, its proxy, where it reads
# settings, or opens a debugger: never inherited by a woken Gemini (the
# waker sets the first three itself), and an env file Gemini may load that
# sets any of them makes the session notify-only (dotenv fills only unset
# variables, so a stripped one could come back from a .env: SANDBOX set
# there would make Gemini skip its sandbox entirely).
GEMINI_SANDBOX_ENV = frozenset({
    "GEMINI_SANDBOX", "SEATBELT_PROFILE", "GEMINI_SANDBOX_PROXY_COMMAND", "SANDBOX", "SANDBOX_FLAGS",
    "GEMINI_SANDBOX_IMAGE", "BUILD_SANDBOX", "SANDBOX_PORTS", "SANDBOX_MOUNTS", "SANDBOX_ENV", "GEMINI_CLI_HOME",
    "GEMINI_CLI_SYSTEM_SETTINGS_PATH", "GEMINI_CLI_SYSTEM_DEFAULTS_PATH", "GEMINI_CLI_TRUST_WORKSPACE",
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
    "NODE_OPTIONS", "DEBUG", "DEBUG_PORT",
})
GEMINI_SANDBOX_ENV_PREFIXES = ("SEATBELT_", "GEMINI_SANDBOX")
# Project files that change the sandbox (gemini-cli 0.63.0: a custom
# seatbelt profile is looked up as .gemini/sandbox-macos-<name>.sb; the
# container sandbox reads sandbox.Dockerfile / sandbox.bashrc).
GEMINI_PROJECT_SANDBOX_FILES = re.compile(r"sandbox-macos-.*\.sb|sandbox\.Dockerfile|sandbox\.bashrc")
# Settings keys that change the sandbox or what it may write, in any
# settings file between cwd and the work-tree root (tools.sandbox* and
# security.* are any value; context.includeDirectories become writable
# INCLUDE_DIR_n inside the sandbox). The command keys (MCP servers, hooks,
# tool discovery) are refused too until a live proof shows Gemini's outer,
# unsandboxed process runs none of them before it enters the sandbox (C-13).
GEMINI_PROJECT_SETTINGS_KEYS = (
    ("tools", "sandbox"), ("tools", "sandboxAllowedPaths"), ("tools", "sandboxNetworkAccess"), ("security",),
    ("context", "includeDirectories"), ("mcpServers",), ("hooks",), ("tools", "discoveryCommand"),
    ("tools", "callCommand"), ("mcp", "serverCommand"),
)
# In the user's and the system's settings only what widens the sandbox.
GEMINI_USER_SETTINGS_KEYS = (
    ("context", "includeDirectories"), ("tools", "sandboxAllowedPaths"), ("tools", "sandboxNetworkAccess"),
    ("security", "toolSandboxing"),
)
GEMINI_SYSTEM_SETTINGS = ("/Library/Application Support/GeminiCli/settings.json",
                          "/Library/Application Support/GeminiCli/system-defaults.json")
MAX_CONFIG_READ = 256 * 1024
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _read_small(path: Path) -> Optional[bytes]:
    """A regular, non-symlink file's bytes (at most MAX_CONFIG_READ), or None
    when it is anything else or cannot be read (callers fail closed)."""
    try:
        st = os.lstat(str(path))
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_CONFIG_READ:
            return None
        with open(str(path), "rb") as f:
            return f.read(MAX_CONFIG_READ + 1)
    except OSError:
        return None


def _env_file_sets_sandbox(path: Path) -> Optional[str]:
    """Why env file `path` disqualifies a Gemini wake, or None (absent, or
    sets nothing in GEMINI_SANDBOX_ENV)."""
    if not os.path.lexists(str(path)):
        return None
    data = _read_small(path)
    if data is None:
        return f"{path} cannot be checked (a symlink, too large or unreadable)"
    for line in data.decode("utf-8", "replace").splitlines():
        m = _ENV_LINE_RE.match(line)
        if m and (m.group(1) in GEMINI_SANDBOX_ENV or m.group(1).startswith(GEMINI_SANDBOX_ENV_PREFIXES)):
            return f"{path} sets {m.group(1)}"
    return None


def _strip_json_comments(text: str) -> str:
    """JSON with // and /* */ comments (Gemini's settings) as plain JSON."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _settings_sets(path: Path, keys, *, any_value: bool) -> Optional[str]:
    """Why settings file `path` disqualifies a Gemini wake, or None.
    any_value: a key present at all counts; else only a non-empty / true
    value."""
    if not os.path.lexists(str(path)):
        return None
    data = _read_small(path)
    if data is None:
        return f"{path} cannot be checked (a symlink, too large or unreadable)"
    try:
        settings = json.loads(_strip_json_comments(data.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        return f"{path} cannot be parsed"
    if not isinstance(settings, dict):
        return f"{path} is not a settings object"
    for key in keys:
        node: Any = settings
        for part in key:
            node = node.get(part, None) if isinstance(node, dict) else None
        if node is None:
            continue
        if any_value or (node not in (False, [], {}, "")):
            return f"{path} sets {'.'.join(key)}"
    return None


def gemini_project_config(cwd: str, home: Path) -> Optional[str]:
    """Why Gemini CLI's own configuration around `cwd` disqualifies an
    unattended wake (fail closed), or None. Checked: from cwd up to the git
    work-tree root, any .gemini/ that is not a plain directory or cannot be
    read, holds a custom sandbox file (GEMINI_PROJECT_SANDBOX_FILES), or a
    settings.json setting GEMINI_PROJECT_SETTINGS_KEYS; every env file
    Gemini may load (.gemini/.env and .env from cwd up to /, then
    ~/.gemini/.env and ~/.env: a superset of the one it picks) setting
    GEMINI_SANDBOX_ENV; the user's and system settings widening the sandbox
    (GEMINI_USER_SETTINGS_KEYS)."""
    root = _git_root(cwd)
    if root is None:
        return "no git work tree"
    d = Path(cwd)
    for current in (d, *d.parents):
        if current == root or root in current.parents:
            entry = current / ".gemini"
            try:
                st = os.lstat(str(entry))
            except FileNotFoundError:
                st = None
            except OSError:
                return f"{entry} cannot be checked"
            if st is not None:
                if not stat.S_ISDIR(st.st_mode):
                    return f"{entry} is not a plain directory"
                try:
                    names = os.listdir(str(entry))
                except OSError:
                    return f"{entry} cannot be read"
                for name in names:
                    if GEMINI_PROJECT_SANDBOX_FILES.fullmatch(name):
                        return f"{entry / name} customizes Gemini's sandbox"
                why = _settings_sets(entry / "settings.json", GEMINI_PROJECT_SETTINGS_KEYS, any_value=True)
                if why:
                    return why
        for env_file in (current / ".gemini" / ".env", current / ".env"):
            why = _env_file_sets_sandbox(env_file)
            if why:
                return why
    for env_file in (Path(home) / ".gemini" / ".env", Path(home) / ".env"):
        why = _env_file_sets_sandbox(env_file)
        if why:
            return why
    for settings in [Path(home) / ".gemini" / "settings.json"] + [Path(p) for p in GEMINI_SYSTEM_SETTINGS]:
        why = _settings_sets(settings, GEMINI_USER_SETTINGS_KEYS, any_value=False)
        if why:
            return why
    return None


def gemini_auth(home: Path, environ: Mapping[str, str]) -> Tuple[Optional[tuple], str]:
    """(the extra hosts the user's Gemini auth needs, "") or (None, why it
    is not supported for an unattended wake). Inside the sandbox Gemini
    cannot read its API key from the macOS keychain, so an API-key user
    needs GEMINI_API_KEY in ~/.gemini/.env (only the name is checked, never
    the value)."""
    settings_path = Path(home) / ".gemini" / "settings.json"
    data = _read_small(settings_path) if os.path.lexists(str(settings_path)) else None
    selected = None
    if data is not None:
        with contextlib.suppress(ValueError, UnicodeDecodeError, AttributeError):
            selected = (json.loads(_strip_json_comments(data.decode("utf-8"))).get("security") or {}) \
                .get("auth", {}).get("selectedType")
    if selected == "oauth-personal":
        # The restrictive-proxied profile denies reading oauth_creds.json (and
        # every .env file) inside the sandbox, which now wraps the whole
        # process from its first instruction (no unsandboxed outer Gemini to
        # read it first), so a Google sign-in cannot work: notify-only.
        return None, ("Sign in with Google is not supported for an unattended Gemini wake (its sandbox cannot read "
                      "the OAuth credentials); a Gemini API key is")
    if selected not in GEMINI_AUTH_HOSTS:
        return None, f"Gemini auth {selected!r} is not supported for an unattended wake (Gemini API key only)"
    if selected == "gemini-api-key" and not environ.get("GEMINI_API_KEY"):
        env = _read_small(Path(home) / ".gemini" / ".env") or b""
        names = {m.group(1) for m in (_ENV_LINE_RE.match(line) for line in env.decode("utf-8", "replace")
                                      .splitlines()) if m}
        if "GEMINI_API_KEY" not in names:
            return None, ("Gemini reads its API key from the keychain, which its sandbox cannot reach: put "
                          "GEMINI_API_KEY in ~/.gemini/.env")
    return GEMINI_AUTH_HOSTS[selected], ""


def gemini_hosts(home: Path, auth_hosts: tuple) -> List[str]:
    """The proxy's allow-list: the Gemini API, the auth hosts, and the
    Cardinal hosts of the Gemini plugin's connection (~/.gemini/cardinal.json
    mcp_url and ingest_endpoint)."""
    import urllib.parse
    from .allowlist_proxy import valid_host
    hosts = [GEMINI_API_HOST, *auth_hosts]
    state = _read_json(Path(home) / ".gemini" / "cardinal.json")
    for key in ("mcp_url", "ingest_endpoint"):
        url = state.get(key)
        if isinstance(url, str) and url.startswith("https://"):
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
            if valid_host(host):
                hosts.append(host)
    return list(dict.fromkeys(hosts))


def gemini_project_root(chat: Path) -> Optional[str]:
    """The project directory Gemini recorded for a chat file
    (~/.gemini/tmp/<project>/.project_root): agent-writable, hence the
    approved roots."""
    data = _read_small(chat.parent.parent / ".project_root")
    if data is None:
        return None
    text = data.decode("utf-8", "replace").strip()
    return text if text and "\n" not in text and os.path.isabs(text) else None


def resolve_gemini_resume_ident(sid: str, cwd: str) -> Optional[str]:
    """What `gemini --resume` is given for session `sid` started in `cwd`.

    LIVE-PROOF HOOK (C-10): gemini-cli 0.63.0's --resume help names "latest"
    or an index; whether it takes the session UUID is unproven. This passes
    the UUID. If the live run shows it does not, implement the fallback
    here: run `<gemini> --list-sessions` in `cwd` (read-only), find the line
    naming `sid`, and return its index as a string of digits; return None
    when it is not listed (the session is then not eligible). Whatever this
    returns is never an option (never starts with "-")."""
    return sid if UUID_RE.fullmatch(sid or "") else None


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def approved_root_problem(path: str, home: Path) -> Optional[str]:
    """Why `path` (a realpath) may not be an approved root or a Gemini wake
    directory: the filesystem root, the home directory or any ancestor of it."""
    real_home = os.path.realpath(str(home))
    if path == os.sep or path == real_home or _within(real_home, path):
        return "it is your home directory or above it"
    return None


def gemini_eligibility(home: Path, sid: str, rt: Optional[Runtime] = None, config: Optional[dict] = None) -> Runtime:
    """rt (runtime_kind) with eligible / reason / cwd / sandbox / extra
    filled in for a Gemini CLI session (§5.3 Gemini). Its records are
    agent-writable, so what bounds a wake is the approved root, the proxy
    and the runtime sandbox check, not the session history."""
    rt = rt or runtime_kind(home, sid)
    config = config or read_config(home)
    if rt.kind != GEMINI:
        return rt
    rt.eligible, rt.cwd, rt.sandbox = False, None, None
    if not UUID_RE.fullmatch(sid):
        rt.reason = "the session id is not a Gemini CLI UUID: notify-only"
        return rt
    roots = [r for r in config.get("roots") or [] if isinstance(r, str) and os.path.isabs(r)]
    if not roots:
        rt.reason = "no approved root for Gemini wakes (cardinal-wake enable --root PATH): notify-only"
        return rt
    projects = {gemini_project_root(p) for p in rt.files}
    if len(projects) != 1 or None in projects:
        rt.reason = "Gemini's project record for this session is missing or ambiguous: notify-only"
        return rt
    cwd = projects.pop()
    if not os.path.isdir(cwd):
        rt.reason = "the session's working directory is gone: notify-only"
        return rt
    real = os.path.realpath(cwd)
    if approved_root_problem(real, home):
        rt.reason = "the session's working directory is your home directory or above it: notify-only"
        return rt
    if not any(_within(real, root) for root in roots):
        rt.reason = "the session's working directory is not inside an approved root: notify-only"
        return rt
    if not _git_work_tree(real):
        rt.reason = "the session's working directory is not a git work tree: notify-only"
        return rt
    plain_home, boxed_home = state_homes(home)
    plain, boxed = ie.sandbox_turns(plain_home, sid), ie.sandbox_turns(boxed_home, sid)
    if plain["unsandboxed"] or plain["sandboxed"] or boxed["unsandboxed"] or ie.read_binding(plain_home, sid):
        rt.reason = "the session ran outside Gemini's sandbox at least once: notify-only"
        return rt
    if boxed["sandboxed"] < 1:
        rt.reason = "no sandboxed turn is recorded for this session: notify-only"
        return rt
    auth_hosts, why = gemini_auth(home, os.environ)
    if auth_hosts is None:
        rt.reason = why + ": notify-only"
        return rt
    found = gemini_project_config(real, home)
    if found:
        rt.reason = f"Gemini configuration changes its sandbox ({found}): notify-only"
        return rt
    ident = resolve_gemini_resume_ident(sid, real)
    if not ident or ident.startswith("-"):
        rt.reason = "Gemini cannot resume this session by id: notify-only"
        return rt
    rt.eligible, rt.reason, rt.cwd, rt.sandbox = True, "eligible", real, GEMINI_PROFILE
    rt.extra = {"ident": ident, "hosts": gemini_hosts(home, auth_hosts)}
    return rt


SANDBOX_EXEC = "/usr/bin/sandbox-exec"
GEMINI_SEATBELT_FILE = "seatbelt-restrictive-proxied.sb"   # cardinal_core's copy of Gemini's profile
GEMINI_INCLUDE_DIRS = 5
GEMINI_PROXY_URL = f"http://localhost:{GEMINI_PROXY_PORT}"


def proxy_script(home: Path) -> Path:
    return lib_dir(home) / "cardinal_core" / "allowlist_proxy.py"


def seatbelt_profile(home: Path) -> Path:
    return lib_dir(home) / "cardinal_core" / GEMINI_SEATBELT_FILE


def gemini_api_key(home: Path, environ: Mapping[str, str]) -> Optional[str]:
    """The user's GEMINI_API_KEY for the woken process (its sandbox denies
    reading any .env file and the keychain): from the waker's environment,
    else the GEMINI_API_KEY line of ~/.gemini/.env. Never logged or stored."""
    if environ.get("GEMINI_API_KEY"):
        return environ["GEMINI_API_KEY"]
    data = _read_small(Path(home) / ".gemini" / ".env") or b""
    for line in data.decode("utf-8", "replace").splitlines():
        m = re.match(r"^\s*(?:export\s+)?GEMINI_API_KEY\s*=\s*(.*?)\s*$", line)
        if m:
            value = m.group(1)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value or None
    return None


def gemini_prepare(home: Path, rt: Runtime, config: dict, log: Path, system: "System") -> Optional[str]:
    """Right before the spawn: this wake's private temp dir (the profile's
    TMP_DIR: private_wake_dir, removed at settle), a private cache dir inside
    it (the profile's CACHE_DIR, 0700; never the shared DARWIN_USER_CACHE_DIR,
    which other tools read later, unsandboxed), and the paths the argv names;
    why it cannot be prepared, else None."""
    try:
        # The profile grants writes under ~/.cache/cardinal/sandboxed only (not ~/.cache), so it
        # must exist before the sandbox starts.
        boxed = paths.sandboxed_home(paths.unsandboxed_home(Path(home)))
        boxed.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = private_wake_dir(home, log)
        cache = os.path.join(tmp, "cache")
        os.mkdir(cache, 0o700)
        os.chmod(cache, 0o700)
    except OSError as e:
        return f"no private temp directory ({type(e).__name__})"
    rt.extra.update(tmp_dir=tmp, cache_dir=cache, log=str(log), python=config.get("python"),
                    proxy=str(proxy_script(home)), profile=str(seatbelt_profile(home)),
                    home_dir=os.path.realpath(str(home)))
    return None


def gemini_argv(gemini_path: str, rt: Runtime, sid: str, seq: int) -> List[str]:
    """The pinned Gemini launch (prevention, not detection): the allow-list
    proxy binds the port exclusively, then runs
      sandbox-exec -D <params> -f <cardinal's restrictive-proxied profile>
        <gemini> --resume <ident> --approval-mode yolo --skip-trust -p <prompt>
    as its child, so the whole Gemini process is sandboxed from its first
    instruction. Gemini's own -s is NOT passed (a seatbelt cannot be nested:
    Gemini would re-exec sandbox-exec inside ours and fail); SANDBOX is set
    in its environment, as Gemini sets it inside its own sandbox, so it never
    tries. yolo acts inside that sandbox: the sandbox and the proxy are the
    boundary. Never --yolo / -y, --sandbox-image or --include-directories."""
    if not (isinstance(gemini_path, str) and os.path.isabs(gemini_path)):
        raise ValueError("gemini must be an absolute path")
    if not UUID_RE.fullmatch(sid or ""):
        raise ValueError(f"not a Gemini session id: {sid!r}")
    ident = rt.extra.get("ident")
    if not (isinstance(ident, str) and (UUID_RE.fullmatch(ident) or ident.isdigit())):
        raise ValueError(f"not a resume ident: {ident!r}")
    if rt.sandbox != GEMINI_PROFILE:
        raise ValueError(f"not the wake profile: {rt.sandbox!r}")
    x = rt.extra
    for k in ("tmp_dir", "cache_dir", "log", "python", "proxy", "profile", "home_dir"):
        if not (isinstance(x.get(k), str) and os.path.isabs(x[k])):
            raise ValueError(f"the launch is not prepared ({k})")
    if not (isinstance(rt.cwd, str) and os.path.isabs(rt.cwd)):
        raise ValueError("no working directory")
    hosts = [h for h in x.get("hosts") or [GEMINI_API_HOST]]
    params = ["TARGET_DIR=" + rt.cwd, "TMP_DIR=" + x["tmp_dir"], "HOME_DIR=" + x["home_dir"],
              "CACHE_DIR=" + x["cache_dir"]] + [f"INCLUDE_DIR_{i}=/dev/null" for i in range(GEMINI_INCLUDE_DIRS)]
    sandbox = [SANDBOX_EXEC] + [a for p in params for a in ("-D", p)] + ["-f", x["profile"]]
    return ([x["python"], "-I", "-S", x["proxy"], "--log", x["log"], "--port", str(GEMINI_PROXY_PORT), *hosts, "--"]
            + sandbox
            + [gemini_path, "--resume", ident, "--approval-mode", "yolo", "--skip-trust", "-p",
               wake_prompt(seq, x.get("investigation_id"))])


def gemini_env(environ: Mapping[str, str], config: dict, home: Path, log: Path, rt: Runtime) -> dict:
    """The woken Gemini's environment: child_env (the vetted PATH) without
    anything in GEMINI_SANDBOX_ENV (or a SEATBELT_* / GEMINI_SANDBOX* name);
    SANDBOX=sandbox-exec (the hooks keep their state under the relocated
    home, and Gemini does not start a sandbox of its own inside ours); the
    proxy in HTTPS_PROXY / HTTP_PROXY; TMPDIR = this wake's temp dir; the
    user's GEMINI_API_KEY (the sandbox cannot read ~/.gemini/.env)."""
    env = {k: v for k, v in child_env(environ, config.get("path_env"), home=home).items()
           if k not in GEMINI_SANDBOX_ENV and not k.startswith(GEMINI_SANDBOX_ENV_PREFIXES)}
    env["SANDBOX"] = paths.SEATBELT_SANDBOX
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        env[k] = GEMINI_PROXY_URL
    tmp = rt.extra.get("tmp_dir")
    if tmp:
        env["TMPDIR"] = env["TMP"] = env["TEMP"] = tmp
    if rt.extra.get("cache_dir"):
        env["XDG_CACHE_HOME"] = rt.extra["cache_dir"]   # the private CACHE_DIR, never ~/.cache
    key = gemini_api_key(home, environ)
    if key:
        env["GEMINI_API_KEY"] = key
    return env


def gemini_preflight(home: Path, config: dict, system: "System", rt: Optional[Runtime] = None) -> Optional[str]:
    """Why a Gemini launch cannot start now (a notification instead), or None."""
    if not proxy_script(home).is_file() or not seatbelt_profile(home).is_file():
        return "the allow-list proxy or the seatbelt profile is not installed (run cardinal-wake enable again)"
    if not os.access(SANDBOX_EXEC, os.X_OK):
        return "sandbox-exec is not available"
    if system.port_busy(GEMINI_PROXY_PORT):
        return f"port {GEMINI_PROXY_PORT} is in use, so the allow-list proxy cannot start"
    return None


_SEATBELT_RE = re.compile(r"(?:^|\s)-f\s+(\S*" + re.escape(GEMINI_PROFILE) + r"\S*\.sb)(?:\s|$)")


def _descendants(rows: list, pid: int) -> List[Tuple[int, int, str]]:
    children: dict = {}
    for row in rows:
        children.setdefault(row[1], []).append(row)
    out, todo, seen = [], [pid], {pid}
    while todo:
        for row in children.get(todo.pop(), []):
            if row[0] not in seen:
                seen.add(row[0])
                out.append(row)
                todo.append(row[0])
    return out


def seatbelt_observed(rows: list, pid: int) -> bool:
    """`pid`'s process tree runs sandbox-exec with the wake profile."""
    for _, _, args in [r for r in rows if r[0] == pid] + _descendants(rows, pid):
        first = args.split(None, 1)[0] if args.strip() else ""
        if os.path.basename(first) == "sandbox-exec" and _SEATBELT_RE.search(args):
            return True
    return False


def gemini_verify_spawn(home: Path, sid: str, pid: int, system: "System") -> Optional[str]:
    """Defense in depth (the argv already starts the sandbox): None once
    `pid`'s tree runs sandbox-exec -f <...restrictive-proxied...>, or when
    the process has already exited (a quick exit is judged after the run,
    never by this check); else, after SPAWN_VERIFY_WINDOW, the whole tree is
    killed and the reason returned (the caller disables Gemini launch)."""
    deadline = system.now() + SPAWN_VERIFY_WINDOW
    seen: List[Tuple[int, int, str]] = []
    while True:
        rows = system.process_rows()
        if rows:
            seen = [r for r in rows if r[0] == pid] + _descendants(rows, pid) or seen
            if seatbelt_observed(rows, pid):
                return None
        if not system.alive(pid):
            return None
        if system.now() >= deadline:
            break
        system.sleep(SPAWN_VERIFY_POLL)
    for victim in [pid] + [r[0] for r in seen if r[0] != pid]:
        with contextlib.suppress(OSError):
            system.kill(victim, signal.SIGKILL)
    with contextlib.suppress(OSError):
        system.kill(-pid, signal.SIGKILL)   # its process group (it was started in its own session)
    return (f"Gemini's {GEMINI_PROFILE} seatbelt sandbox was not observed within {int(SPAWN_VERIFY_WINDOW)} s; "
            "the woken process tree was killed")


def gemini_snapshot(home: Path, sid: str, rt: Optional[Runtime] = None) -> dict:
    plain_home, boxed_home = state_homes(home)
    plain, boxed = ie.sandbox_turns(plain_home, sid), ie.sandbox_turns(boxed_home, sid)
    return {"sandboxed": boxed["sandboxed"], "unsandboxed": plain["unsandboxed"] + boxed["unsandboxed"]
            + plain["sandboxed"]}


def gemini_verify_after(home: Path, sid: str, before: dict, pinned: str, *, require_turn: bool = True) -> Optional[str]:
    """None when the woken run recorded only sandboxed turns (and, with
    require_turn, at least one). The record is agent-writable: a hint
    beside the runtime check, never the boundary."""
    now = gemini_snapshot(home, sid)
    if now["unsandboxed"] > int(before.get("unsandboxed") or 0):
        return "the woken run recorded a turn outside Gemini's sandbox"
    if require_turn and now["sandboxed"] <= int(before.get("sandboxed") or 0):
        return "the woken run recorded no sandboxed turn, so its sandbox could not be confirmed"
    return None


class Launcher:
    """How the waker may launch one runtime once all three gates of §6 are
    demonstrated for it (cannot arm itself, sandbox enforced on resume, one
    execution per decision). Everything runtime-specific about a launch:

      executable: the config key of its absolute path (resolved on PATH at
        enable time; `binary` is the name looked up);
      eligibility(home, sid, rt, config) -> rt with eligible / reason / cwd
        / sandbox / extra;
      prepare(home, rt, config, log, system) -> None, or why the launch
        cannot be prepared (right before the spawn);
      argv(executable, rt, sid, seq) -> the pinned argv (golden-tested);
      env(environ, config, home, log, rt) -> the child's environment (PATH
        is always the vetted path_env);
      preflight(home, config, system, rt) -> None, or why it cannot start now
        (a notification);
      exclusive: at most one in-flight launch of this runtime machine-wide;
      snapshot(home, sid, rt) -> what verify needs from before the launch;
      after_spawn(home, sid, pid, system) -> None, or why the started child
        is not in its sandbox (it has been killed; the runtime is disabled);
      verify(home, sid, snapshot, pinned, require_turn) -> None, or why the
        woken run did not stay in bounds (that disables the runtime:
        ~/.cardinal/wake/disabled/<runtime>);
      proven: False keeps the launcher off (its sessions notify-only).

    Adding a runtime is one Launcher in LAUNCHERS; it launches only once
    `proven` is True, after its gates are demonstrated."""

    def __init__(self, runtime: str, binary: str, eligibility, argv, snapshot, verify, can_launch: str, *,
                 env=None, preflight=None, after_spawn=None, prepare=None, exclusive: bool = False,
                 proven: bool = True):
        self.runtime, self.binary = runtime, binary
        self.executable = f"{runtime}_path"
        self.eligibility, self.argv, self.snapshot, self.verify = eligibility, argv, snapshot, verify
        self.can_launch = can_launch
        self.env = env or (lambda environ, config, home, log, rt: child_env(environ, config.get("path_env")))
        self.preflight = preflight or (lambda home, config, system, rt: None)
        self.after_spawn = after_spawn or (lambda home, sid, pid, system: None)
        self.prepare = prepare or (lambda home, rt, config, log, system: None)
        self.exclusive, self.proven = exclusive, proven


GEMINI_LAUNCH_TEXT = (
    "Gemini CLI sessions in an approved root (cardinal-wake enable --root PATH) that only ever ran in Gemini's "
    "sandbox, resumed with -s in the restrictive-proxied seatbelt profile behind an allow-list proxy.")

# Codex launch is built and shipped OFF (CODEX_LAUNCH_PROVEN), like Gemini.
# Its lockdown and sandbox gates passed (§6 C-1..C-5, C-8, C-15..C-21), but
# gate 3 (duplicate decisions cannot start concurrent executions) rests on
# the survivor check, and two cases of that could not be demonstrated live
# in this session (§6 C-23, C-24: the TMPDIR writable-root probe against a
# process under Codex's real seatbelt, and against an env-cleared setsid +
# chdir Python survivor). Until an owner verifies both and flips this
# constant in a reviewed change (no runtime flag), every Codex wake is
# notify-only. The notify path, wake intent and session.wake reports are
# all active regardless.
CODEX_LAUNCH_PROVEN = False

# The runtimes that may be launched unattended, an explicit allow-list.
# Gemini CLI's launch is built and off (GEMINI_LAUNCH_PROVEN) until its live
# gates pass. Claude Code did not pass (C-6) and Cursor has no demonstrated
# launch: notify-only.
LAUNCHERS = {
    CODEX: Launcher(CODEX, "codex", lambda home, sid, rt, config: codex_eligibility(home, sid, rt),
                    lambda exe, rt, sid, seq: codex_argv(exe, rt.sandbox or "", sid, seq,
                                                         rt.extra.get("mcp_overrides") or (),
                                                         rt.extra.get("investigation_id", "")),
                    rollout_offsets, verify_after, LAUNCH_TEXT, preflight=codex_lockdown_problem,
                    prepare=codex_prepare, proven=CODEX_LAUNCH_PROVEN,
                    env=lambda environ, config, home, log, rt: child_env(
                        environ, config.get("path_env"), home=home, tmpdir=rt.extra.get("tmp_dir"),
                        extra=CODEX_AUTH_ENV)),
    GEMINI: Launcher(GEMINI, "gemini", gemini_eligibility, gemini_argv, gemini_snapshot, gemini_verify_after,
                     GEMINI_LAUNCH_TEXT, env=gemini_env, preflight=gemini_preflight, prepare=gemini_prepare,
                     after_spawn=gemini_verify_spawn, exclusive=True, proven=GEMINI_LAUNCH_PROVEN),
}


def active_launcher(runtime: str) -> Optional[Launcher]:
    """The runtime's launcher when it may launch (proven), else None."""
    launcher = LAUNCHERS.get(runtime)
    return launcher if launcher is not None and launcher.proven else None


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------

def live_signals(home: Path, sid: str, rt: Runtime, system: System, now: float,
                 ledger: Optional[dict] = None, state: Optional[Path] = None) -> List[str]:
    """Why the session may still be running (empty: it is stopped). The
    poller status and .active of both state homes count (state: unused,
    kept for callers; any home saying live is enough)."""
    why = []
    led = ledger if ledger is not None else read_ledger(home, sid)
    if any(isinstance(e.get("child"), dict) and child_alive(e["child"], system) for e in led["entries"].values()):
        why.append("a run the waker started is still going")
    homes = state_homes(home)
    for h in homes:
        st = poller.read_status(h, sid)
        anchor = st.get("anchor")
        if isinstance(anchor, int) and not isinstance(anchor, bool) and anchor > 1 and system.alive(anchor):
            started = system.process_start(anchor)
            try:
                written = poller.status_path(h, sid).stat().st_mtime
            except OSError:
                written = None
            if started is None or written is None or started < written:
                why.append("its agent process is running")
                break
    table = system.process_table()
    if table is None:
        why.append("the process table could not be read")
    elif any(sid in args for pid, args in table if pid != os.getpid()):
        why.append("a running process names the session")
    for p in rt.files:
        with contextlib.suppress(OSError):
            if now - p.stat().st_mtime <= LIVE_WINDOW:
                why.append("its transcript changed in the last 2 minutes")
                break
    if any(now - ie.last_activity(h, sid) <= LIVE_WINDOW for h in homes):
        why.append("it used a tool in the last 2 minutes")
    # Gate 3 (review 6): a process any earlier wake of this session left behind is this session's,
    # whatever its environment or group, iff the sandbox grants it that wake's TMPDIR. The session
    # is not stopped while one runs. The TMPDIRs of its in-flight and recently-finished entries are
    # the ones still on disk (a settled entry's was removed once its sweep found none).
    tmps = [e.get("tmp_dir") for e in led["entries"].values()
            if isinstance(e.get("tmp_dir"), str) and e.get("runtime") == CODEX]
    if tmps:
        found = system.sandboxed_procs(0.0, tmps, str(wake_tmp_root(home)))
        if found is None:
            why.append("its sandboxed processes could not be checked")
        elif found:
            why.append("a process of an earlier wake is still running")
    return why


def child_alive(child: dict, system: System) -> bool:
    pid = child.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or not system.alive(pid):
        return False
    recorded = _num(child.get("start"))
    if recorded is None:
        return True
    now_start = system.process_start(pid)
    return now_start is None or abs(now_start - recorded) <= 2.0


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

def state_homes(home: Path) -> Tuple[Path, Path]:
    """Where hooks keep per-session Cardinal state: the user's home
    (~/.cardinal) and the relocated home of sessions run inside a seatbelt
    sandbox (paths.sandboxed_home). Both hold agent-writable hints."""
    home = paths.unsandboxed_home(Path(home))
    return home, paths.sandboxed_home(home)


def candidate_sessions(home: Path, now: float) -> List[Tuple[str, dict, float, Path]]:
    """(sid, binding, binding mtime, state home) of sessions that may be
    woken: touched within CANDIDATE_WINDOW, the investigation's author,
    owner_decision on. A session bound under ~/.cardinal is decided there
    alone, stale or not (the relocated binding, which a sandboxed agent can
    write, never shadows it). Relocated bindings count only for sessions
    with no Codex or Claude Code records (whose hooks never run in a
    seatbelt sandbox), and are as untrusted as any binding."""
    out, seen = [], set()
    plain, boxed = state_homes(home)
    for state in (plain, boxed):
        d = ie.sessions_dir(state)
        try:
            names = os.listdir(str(d))
        except OSError:
            continue
        for name in sorted(names):
            sid = name[:-5] if name.endswith(".json") else ""
            if not ie.valid_session(sid) or sid in seen:
                continue
            seen.add(sid)   # before any other check: a stale real binding is never replaced by a relocated one
            if state == boxed and (codex_rollouts(home, sid) or claude_transcripts(home, sid)):
                continue
            try:
                mtime = (d / name).stat().st_mtime
            except OSError:
                continue
            if now - mtime > CANDIDATE_WINDOW:
                continue
            b = ie.read_binding(state, sid)
            if b is None or b.get("is_author") is not True or not ie.capability(b, "owner_decision"):
                continue
            out.append((sid, b, mtime, state))
    return out


def read_from(binding: dict) -> int:
    """Where the session's unconsumed events start: its cursor, or before
    the oldest decision it was shown and has not acknowledged."""
    seqs = [p["seq"] for p in binding.get("pending_decisions") or []
            if isinstance(p, dict) and isinstance(p.get("seq"), int) and not isinstance(p.get("seq"), bool)]
    return max(0, min([int(binding["cursor"])] + [s - 1 for s in seqs]))


WAKE_NOT_REQUESTED = "decision posted without resume requested"


def decision_requests_wake(e: Any) -> bool:
    """The owner asked for the session to be resumed (conductor#2110: the
    Composer's "Resume this session if it has stopped" sets payload.wake,
    which the server allows only with to_session_id). Exactly the JSON true:
    a missing, false, "true" or 1 wake is a notification only."""
    p = e.get("payload") if isinstance(e, dict) else None
    return isinstance(p, dict) and p.get("wake") is True


def wake_decisions(events: list, sid: str, binding: dict) -> List[dict]:
    """The decisions in `events` that may wake `sid` (§5.2, before the
    ledger, caps, liveness and eligibility): verified, addressed to it, and
    still deliverable (unacknowledged by it, owner_decision on)."""
    inv = binding["investigation_id"]
    out = []
    for e in ie.deliverable(events, sid, inv, binding):
        seq = e.get("seq")
        if ie.verified_owner_decision(e) and e.get("to_session_id") == sid and isinstance(seq, int) \
                and not isinstance(seq, bool) and seq > 0:
            out.append(e)
    return sorted(out, key=lambda e: e["seq"])


def created_epoch(e: dict) -> Optional[float]:
    """The server's created_at of an event, as epoch seconds, or None."""
    import calendar
    v = e.get("created_at")
    if not isinstance(v, str):
        return None
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?Z", v)
    if not m:
        return None
    try:
        return float(calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


DISPOSITIONS = ("accepted", "declined", "noted", "conflicts")


def acknowledged(events: list, sid: str, seq: int) -> Optional[str]:
    """The disposition (accepted / declined / noted / conflicts) with which
    this session acknowledged #seq, or None. Only an acknowledgment by the
    investigation author's own principal naming this session counts: never
    a grantee's, never one carrying granted_by. (An agent holding the
    author's key could still post one in this session's name: an
    acknowledgment reports what the session said, not proof it acted.)"""
    for e in events:
        if not (isinstance(e, dict) and ie.event_class(e) == ie.CONTROL and e.get("type") == ie.ACKNOWLEDGED
                and ie._own(e, sid)):
            continue
        producer = e.get("producer") if isinstance(e.get("producer"), dict) else {}
        if producer.get("granted_by"):
            continue
        p = e.get("payload")
        if isinstance(p, dict) and p.get("ack_of") == seq:
            d = p.get("disposition")
            return d if d in DISPOSITIONS else "acknowledged"
    return None


def _connection_for(home: Path, binding: dict, prefer: str, system: System) -> Tuple[dict, str]:
    """The plugin connection whose org is the binding's: the session's own
    runtime first. ({}, "") when none matches."""
    order = [prefer] + [r for r in RUNTIMES if r != prefer] if prefer in RUNTIMES else list(RUNTIMES)
    for runtime in order:
        conn = system.connection(runtime, home) or {}
        if not (conn.get("origin") and conn.get("org") and conn.get("key")):
            continue
        if binding.get("org") in (None, conn.get("org")):
            return conn, runtime
    return {}, ""


def _client(runtime: str, config: dict) -> str:
    """The X-Cardinal-Client of the plugin whose key reads: <name>/<version
    of that plugin when it ran enable>, the name alone otherwise."""
    version = (config.get("versions") or {}).get(runtime)
    from .evidence import client_string
    try:
        return client_string("claude-plugin" if runtime == CLAUDE else runtime, version)
    except ValueError:
        return "cardinal-wake"


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------

def _where(binding: Optional[dict], origin: Optional[str]) -> str:
    """'inv_… on <host>': built only from the investigation id and the
    plugin connection's origin, never from a link the (agent-writable)
    binding carries."""
    import urllib.parse
    inv = (binding or {}).get("investigation_id")
    if not ie.valid_investigation(inv):
        return ""
    host = ""
    if isinstance(origin, str) and origin.startswith("https://"):
        h = (urllib.parse.urlsplit(origin).hostname or "").lower()
        host = h if re.fullmatch(r"[a-z0-9.-]{1,253}", h) else ""
    return f"{inv} on {host}" if host else inv


def _notify(system: System, binding: Optional[dict], sid: str, state: str, words: str,
            runtime: str = UNKNOWN, origin: Optional[str] = None) -> None:
    """The runtime, the session's short id, the investigation and the state;
    never decision text."""
    where = _where(binding, origin)
    label = LABELS.get(runtime, LABELS[UNKNOWN])
    msg = f"{label} session {sid[:8]}: {state.lower()} - {words}" + (f" ({where})" if where else "")
    with contextlib.suppress(Exception):
        system.notifier("Cardinal wake", msg[:400])


def _report(system: System, report: list, sid: str, seq: Optional[int], action: str, why: str = "") -> None:
    report.append({"session_id": sid, "seq": seq, "action": action, "reason": why})
    system.out(f"{sid[:8]} #{seq if seq is not None else '-'} {action}" + (f": {why}" if why else ""))


def run_once(home: Path, system: Optional[System] = None, *, dry_run: bool = False) -> List[dict]:
    """One pass of the waker (see the module docstring). dry_run: reads
    Cardinal and reports what it would do; writes, notifies and launches
    nothing. Returns the report (one dict per action)."""
    system = system or System()
    report: list = []
    config = read_config(home)
    for sid in ledger_sids(home):
        try:
            settle(home, sid, system, config, report, dry_run=dry_run)
        except Exception as e:  # one broken ledger never stops the pass
            _report(system, report, sid, None, "error", f"settling failed ({type(e).__name__})")
    if not dry_run:
        with contextlib.suppress(OSError):
            prune_logs(home)
    if not config["enabled"]:
        if not dry_run:
            report_all_wake_states(home, system, config)
        _report(system, report, "-", None, "skip", "wake is not enabled (cardinal-wake enable)")
        return report
    now = system.now()
    polls = _read_json(poll_state_path(home))
    for sid, binding, mtime, state in candidate_sessions(home, now):
        if now - mtime > RECENT_WINDOW and 0 <= now - (_num(polls.get(sid)) or 0.0) < OLD_CHECK_EVERY:
            continue
        polls[sid] = now
        try:
            consider(home, sid, binding, system, config, report, dry_run=dry_run, state=state)
        except Exception as e:
            _report(system, report, sid, None, "error", f"{type(e).__name__}")
    if not dry_run:
        with contextlib.suppress(OSError, ValueError):
            _private_dir(wake_dir(home))
            ie.write_json(poll_state_path(home), {k: v for k, v in polls.items()
                                                   if isinstance(v, (int, float)) and now - v < CANDIDATE_WINDOW})
        report_all_wake_states(home, system, config)
    return report


# ---------------------------------------------------------------------------
# session.wake reports (conductor#2110)
# ---------------------------------------------------------------------------

WAKE_REPORT_STATES = {NOTIFIED: "notified", CLAIMED: "claimed", RESUMING: "resuming", TIMED_OUT: "timed_out",
                      ACKNOWLEDGED: "acknowledged", FAILED: "failed"}   # ACK_RELAYED: no report
WAKE_REPORT_TRIES = 3
WAKE_RUNTIMES = ("codex", "claude", "gemini", "cursor")
# The only reasons a report may carry: readable by every org member and read
# grantee, so a fixed vocabulary, never the waker's own words (which can name
# paths under $HOME, binaries or config), decision text, an ack note or a key.
# (substring of the history note, reason), first match wins.
_WAKE_REASON_RULES = (
    (WAKE_NOT_REQUESTED, WAKE_NOT_REQUESTED),
    ("older than 7 days", "decision older than 7 days"),
    ("the session is live", "session is live"),
    ("unattended launch is off", "unattended launch is off"),
    ("launch is disabled", "launch is disabled on this machine"),
    ("its own Codex config", "repository has its own Codex config"),
    ("changed since cardinal-wake enable", "launch binaries changed since enable"),
    ("not vetted", "launch binaries not vetted"),
    ("lockdown", "lockdown could not be verified"),
    ("MCP servers", "lockdown could not be verified"),
    ("features on that", "lockdown could not be verified"),
    ("ended without acknowledging", "no acknowledgment"),
    ("acknowledgment could not be read", "acknowledgment could not be read"),
    ("could not prepare", "launch failed"),
    ("could not start", "launch failed"),
    ("stopped during the launch", "launch interrupted"),
    ("still running after 2 h", "still running after 2 hours"),
    ("relayed by the waker", "acknowledgment relayed by the waker"),
    ("acknowledged the decision", "acknowledged"),
    ("resumed in", "resumed"),
    ("recovered", "resumed"),
    ("notify-only", "session cannot be resumed unattended"),
)
WAKE_REASONS = frozenset(r for _, r in _WAKE_REASON_RULES) | {"session cannot be resumed unattended", "failed"}


def wake_reason(state: str, note: Any) -> Optional[str]:
    """The fixed-vocabulary reason for a report of `state`, from the waker's
    history note (never the note itself), or None."""
    text = note if isinstance(note, str) else ""
    for needle, reason in _WAKE_REASON_RULES:
        if needle in text:
            return reason
    if state == NOTIFIED:
        return "session cannot be resumed unattended"
    if state == FAILED:
        return "failed"
    return None


def wake_report_key(investigation_id: str, sid: str, seq: int, state: str) -> str:
    """wake:<sha256(investigation:sid:seq)[:40]>:<state>: one report per
    decision, session and state (A-Za-z0-9._:-, at most 128, never a
    server-reserved prefix)."""
    import hashlib
    h = hashlib.sha256(f"{investigation_id}:{sid}:{seq}".encode()).hexdigest()[:40]
    return f"wake:{h}:{state}"


def report_all_wake_states(home: Path, system: System, config: dict) -> None:
    for sid in ledger_sids(home):
        with contextlib.suppress(Exception):   # reporting never stops or changes a pass
            report_wake_states(home, sid, system, config)


def report_wake_states(home: Path, sid: str, system: System, config: dict) -> None:
    """Post the session.wake reports this session's entries still owe
    (best effort): one per lifecycle state an entry reached (NOTIFIED,
    CLAIMED, RESUMING, TIMED_OUT, ACKNOWLEDGED, FAILED), in order, only for
    entries this version created, only when the binding advertises
    session_wake, with the plugin connection whose org is the binding's.
    Never changes the lifecycle. An older server (400 unknown_event_type),
    a missing route (404), a refusal (403) or the limit (409
    event_limit_reached) stops reports for that investigation (recorded in
    the ledger); a decision the server cannot match (404
    wake_decision_not_found, 422, 400 invalid_event) ends that entry's
    reports; 409 idempotency_conflict counts as reported; anything else is
    retried on later passes, WAKE_REPORT_TRIES times per entry."""
    with session_lock(home, sid, wait=0.5) as got:
        if not got:
            return
        led = read_ledger(home, sid)
        off = led.get("wake_reports_off") if isinstance(led.get("wake_reports_off"), dict) else {}
        plain_home, boxed_home = state_homes(home)
        changed = False
        for entry in led["entries"].values():
            reports = entry.get("reports")
            inv, seq = entry.get("investigation_id"), entry.get("seq")
            if not isinstance(reports, list) or entry.get("reports_ended") or inv in off \
                    or not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
                continue
            states = []
            for line in entry.get("history") or []:
                st = WAKE_REPORT_STATES.get(line.get("state")) if isinstance(line, dict) else None
                if st and st not in reports and st not in [s for s, _ in states]:
                    states.append((st, wake_reason(line.get("state"), line.get("note"))))
            if not states:
                continue
            binding = ie.read_binding(boxed_home if entry.get("sandboxed_state") else plain_home, sid)
            if not binding or binding.get("investigation_id") != inv or not ie.capability(binding, "session_wake"):
                continue
            runtime = entry.get("runtime") if entry.get("runtime") in WAKE_RUNTIMES else "unknown"
            conn, conn_runtime = _connection_for(home, binding, runtime, system)
            if not conn:
                continue
            client = _client(conn_runtime, config)
            for st, reason in states:
                payload = {"decision_seq": seq, "state": st, "runtime": runtime}
                if reason in WAKE_REASONS:
                    payload["reason"] = reason
                changed = True
                try:
                    system.post_wake(conn, inv, sid, payload, wake_report_key(inv, sid, seq, st), client)
                except sync.ServerError as e:
                    body = e.body if isinstance(getattr(e, "body", None), dict) else {}
                    err, status = body.get("error"), getattr(e, "status", None)
                    if err == "idempotency_conflict":
                        reports.append(st)
                        continue
                    if err in ("unknown_event_type", "event_limit_reached") or status == 403 \
                            or (status == 404 and err != "wake_decision_not_found"):
                        off[inv] = str(err or status)[:60]
                        break
                    if status in (400, 404, 422):
                        entry["reports_ended"] = str(err or status)[:60]
                        break
                    entry["report_tries"] = int(entry.get("report_tries") or 0) + 1
                    break
                except Exception:
                    entry["report_tries"] = int(entry.get("report_tries") or 0) + 1
                    break
                reports.append(st)
            if int(entry.get("report_tries") or 0) >= WAKE_REPORT_TRIES and not entry.get("reports_ended"):
                entry["reports_ended"] = "gave up after retries"
        if changed:
            led["wake_reports_off"] = off
            write_ledger(home, sid, led, system.now())


def consider(home: Path, sid: str, binding: dict, system: System, config: dict, report: list, *,
             dry_run: bool = False, state: Optional[Path] = None) -> None:
    """One candidate session (§5.2). state: the home its binding was found
    under (state_homes); the waker's own files always live under `home`."""
    state = state or home
    now = system.now()
    led = read_ledger(home, sid)
    if in_flight(led):
        return  # one run per session: a decision posted now is delivered inside that run
    if 0 <= now - _last_finished(led) < LIVE_WINDOW:
        return  # the run just ended; its own writes would read as a live session
    rt = runtime_kind(home, sid)
    conn, conn_runtime = _connection_for(home, binding, rt.kind, system)
    if not conn:
        return
    inv = binding["investigation_id"]
    try:
        events, complete = system.fetch(conn, inv, sid, read_from(binding), _client(conn_runtime, config))
    except Exception as e:
        _report(system, report, sid, None, "skip", f"could not read the investigation ({type(e).__name__})")
        return
    if not complete:
        _report(system, report, sid, None, "skip", "the investigation's events could not all be read this pass")
        return
    pending = sorted((e for e in wake_decisions(events, sid, binding) if not decision_seen(led, inv, e["seq"])),
                     key=lambda e: e["seq"])
    if not pending:
        return
    launcher = active_launcher(rt.kind)   # None: notify-only (no launch has passed, or is proven for, its gates)
    # Review 7 F2: handle decisions in ascending seq and stop at the first one that is launched or
    # cannot be claimed this pass. A notification records a ledger entry (which raises the
    # high-water mark), so an old or no-wake decision must never be notified ahead of a lower
    # pending wake decision that has no entry yet, or that wake decision would be skipped for good.
    for e in pending:
        seq = e["seq"]
        created = created_epoch(e)
        if created is None or now - created > DECISION_MAX_AGE:
            _notify_only(home, sid, binding, seq, rt.kind, "the decision is older than 7 days (or undated): "
                         "post a new one to resume the session", system, report, dry_run=dry_run,
                         origin=conn.get("origin"))
            continue   # an old decision records an entry; a lower pending seq was handled first
        if not decision_requests_wake(e):   # conductor#2110: only a decision that asks for it may resume
            _notify_only(home, sid, binding, seq, rt.kind, WAKE_NOT_REQUESTED, system, report,
                         dry_run=dry_run, origin=conn.get("origin"))
            continue
        # A wake decision: at most one run per session, so attempt this lowest one and stop. Higher
        # pending decisions wait for a later pass (or are delivered inside the run), so the
        # high-water mark never passes this seq while it is unhandled.
        if launcher is not None:
            rt = launcher.eligibility(home, sid, rt, config)
        # The probes (preflight) run once, under the session lock right before the spawn
        # (claim_and_launch re-checks everything there); a dry run runs them here.
        why = launch_problem(home, sid, rt, launcher, config, system, now, led, state, probes=dry_run)
        if why:
            _notify_only(home, sid, binding, seq, rt.kind, why, system, report, dry_run=dry_run,
                         origin=conn.get("origin"))
        else:
            claim_and_launch(home, sid, binding, seq, rt, config, system, report, dry_run=dry_run,
                             state=state, origin=conn.get("origin"))
        break


def launch_problem(home: Path, sid: str, rt: Runtime, launcher: Optional["Launcher"], config: dict, system: System,
                   now: float, led: dict, state: Path, *, probes: bool = True) -> str:
    """Why `sid` may not be launched now ("" when it may): live, not eligible
    (rt is the launcher's fresh eligibility), launch off or disabled, the
    vetted executables changed, or (probes) the launcher's preflight
    (Codex: the features / mcp list probes in the launch's cwd)."""
    live = live_signals(home, sid, rt, system, now, led, state=state)
    if live:
        return "the session is live (" + "; ".join(live) + "): it will see the decision itself"
    if launcher is None or not rt.eligible:
        return rt.reason
    if not config["launch"]:
        return "unattended launch is off (cardinal-wake enable --launch)"
    if disabled_reason(home, rt.kind):
        return (f"{LABELS[rt.kind]} launch is disabled: {disabled_reason(home, rt.kind)} "
                f"(cardinal-wake reenable {rt.kind})")
    if not (config.get(launcher.executable) and os.access(config[launcher.executable], os.X_OK)):
        return f"{launcher.binary} was not found when wake was enabled (run cardinal-wake enable --launch again)"
    return (binaries_problem(config, home, rt.cwd) or (launcher.preflight(home, config, system, rt) if probes
                                                      else None) or "")


def _notify_only(home, sid, binding, seq, kind, why, system, report, *, dry_run=False, origin=None) -> None:
    if dry_run:
        _report(system, report, sid, seq, "would notify", why)
        return
    now = system.now()
    inv = binding["investigation_id"]
    with session_lock(home, sid) as got:
        if not got:
            return
        led = read_ledger(home, sid)
        if decision_seen(led, inv, seq):
            return
        entry = {"seq": seq, "investigation_id": inv, "runtime": kind, "reports": []}
        transition(entry, DECISION_SEEN, now)
        transition(entry, NOTIFIED, now, why)
        add_entry(led, entry)
        if isinstance(led.get("probe_retries"), dict):   # this seq is settled now; drop any retry counter
            led["probe_retries"].pop(entry_key(inv, seq), None)
        write_ledger(home, sid, led, now)
    _notify(system, binding, sid, NOTIFIED, f"owner decision #{seq} waits for you ({why.split(':')[0]})", kind,
            origin)
    _report(system, report, sid, seq, "notified", why)


def wake_hold(home: Path, sid: str, state: Path) -> None:
    """Before a launch: hold the session's local evidence (evidence.set_hold,
    reason "wake"; resume-and-wake.md §4) so the woken run's captures
    survive size eviction while space allows. Only the user's own spool
    (~/.cardinal/evidence) and only an existing session directory; never a
    relocated sandboxed spool, which the agent can write (the waker writes
    nothing there). Nothing while capture is off. Fail open."""
    from . import evidence
    try:
        if paths.is_sandboxed_home(Path(state)):
            return
        root = Path(paths.unsandboxed_home(Path(home))) / ".cardinal" / "evidence"
        if evidence.capture_disabled(root):
            return
        evidence.set_hold(root, sid, reason="wake")
    except Exception:
        return


def claim_and_launch(home: Path, sid: str, binding: dict, seq: int, rt: Runtime, config: dict, system: System,
                     report: list, *, dry_run: bool = False, state: Optional[Path] = None,
                     origin: Optional[str] = None) -> str:
    """Claim #seq for `sid` and launch its runtime's pinned resume
    (LAUNCHERS, proven; only an eligible session of such a runtime gets
    here), under the session's lock (§5.4). The CLAIMED entry (with the
    pre-launch snapshot) is written before the spawn. Returns launched |
    failed | seen | in_flight | runtime_busy | cap | busy | dry_run."""
    launcher = LAUNCHERS[rt.kind]
    if not launcher.proven:
        raise ValueError(f"{rt.kind} launch is not proven")
    if not rt.eligible:
        raise ValueError("not an eligible session")
    if dry_run:
        _report(system, report, sid, seq, "would launch", f"in {rt.sandbox}, cwd from the session's own record")
        return "dry_run"
    inv = binding["investigation_id"]
    if system.before_claim is not None:
        system.before_claim(sid, seq)
    with session_lock(home, sid) as got:
        if not got:
            return "busy"
        now = system.now()
        led = read_ledger(home, sid)
        if decision_seen(led, inv, seq):
            return "seen"
        if in_flight(led):
            return "in_flight"
        # Review 3 #2 (TOCTOU): everything consider() checked is checked again here, under the
        # lock, right before the spawn: the runtime records, the full eligibility (the project
        # config walk, the Codex config scan, the turns), liveness, the vetted executables, and
        # the probes in the launch's cwd. The launch uses this fresh view, never the earlier one.
        fresh = runtime_kind(home, sid)
        if fresh.kind == rt.kind:
            fresh = launcher.eligibility(home, sid, fresh, config)
            why = launch_problem(home, sid, fresh, launcher, config, system, now, led, state or home)
        else:
            why = f"the session's runtime changed ({fresh.kind}) before the launch"
        if why:
            # Review 6: a transient probe failure (codex could not be run to validate the lockdown
            # or list its MCP servers) must not consume the decision. Leave it unclaimed, with a
            # bounded retry counter in the ledger (no entry, so the high-water mark is untouched and
            # the decision is reconsidered next pass), up to PROBE_RETRY_PASSES times.
            if _transient_probe(why):
                tries = led.setdefault("probe_retries", {})
                key = entry_key(inv, seq)
                n = int(tries.get(key) or 0) + 1
                if n < PROBE_RETRY_PASSES:
                    tries[key] = n
                    write_ledger(home, sid, led, now)
                    _report(system, report, sid, seq, "retry", f"{why} (attempt {n}; reconsidered next pass)")
                    return "retry"
                tries.pop(key, None)
            entry = {"seq": seq, "investigation_id": inv, "runtime": rt.kind, "reports": []}
            transition(entry, DECISION_SEEN, now)
            transition(entry, NOTIFIED, now, why)
            add_entry(led, entry)
            write_ledger(home, sid, led, now)
            _notify(system, binding, sid, NOTIFIED, f"owner decision #{seq} waits for you ({why.split(':')[0]})",
                    rt.kind, origin)
            _report(system, report, sid, seq, "notified", why)
            return "notified"
        led.get("probe_retries", {}).pop(entry_key(inv, seq), None)
        rt = fresh
        with global_lock(home) as gotg:
            if not gotg:
                return "busy"
            if launcher.exclusive and runtime_in_flight(home, rt.kind):
                _report(system, report, sid, seq, "waiting", f"another {LABELS[rt.kind]} wake is running")
                return "runtime_busy"   # one at a time machine-wide (Gemini: the proxy's port); reconsidered later
            if _claims(led, now) >= SESSION_DAILY_CAP or global_claims(home, now) >= GLOBAL_DAILY_CAP:
                capped = _num(led.get("cap_notified_at"))
                if capped is None or now - capped >= DAY:
                    led["cap_notified_at"] = now
                    write_ledger(home, sid, led, now)
                    _notify(system, binding, sid, "waiting", f"owner decision #{seq}: the daily wake cap is reached",
                            rt.kind, origin)
                _report(system, report, sid, seq, "capped", "the daily wake cap is reached; reconsidered later")
                return "cap"
            entry = {"seq": seq, "investigation_id": inv, "runtime": rt.kind, "sandbox": rt.sandbox, "reports": [],
                     "claimed_at": now, "spawn_errors": 0, "sandboxed_state": paths.is_sandboxed_home(state or home),
                     "snapshot": launcher.snapshot(home, sid, rt)}
            transition(entry, DECISION_SEEN, now)
            transition(entry, CLAIMED, now)
            add_entry(led, entry)
            write_ledger(home, sid, led, now)
        wake_hold(home, sid, state or home)
        log = logs_dir(home) / f"{sid}-{seq}-{int(now)}.log"
        rt.extra["investigation_id"] = inv
        problem = launcher.prepare(home, rt, config, log, system)
        if rt.extra.get("tmp_dir"):
            entry["tmp_dir"] = rt.extra["tmp_dir"]
            write_ledger(home, sid, led, now)
        argv = None if problem else launcher.argv(config.get(launcher.executable), rt, sid, seq)
        env = None if problem else launcher.env(system.environ, config, home, log, rt)
        for _ in range(0 if problem else 1 + SPAWN_RETRIES):
            try:
                pid = system.launcher(argv, rt.cwd, env, log)
            except OSError as e:
                entry["spawn_errors"] = int(entry.get("spawn_errors") or 0) + 1
                entry.setdefault("history", []).append({"state": CLAIMED, "at": _iso(system.now()),
                                                        "note": f"spawn error ({type(e).__name__})"})
                write_ledger(home, sid, led, system.now())
                continue
            started = system.process_start(pid)
            entry["child"] = {"pid": pid, "start": started, "launched_at": system.now(), "log": str(log),
                              "offsets": entry["snapshot"], "sandbox": rt.sandbox}
            transition(entry, RESUMING, system.now(), f"{launcher.binary} resumed in {rt.sandbox}")
            write_ledger(home, sid, led, system.now())
            bad = launcher.after_spawn(home, sid, pid, system)
            if bad:   # the started child was not in its sandbox: killed; the runtime is disabled; never retried
                entry["verified"] = False
                transition(entry, FAILED, system.now(), bad)
                write_ledger(home, sid, led, system.now())
                _disable_launch(home, rt.kind, sid, binding, bad, system, report, system.now(), origin)
                return "failed"
            _notify(system, binding, sid, RESUMING, f"resuming for owner decision #{seq} in {rt.sandbox}", rt.kind,
                    origin)
            _report(system, report, sid, seq, "launched", f"pid {pid}, {rt.sandbox}")
            return "launched"
        transition(entry, FAILED, system.now(), f"could not prepare {launcher.binary}: {problem}" if problem
                   else f"could not start {launcher.binary} ({entry['spawn_errors']} spawn errors)")
        write_ledger(home, sid, led, system.now())
        remove_tmpdir(home, entry.get("tmp_dir"))
    _notify(system, binding, sid, FAILED, f"could not start {launcher.binary} for owner decision #{seq}", rt.kind,
            origin)
    _report(system, report, sid, seq, "failed", problem or "spawn errors")
    return "failed"


def settle(home: Path, sid: str, system: System, config: dict, report: list, *, dry_run: bool = False) -> None:
    """Finish the session's in-flight entries (see the module docstring)."""
    led = read_ledger(home, sid)
    if not in_flight(led):
        return
    if dry_run:
        for e in in_flight(led):
            _report(system, report, sid, e.get("seq"), "in flight", str(e.get("state")))
        return
    with session_lock(home, sid, wait=0.5) as got:
        if not got:
            return  # a claim is launching right now
        led = read_ledger(home, sid)
        plain_home, boxed_home = state_homes(home)
        for key, entry in list(led["entries"].items()):
            binding = ie.read_binding(boxed_home if entry.get("sandboxed_state") else plain_home, sid)
            if entry.get("state") == CLAIMED:
                _settle_claimed(home, sid, led, entry, binding, system, report)
            if entry.get("state") in (RESUMING, TIMED_OUT, ACK_RELAYED):
                _settle_resuming(home, sid, led, entry, binding, system, config, report)


def _settle_claimed(home, sid, led, entry, binding, system, report) -> None:
    """A CLAIMED entry found later: the waker stopped between the claim and
    the RESUMING write. A live process whose argv names the session and this
    decision's wake prompt is taken as the child (RESUMING, recovered,
    judged like any run), so "interrupted" is never reported while the
    child runs; none: FAILED,
    never retried; the process table unreadable: left CLAIMED (it blocks
    further claims) until WAKE_TIMEOUT."""
    now = system.now()
    runtime = str(entry.get("runtime"))
    table = system.process_table()
    if table is None:
        if now - (_num(entry.get("claimed_at")) or now) > WAKE_TIMEOUT:
            transition(entry, FAILED, now, "the waker stopped during the launch and no child could be found")
            write_ledger(home, sid, led, now)
        return
    # Our child's argv names the session and carries this decision's wake prompt (a per-decision marker a
    # session the user resumed by hand does not have).
    marker = f"owner decision #{entry.get('seq')} in Investigation {entry.get('investigation_id')}"
    found = [pid for pid, args in table if pid != os.getpid() and sid in args and marker in args]
    if found:
        pid = found[0]
        snap = entry.get("snapshot") if isinstance(entry.get("snapshot"), dict) else {}
        entry["child"] = {"pid": pid, "start": system.process_start(pid), "launched_at": entry.get("claimed_at", now),
                          "offsets": snap, "sandbox": entry.get("sandbox"), "recovered": True}
        transition(entry, RESUMING, now, "recovered: the waker stopped during the launch but its child is running")
        write_ledger(home, sid, led, now)
        _report(system, report, sid, entry.get("seq"), "recovered", f"pid {pid}")
        return
    transition(entry, FAILED, now, "the waker stopped during the launch and no child is running; not retried")
    write_ledger(home, sid, led, now)
    _notify(system, binding, sid, FAILED, f"owner decision #{entry.get('seq')}: launch interrupted", runtime)
    _report(system, report, sid, entry.get("seq"), "failed", "launch interrupted")


def _settle_resuming(home, sid, led, entry, binding, system, config, report) -> None:
    now = system.now()
    child = entry.get("child") if isinstance(entry.get("child"), dict) else {}
    seq = entry.get("seq")
    pinned = child.get("sandbox") or entry.get("sandbox")
    offsets = child.get("offsets") if isinstance(child.get("offsets"), dict) else {}
    launched = _num(child.get("launched_at")) or now
    runtime = entry.get("runtime") if entry.get("runtime") in RUNTIMES else CODEX
    launcher = LAUNCHERS.get(runtime)

    def verify(require_turn: bool) -> Optional[str]:
        if launcher is None:   # a launch by a runtime no longer allowed: never trusted
            return f"{LABELS.get(runtime, runtime)} has no launcher to verify the woken run"
        return launcher.verify(home, sid, offsets, pinned, require_turn=require_turn)

    if child_alive(child, system):
        if entry.get("state") == TIMED_OUT or now - launched <= WAKE_TIMEOUT:
            return
        bad = verify(False)
        if bad:
            _disable_launch(home, runtime, sid, binding, bad, system, report, now)
        transition(entry, TIMED_OUT, now, "still running after 2 h; left running, judged when it exits")
        write_ledger(home, sid, led, now)
        _notify(system, binding, sid, TIMED_OUT, f"owner decision #{seq}: the resumed session is still running "
                                                 "after 2 h (left running)", runtime)
        _report(system, report, sid, seq, "timed out", "child left running")
        return
    if entry.get("state") != ACK_RELAYED and "swept" not in entry:
        # Review 3 #4: the child exiting is not the run ending. A process it left (nohup, setsid,
        # a background daemon, even one that cleared TMPDIR and left the group) is this wake's iff
        # its sandbox grants the wake's TMPDIR (review 6); it is stopped (SIGTERM, then SIGKILL).
        # While any is left, or the sandbox cannot be asked, the entry stays in flight (no claim for
        # the session, no verification, relay or cleanup of the TMPDIR under it) and is judged again.
        if "sweep_started" not in entry:
            entry["sweep_started"] = now
            write_ledger(home, sid, led, now)
        left = end_wake_processes(entry, child, system)
        if left is None or left:
            # Review 6 #5: bound the wedge. A sandbox that can never be asked (or a survivor that
            # cannot be stopped) would otherwise hold the entry in flight forever and never free the
            # TMPDIR. After WAKE_TIMEOUT it is FAILED; the TMPDIR is kept and its path recorded.
            if now - (_num(entry.get("sweep_started")) or now) > WAKE_TIMEOUT:
                transition(entry, FAILED, now, "the woken run's processes could not be cleared within 2 h; "
                           f"not retried (its temp directory {entry.get('tmp_dir')} was left in place)")
                entry["verified"] = False
                write_ledger(home, sid, led, now)
                _disable_launch(home, runtime, sid, binding, "a woken run left processes that could not be "
                                "stopped or confirmed", system, report, now)
                _notify(system, binding, sid, FAILED, f"owner decision #{seq}: the run left processes that could "
                        "not be cleared (its temp directory was kept)", runtime)
                _report(system, report, sid, seq, "failed", "survivors could not be cleared")
                return
            note = ("the woken run's processes could not be checked; judged again next pass" if left is None
                    else f"{len(left)} process(es) of the woken run outlived it; stopping them before judging the run")
            if entry.get("lingering") != note:
                entry["lingering"] = note
                entry.setdefault("history", []).append({"state": entry.get("state"), "at": _iso(now), "note": note})
                write_ledger(home, sid, led, now)
            return
        entry["swept"] = now
        write_ledger(home, sid, led, now)
    if "verified" not in entry:
        bad = verify(True)
        entry["verified"] = bad is None
        if bad:
            entry.setdefault("history", []).append({"state": entry.get("state"), "at": _iso(now), "note": bad})
            _disable_launch(home, runtime, sid, binding, bad, system, report, now)
        entry.setdefault("exited_at", now)
        write_ledger(home, sid, led, now)
    disposition: Optional[str] = None
    readable = False
    origin = None
    if binding is not None and binding.get("investigation_id") == entry.get("investigation_id"):
        conn, conn_runtime = _connection_for(home, binding, runtime, system)
        origin = conn.get("origin") if conn else None
        if conn:
            relayed = relay_ack(home, sid, led, entry, conn, _client(conn_runtime, config), system, now,
                                relay_notes=config.get("relay_notes") is True)
            try:
                events, complete = system.fetch(conn, entry["investigation_id"], sid, int(seq),
                                                _client(conn_runtime, config))
                disposition = acknowledged(events, sid, seq)
                # A relay that could not post yet, or a posted one the read does not show yet: asked again.
                readable = disposition is not None or (complete and relayed is not None
                                                       and entry.get("state") != ACK_RELAYED)
            except Exception:
                readable = False
    if not readable and now - (_num(entry.get("exited_at")) or now) < ACK_CHECK_GRACE:
        return  # the acknowledgment could not be read: asked again next pass
    if disposition:
        entry["disposition"] = disposition
        how = " (relayed by the waker)" if entry.get("state") == ACK_RELAYED else ""
        transition(entry, ACKNOWLEDGED, now, f"the session acknowledged the decision ({disposition}){how}")
        words = f"acknowledged owner decision #{seq} ({disposition})"
    else:
        relay = entry.get("relay") if isinstance(entry.get("relay"), dict) else {}
        transition(entry, FAILED, now, ("the run ended without acknowledging the decision"
                                        + (f" ({relay['reason']})" if relay.get("reason") else "")) if readable
                   else "the run ended; its acknowledgment could not be read")
        words = f"the run for owner decision #{seq} ended without an acknowledgment (not retried)"
    write_ledger(home, sid, led, now)
    trim_log(child.get("log"))
    remove_tmpdir(home, entry.get("tmp_dir"))
    _notify(system, binding, sid, entry["state"], words, runtime, origin)
    _report(system, report, sid, seq, entry["state"].lower(), entry.get("reason", ""))


KILL_GRACE = 2.0


def wake_processes(entry: dict, child: dict, system: System) -> Optional[List[int]]:
    """The processes of this wake still running (the child itself has
    exited), or None when it cannot be told (fail closed). A process is this
    wake's exactly when the sandbox grants it this wake's private TMPDIR and
    denies the parent canary (sandboxed_procs / wake_owned): everything the
    run starts inherits that writable root, and nothing it does (clearing
    the TMPDIR variable, setsid, chdir, re-exec) removes it; a different
    wake's run, the owner's own Codex, an editor, or an Apple agent allowed
    to write broadly under $HOME is not this wake's. Process group and
    environment are not used (a reused pid or a cleared variable could
    mislead them)."""
    tmp = entry.get("tmp_dir")
    if not (isinstance(tmp, str) and tmp):
        return []
    canary = os.path.dirname(tmp)   # ~/.cardinal/wake/tmp: the TMPDIR's parent, which the wake sandbox denies
    since = _num(child.get("start")) or _num(child.get("launched_at")) or _num(entry.get("claimed_at"))
    found = system.sandboxed_procs(since if since is not None else 0.0, [tmp], canary)
    if found is None:
        return None
    return sorted(pid for pid in found if pid != os.getpid())


def end_wake_processes(entry: dict, child: dict, system: System) -> Optional[List[int]]:
    """Stop this wake's surviving processes: SIGTERM, a grace, SIGKILL, a
    grace. Returns what is still left ([] when none), or None when it cannot
    be told."""
    left = wake_processes(entry, child, system)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if left is None:
            return None
        if not left:
            return left
        for pid in left:
            with contextlib.suppress(OSError):
                system.kill(pid, sig)
        system.sleep(KILL_GRACE)
        left = wake_processes(entry, child, system)
    return left


def relay_ack(home, sid, led, entry, conn, client, system, now, *, relay_notes: bool = False) -> Optional[bool]:
    """After the child exited (never before), relay the acknowledgment the
    woken agent wrote to $TMPDIR/ACK_FILE: post exactly that one
    acknowledged event of the launched seq, as the session, with the
    plugin's connection (System.post_ack). True: posted (now or on an
    earlier pass; ACK_RELAYED); False: nothing to relay (no file, an invalid
    one, the wrong seq, or a run that failed verification: the reason is
    kept in the ledger, never the note); None: the post failed, tried again
    next pass. The content is the agent's: the waker only carries it out of
    the sandbox (no network, no hooks in a woken Codex run). The note is
    dropped unless the owner opted in (enable --relay-notes; review 3 #5:
    an advisory could steer the agent into putting secrets in it, which the
    relay would post where others read), and scrubbed when relayed."""
    seq = entry.get("seq")
    relay = entry.get("relay") if isinstance(entry.get("relay"), dict) else None
    if entry.get("state") == ACK_RELAYED or (relay and relay.get("posted")):
        return True
    if relay is not None:
        return False
    if entry.get("verified") is False:
        ack, why = None, "the woken run failed verification, so its acknowledgment is not relayed"
    else:
        ack, why = read_ack_file(entry.get("tmp_dir"), seq)
    if ack is None:
        entry["relay"] = {"posted": False, "reason": why}
        write_ledger(home, sid, led, now)
        return False
    try:
        note = ack.get("note") if relay_notes else None
        if note:
            from .evidence_capture import scrub_prompt
            note = scrub_prompt(note)[:ie.MAX_NOTE]
        system.post_ack(conn, entry["investigation_id"], sid, seq, ack["disposition"], note or None, client)
    except Exception as e:
        note = f"the acknowledgment could not be relayed ({type(e).__name__})"
        entry.setdefault("history", []).append({"state": entry.get("state"), "at": _iso(now), "note": note})
        write_ledger(home, sid, led, now)
        return None
    entry["relay"] = {"posted": True, "disposition": ack["disposition"]}
    transition(entry, ACK_RELAYED, now, f"the waker relayed the session's acknowledgment ({ack['disposition']})")
    write_ledger(home, sid, led, now)
    return True


def _disable_launch(home, runtime, sid, binding, why, system, report, now, origin=None) -> None:
    disable_runtime(home, runtime, f"session {sid[:8]}: {why}", now)
    _notify(system, binding, sid, "disabled", f"{LABELS.get(runtime, runtime)} wake is disabled: {why}. Run "
                                              f"cardinal-wake reenable {runtime} after checking.", runtime, origin)
    _report(system, report, sid, None, f"{runtime} disabled", why)


def trim_log(path: Any) -> None:
    """A finished wake's log larger than MAX_WAKE_LOG keeps only its last
    WAKE_LOG_TAIL bytes. Fail open."""
    if not (isinstance(path, str) and os.path.isabs(path)):
        return
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size <= MAX_WAKE_LOG:
            return
        with open(path, "rb") as f:
            f.seek(-WAKE_LOG_TAIL, os.SEEK_END)
            tail = f.read()
        tmp = path + ".trim"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(b"[cardinal-wake: earlier output trimmed]\n" + tail)
        os.replace(tmp, path)
    except OSError:
        return


def prune_logs(home: Path) -> None:
    """Keep the newest WAKE_LOGS_KEEP per-wake logs (never launchd.log, never
    a log an in-flight run is writing)."""
    d = logs_dir(home)
    try:
        names = [n for n in os.listdir(str(d)) if n.endswith(".log") and n != "launchd.log"]
    except OSError:
        return
    if len(names) <= WAKE_LOGS_KEEP:
        return
    busy = set()
    for sid in ledger_sids(home):
        for e in in_flight(read_ledger(home, sid)):
            child = e.get("child") if isinstance(e.get("child"), dict) else {}
            if isinstance(child.get("log"), str):
                busy.add(os.path.basename(child["log"]))
    files = sorted(names, key=lambda n: (d / n).stat().st_mtime if (d / n).exists() else 0.0)
    for n in files[:len(files) - WAKE_LOGS_KEEP]:
        if n not in busy:
            with contextlib.suppress(OSError):
                (d / n).unlink()


# ---------------------------------------------------------------------------
# Install (launchd) and the CLI
# ---------------------------------------------------------------------------

RUNNER = '''#!/usr/bin/env python3
"""cardinal-wake, the stable copy launchd runs (written by `cardinal-wake
enable` with cardinal_core next to it; re-written by every enable)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cardinal_core import session_wake  # noqa: E402

if __name__ == "__main__":
    sys.exit(session_wake.cli_main(sys.argv[1:]))
'''


def install_lib(home: Path, core_dir: Optional[Path] = None) -> Path:
    """Copy cardinal_core (this package) and the runner into
    ~/.cardinal/wake/lib, replacing what was there. Returns the runner."""
    core_dir = core_dir or Path(__file__).resolve().parent
    lib = _private_dir(lib_dir(home))
    tmp = lib / f".cardinal_core.{os.getpid()}.tmp"
    if tmp.exists():
        shutil.rmtree(str(tmp))
    shutil.copytree(str(core_dir), str(tmp), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    dest = lib / "cardinal_core"
    old = lib / f".cardinal_core.{os.getpid()}.old"
    if dest.exists():
        os.replace(str(dest), str(old))
    os.replace(str(tmp), str(dest))
    if old.exists():
        shutil.rmtree(str(old), ignore_errors=True)
    runner = lib / "cardinal-wake"
    tmp_runner = lib / f".cardinal-wake.{os.getpid()}.tmp"
    tmp_runner.write_text(RUNNER, encoding="utf-8")
    os.chmod(str(tmp_runner), 0o700)
    os.replace(str(tmp_runner), str(runner))
    return runner


def plist_bytes(home: Path, python: str, runner: Path, path_env: str) -> bytes:
    log = logs_dir(home) / "launchd.log"
    return plistlib.dumps({
        "Label": PLIST_LABEL,
        "ProgramArguments": [python, "-I", "-S", str(runner), "run", "--once"],
        "StartInterval": RUN_INTERVAL,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {"PATH": path_env},
    })


def launchctl_default(args: List[str]) -> Tuple[int, str]:
    try:
        r = subprocess.run([LAUNCHCTL] + args, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


def _domain() -> str:
    return f"gui/{os.getuid()}"


def enable(home: Path, *, launch: bool, environ: Mapping[str, str], launchctl: Callable[[List[str]], Tuple[int, str]],
           out: Callable[[str], None], now: float, version: Optional[str] = None,
           runtime: Optional[str] = None, core_dir: Optional[Path] = None,
           roots: Optional[List[str]] = None, relay_notes: bool = False) -> int:
    """Install or update the launchd agent and the config. roots: the
    approved roots for Gemini wakes (realpath'd; None keeps the current
    ones, given ones replace them). relay_notes: relay the woken agent's
    acknowledgment note too (scrubbed), not only its disposition; off
    unless given on this enable."""
    old = read_config(home)
    if roots is not None:
        approved = []
        for r in roots:
            real = os.path.realpath(os.path.expanduser(r))
            problem = None if os.path.isdir(real) else "it is not a directory"
            problem = problem or approved_root_problem(real, home)
            if problem:
                out(f"cardinal-wake: --root {r} is not allowed: {problem}")
                return 2
            approved.append(real)
        roots = list(dict.fromkeys(approved))
    else:
        roots = list(old.get("roots") or [])
    # B1: the daemon never runs anything an agent can write. The interpreter
    # is this Python or /usr/bin/python3, outside $HOME, git work trees,
    # approved roots and temp dirs, run with -I; the launch binaries (and
    # node, for a node script) resolve outside git work trees, approved roots
    # and temp dirs; PATH is the system dirs plus their directories only.
    user_path = environ.get("PATH") or ":".join(SYSTEM_PATH_DIRS)
    python, why = pick_python(home, roots)
    if python is None:
        out(f"cardinal-wake: no Python the daemon may run: {why}. Install python3 outside your home directory "
            "and your repositories (or the Xcode command line tools for /usr/bin/python3).")
        return 2
    executables: dict = {}
    binaries = {python: fingerprint(python)}
    node_path = None
    dirs = [os.path.dirname(python)]
    for launcher in LAUNCHERS.values():   # each launchable runtime's binary, resolved now
        found = shutil.which(launcher.binary, path=user_path)
        real = os.path.realpath(found) if found else None
        problem = (unsafe_location(real, home, roots) or shim_problem(found, home)) if real else None
        node_real = None
        if real and not problem and launcher.runtime == CODEX and not is_native_executable(real):
            # N2: codex must be the Mach-O it runs; an npm install's codex.js is resolved to the
            # native binary it would spawn, launched directly; any other script is refused.
            native = npm_codex_native(real) if needs_node(real) else None
            if native:
                real = native
                problem = unsafe_location(native, home, roots) or shim_problem(native, home)
            else:
                problem = f"{real} is not a native (Mach-O) executable (a script may pick or spawn another binary)"
        if real and not problem and needs_node(real):
            node = shutil.which("node", path=user_path)
            node_real = os.path.realpath(node) if node else None
            problem = ("node was not found" if not node_real else
                       unsafe_location(node_real, home, roots) or shim_problem(node, home)
                       or (None if is_native_executable(node_real)
                           else f"{node_real} is not a native (Mach-O) executable"))
            if problem:
                problem = f"{launcher.binary} is a node script and {problem}"
        if problem:
            if launch and launcher.proven:
                out(f"cardinal-wake: {launcher.binary} cannot be trusted: {problem}. Install it outside your "
                    "repositories, temp dirs and version-manager shims.")
                return 2
            real, node_real = None, None
        if node_real:
            node_path = node_real
            binaries[node_real] = fingerprint(node_real)
            dirs.append(os.path.dirname(node_real))
        executables[launcher.executable] = real
        if real:
            binaries[real] = fingerprint(real)
            dirs.append(os.path.dirname(real))
    path_env = daemon_path(*dirs)
    runner = install_lib(home, core_dir)
    _private_dir(logs_dir(home))
    versions = dict(old.get("versions") or {})
    if runtime in RUNTIMES and version:
        versions[runtime] = version
    write_config(home, {"enabled": True, "launch": bool(launch), **executables, "python": python,
                        "node_path": node_path, "path_env": path_env, "binaries": binaries,
                        "enabled_at": old.get("enabled_at") if old["enabled"] and old.get("enabled_at") else _iso(now),
                        "versions": versions, "roots": roots, "relay_notes": bool(relay_notes)})
    plist = plist_path(home)
    plist.parent.mkdir(parents=True, exist_ok=True)
    tmp = plist.with_name(f".{plist.name}.tmp")
    tmp.write_bytes(plist_bytes(home, python, runner, path_env))
    os.chmod(str(tmp), 0o644)
    os.replace(str(tmp), str(plist))
    launchctl(["bootout", f"{_domain()}/{PLIST_LABEL}"])
    rc, said = launchctl(["bootstrap", _domain(), str(plist)])
    if rc != 0:
        out(f"cardinal-wake: launchctl bootstrap failed ({said or rc}); the agent is written to {plist} but not "
            "loaded")
        return 1
    out(f"Wake is on: launchd runs {runner} every {RUN_INTERVAL} s ({plist}).")
    out("It wakes a session only for an owner decision you post on its Investigation page, addressed to that "
        "session with \"Resume this session if it has stopped\" checked. Agents cannot post one.")
    if runtime in NOTIFY_ONLY_REASONS and active_launcher(runtime) is None:
        out(f"{LABELS[runtime]} sessions are notify-only: an addressed decision shows a notification and never "
            f"resumes a {LABELS[runtime]} session" + (", with or without --launch." if launch else "."))
        if runtime == CODEX:
            out("  Codex unattended launch is built but pending live verification of survivor detection "
                "(the TMPDIR writable-root probe against a real Codex seatbelt and an env-cleared survivor; "
                "docs/specs/resume-and-wake.md §6 C-23/C-24). An owner turns it on by setting "
                "CODEX_LAUNCH_PROVEN = True in a reviewed change (there is no runtime flag).")
    if roots:
        out("Approved roots (Gemini wakes): " + ", ".join(roots))
    if launch:
        for r, launcher in LAUNCHERS.items():
            if not launcher.proven:
                continue
            out(launcher.can_launch)
            exe = executables.get(launcher.executable)
            if not exe:
                out(f"{launcher.binary} was not found on PATH, so {LABELS[r]} sessions cannot launch yet: install "
                    f"it and run cardinal-wake enable --launch again.")
            else:
                out(f"{LABELS[r]}: {exe}")
            reason = disabled_reason(home, r)
            if reason:
                out(f"{LABELS[r]} launch is disabled: {reason}. Run cardinal-wake reenable {r} after checking.")
    else:
        out("Unattended launch is off: every eligible decision is a notification only. (No runtime launches "
            "unattended yet; Codex and Gemini CLI launch are built but shipped off pending their live gates.)")
    out(NOTIFY_ONLY_TEXT)
    return 0


def disable(home: Path, *, launchctl: Callable[[List[str]], Tuple[int, str]], out: Callable[[str], None]) -> int:
    launchctl(["bootout", f"{_domain()}/{PLIST_LABEL}"])
    with contextlib.suppress(FileNotFoundError):
        plist_path(home).unlink()
    config = read_config(home)
    if config_path(home).exists():
        config.update(enabled=False, launch=False)
        write_config(home, config)
    out("Wake is off: the launchd agent is removed. Sessions resumed earlier are not stopped.")
    return 0


def status(home: Path, system: System, out: Callable[[str], None]) -> int:
    """The opt-in, disabled runtimes, recent ledger entries and candidate
    sessions; local files only, never decision text."""
    now = system.now()
    c = read_config(home)
    out(f"wake: {'on' if c['enabled'] else 'off'}; unattended launch: {'on' if c['launch'] else 'off'}"
        f"{'; enabled ' + str(c['enabled_at']) if c['enabled'] and c['enabled_at'] else ''}")
    out(f"launchd agent: {'installed' if plist_path(home).exists() else 'not installed'} ({plist_path(home)})")
    out(f"python: {c.get('python') or 'not vetted'} (-I); PATH: {c.get('path_env') or 'not vetted'}")
    if c["enabled"]:
        problem = binaries_problem(c, home)
        out("executables: " + (problem if problem else "unchanged since enable"))
    for launcher in LAUNCHERS.values():
        out(f"{launcher.binary}: {c.get(launcher.executable) or 'not found'}"
            + ("" if launcher.proven else " (launch built, off until its live proofs pass)"))
    out("approved roots: " + (", ".join(c.get("roots") or []) or "none (cardinal-wake enable --root PATH)"))
    out("notify-only runtimes: " + ", ".join(LABELS[r] for r in RUNTIMES if active_launcher(r) is None)
        + ", and any session whose runtime is not recognized")
    for r in RUNTIMES:
        reason = disabled_reason(home, r)
        if reason:
            out(f"{r} launch disabled: {reason}")
    rows = []
    for sid in ledger_sids(home):
        for e in read_ledger(home, sid)["entries"].values():
            rows.append((_num(e.get("updated_at")) or 0.0, sid, e))
    rows.sort(key=lambda r: r[0], reverse=True)
    out("recent wakes:" if rows else "recent wakes: none")
    for t, sid, e in rows[:20]:
        out(f"  {_iso(t)} {sid[:8]} #{e.get('seq')} {e.get('state')}"
            + (f": {e.get('reason')}" if e.get("reason") else ""))
    cands = candidate_sessions(home, now)
    out("candidate sessions:" if cands else "candidate sessions: none")
    for sid, b, _, _ in cands:
        rt = resolve(home, sid)
        out(f"  {sid[:8]} {LABELS.get(rt.kind, rt.kind)}: "
            f"{'can launch in ' + str(rt.sandbox) if rt.eligible else rt.reason}")
    return 0


def _plugin_version(plugin_root: Optional[Path]) -> Optional[str]:
    if plugin_root is None:
        return None
    for manifest in glob.glob(str(plugin_root / ".*plugin" / "plugin.json")):
        v = _read_json(Path(manifest)).get("version")
        if isinstance(v, str):
            return v
    return None


def cli_main(argv: Optional[List[str]] = None, *, runtime: Optional[str] = None, plugin_root: Optional[Path] = None,
             home: Optional[Path] = None, system: Optional[System] = None,
             launchctl: Optional[Callable[[List[str]], Tuple[int, str]]] = None,
             platform: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> int:
    """cardinal-wake: enable [--launch] | disable | status | run [--once]
    [--dry-run] | reenable codex. Run it in your own terminal."""
    parser = argparse.ArgumentParser(
        prog="cardinal-wake",
        description="Owner-woken sessions: when you post an owner decision on an Investigation page addressed "
                    "to a stopped session, resume it (Codex CLI sessions that only ran sandboxed) or notify "
                    "you (everything else). Run it in your own terminal, not through an agent.")
    sub = parser.add_subparsers(dest="command")
    p_enable = sub.add_parser("enable", help="install the per-user launchd agent (macOS)")
    p_enable.add_argument("--launch", action="store_true",
                          help="also allow unattended launch of eligible Codex CLI sessions")
    p_enable.add_argument("--relay-notes", action="store_true",
                          help="also relay the note a woken agent writes with its acknowledgment (credential "
                               "shapes scrubbed); by default only the disposition is relayed")
    p_enable.add_argument("--root", action="append", default=None, metavar="PATH",
                          help="an approved root for Gemini wakes (repeatable; replaces the current list; never "
                               "your home directory or above it)")
    sub.add_parser("disable", help="remove the launchd agent")
    sub.add_parser("status", help="the opt-in, disabled runtimes, recent wakes and candidate sessions")
    p_run = sub.add_parser("run", help="look for addressed owner decisions (launchd runs run --once)")
    p_run.add_argument("--once", action="store_true", help="one pass, then exit")
    p_run.add_argument("--dry-run", action="store_true", help="print what would happen; launch nothing")
    p_re = sub.add_parser("reenable", help="allow a runtime to launch again after a failed sandbox check")
    p_re.add_argument("target", choices=sorted(LAUNCHERS))
    args = parser.parse_args(argv)
    home = home or ie.home_dir()
    system = system or System()
    environ = os.environ if environ is None else environ
    launchctl = launchctl or launchctl_default
    platform = platform or sys.platform
    out = system.out
    if args.command in ("enable", "disable") and platform != "darwin":
        out("cardinal-wake: wake runs as a launchd agent, on macOS only")
        return 2
    if args.command == "enable":
        return enable(home, launch=args.launch, environ=environ, launchctl=launchctl, out=out, now=system.now(),
                      version=_plugin_version(plugin_root), runtime=runtime, roots=args.root,
                      relay_notes=args.relay_notes)
    if args.command == "disable":
        return disable(home, launchctl=launchctl, out=out)
    if args.command == "status":
        return status(home, system, out)
    if args.command == "reenable":
        cleared = reenable_runtime(home, args.target)
        out(f"{args.target} launch re-enabled" if cleared else f"{args.target} launch was not disabled")
        return 0
    if args.command == "run":
        _trim_launchd_log(home)
        if args.once or args.dry_run:
            report = run_once(home, system, dry_run=args.dry_run)
            if args.dry_run and not report:
                out("nothing to do: no addressed owner decision is waiting for a stopped session")
            return 0
        while True:  # pragma: no cover - a foreground loop for debugging
            reap()
            run_once(home, system)
            time.sleep(RUN_INTERVAL)
    parser.print_help()
    return 2


def _trim_launchd_log(home: Path) -> None:
    log = logs_dir(home) / "launchd.log"
    with contextlib.suppress(OSError):
        if log.stat().st_size > MAX_LAUNCHD_LOG:
            with open(str(log), "w"):
                pass
