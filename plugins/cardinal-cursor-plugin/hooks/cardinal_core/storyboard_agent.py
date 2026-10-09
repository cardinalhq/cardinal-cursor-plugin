"""Storyboard associations for the adapters without Claude Code's dedicated
storyboard hooks: Codex, Cursor and Gemini CLI.

Claude Code has its own hook processes (storyboard-discovery.py,
storyboard-context.py, evidence-capture.py). The other adapters reuse the
hook events they already handle; this module is what those handlers and
their `scripts/cardinal-storyboard` launcher share:

  - session_start_text: at session start, a line naming this session's id
    (for storyboard__create / add_act / find `session_id`) and the CLI whose
    output is the `context`, plus the storyboard_discovery block for this
    checkout (cache-only PR, X-Cardinal-Client: <runtime>/<plugin version>).
  - record_edits / apply_patch_paths: the files an edit tool changed, into
    storyboard_files (`context.paths`), only after the tool succeeded.
  - stamped_input: a copy of a storyboard tool call's input with an absent
    session_id / context filled, for the adapters whose pre-tool hook can
    rewrite MCP input (Codex PreToolUse, Gemini BeforeTool; Cursor's
    beforeMCPExecution cannot). Never alters a context the model set and
    never writes `about`.
  - registered_hook_group / hooks_file_registers: whether the adapter's
    stamping hook is actually registered (session start wording).
  - cli_main: `cardinal-storyboard context [--bare] | discover`.

State lives under the adapter's runtime dir (~/.<agent>/cardinal/):
storyboard-discovery/ (per-session discovery cache), storyboard-files/,
server-caps.json. Every entry point is fail-open: it returns None / [] /
False, or prints what it could, and never raises.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional

from . import storyboard_context, storyboard_discovery, storyboard_files
from .paths import AgentPaths

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
CONTEXT_DISABLE_ENV = "CARDINAL_STORYBOARD_CONTEXT"
# No storyboard text at session start at all (the session id line and the
# discovery block); CARDINAL_STORYBOARD_DISCOVERY=0 drops the block only.
SESSION_START_DISABLE_ENV = "CARDINAL_STORYBOARD_SESSION_START"
OFF_VALUES = ("0", "false", "off", "no")
STORYBOARD_TOOLS = ("create", "add_act", "publish", "find", "link")
SESSION_TOOLS = ("create", "add_act", "find")
CONTEXT_TOOLS = ("create", "add_act", "publish", "find")
GH_TOOLS = ("create", "add_act", "publish")
GH_TIMEOUT_S = 2.0
MAX_COMMITS = 10
MAX_PRS = 50
MAX_PATCH_FILES = 50

# apply_patch headers naming a file the patch touches (codex-rs apply-patch
# grammar). Paths are relative to the call's cwd.
_PATCH_HEADER_RE = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$")
_EXIT_CODE_RE = re.compile(r"(?m)^Exit code: (-?\d+)\s*$")


class Wiring:
    """One adapter's storyboard wiring.

    runtime: "codex" | "cursor" | "gemini" (the client name).
    paths: the adapter's AgentPaths (~/.codex, ~/.cursor, ~/.gemini): the
      connection (cardinal.json mcp_url + cardinal-secrets.json
      mcp_api_key) and the runtime dir.
    version: the plugin version (client codex/<ver>).
    cli: absolute path of the adapter's scripts/cardinal-storyboard, named
      in the session-start text.
    """

    def __init__(self, runtime: str, paths: AgentPaths, version: Optional[str] = None,
                 cli: Optional[str] = None, environ: Optional[dict] = None):
        self.runtime = runtime
        self.paths = paths
        # _plugin_version's "unknown" fallback is not a version.
        self.version = version if isinstance(version, str) and version and version != "unknown" else None
        self.cli = cli
        self.environ = os.environ if environ is None else environ

    @property
    def client(self) -> str:
        """<runtime>/<plugin version>: context `client` and the
        X-Cardinal-Client header (maestro tells a current plugin from a
        pre-0.40 one by the header's presence)."""
        try:
            from . import evidence
            return evidence.client_string(self.runtime, self.version)
        except Exception:
            return self.runtime

    @property
    def runtime_dir(self) -> Path:
        return self.paths.runtime_dir

    @property
    def state_dir(self) -> Path:
        return self.runtime_dir / "storyboard-discovery"

    @property
    def files_dir(self) -> Path:
        return self.runtime_dir / "storyboard-files"

    @property
    def caps_path(self) -> Path:
        return self.runtime_dir / "server-caps.json"

    def connection(self) -> dict:
        """{origin, org, key} or {}."""
        try:
            from .evidence_promote import agent_paths_connection
            home = Path(self.environ.get("HOME") or str(Path.home()))
            return agent_paths_connection(self.paths)(home, dict(self.environ)) or {}
        except Exception:
            return {}

    def actor_email(self) -> Optional[str]:
        """user_email cardinal-connect stored in cardinal.json (the identity
        telemetry is tagged with); never git config."""
        try:
            value = self.paths.read_state().get("user_email")
            return value if isinstance(value, str) and value else None
        except Exception:
            return None

    def discovery_disabled(self) -> bool:
        return storyboard_discovery.is_disabled(self.environ)

    def _off(self, name: str) -> bool:
        value = self.environ.get(name)
        return isinstance(value, str) and value.strip().lower() in OFF_VALUES

    def context_disabled(self) -> bool:
        return self._off(CONTEXT_DISABLE_ENV)

    def session_start_disabled(self) -> bool:
        return self._off(SESSION_START_DISABLE_ENV)

    def caps(self, conn: Optional[dict] = None) -> Optional[int]:
        conn = self.connection() if conn is None else conn
        try:
            return storyboard_discovery.read_caps(self.caps_path, conn.get("origin") if conn else None)
        except Exception:
            return None

    def edited_paths(self, session_id: Optional[str]):
        """storyboard_files.for_repo for this session, or None without one."""
        if not valid_session(session_id):
            return None
        files = self.files_dir
        return lambda repo: storyboard_files.for_repo(files, session_id, repo)

    def pr_resolver(self, gh: bool):
        """gh (cached, GH_TIMEOUT_S) when gh, else the gh cache only."""
        try:
            from . import decisions
            cache_dir = decisions.cache_dir(self.runtime_dir)
            if not gh:
                return storyboard_context.cache_only_pr_resolver(cache_dir)

            def resolve(cwd: str, repo: str, branch: str):
                return decisions.resolve_pr(cwd, repo, branch, cache_dir, timeout=GH_TIMEOUT_S)

            return resolve
        except Exception:
            return None

    def collect(self, cwd: str, session_id: Optional[str], *, gh: bool) -> dict:
        return storyboard_context.collect(cwd, client=self.client, actor_email=self.actor_email(),
                                          pr_resolver=self.pr_resolver(gh),
                                          edited_paths=self.edited_paths(session_id))


def valid_session(session_id: Any) -> bool:
    return isinstance(session_id, str) and bool(SESSION_RE.match(session_id))


def _connected(conn: Any) -> bool:
    return isinstance(conn, dict) and bool(conn.get("key")) and bool(conn.get("org"))


# ---------------------------------------------------------------------------
# Session start
# ---------------------------------------------------------------------------

def session_line(session_id: Optional[str], cli: Optional[str], auto_context: bool = False) -> Optional[str]:
    """The line naming this session's id and the context CLI, or None
    without a valid id. auto_context: the adapter's pre-tool hook is
    registered and will fill an absent session_id / context itself (the CLI
    stays the fallback); the adapter decides (registered_hook_group).

    The command named is `context --bare`: it prints the context object
    itself, so the model passes exactly what it reads, never the
    {"context": ...} wrapper (A1's strict context rejects the unknown key
    `context`). session_id is its own argument, never a context key."""
    if not valid_session(session_id):
        return None
    line = (f"Cardinal session id for this session: {session_id}. When you call Cardinal's "
            f"storyboard__create, storyboard__add_act or storyboard__find, pass it as session_id")
    if cli:
        cmd = f'python3 "{cli}" context --bare --session-id {session_id}'
        if auto_context:
            line += (f"; the plugin fills session_id and context when you leave them out, and "
                     f"`{cmd}` prints that context object (where this storyboard is written from), "
                     f"to pass yourself as `context`, exactly as printed, when it does not.")
        else:
            line += (f", and pass as `context` the JSON object `{cmd}` prints, exactly as printed "
                     f"(where this storyboard is written from: repo, branch, PR, HEAD, the files "
                     f"this session edited). session_id is never a key inside context.")
    else:
        line += "."
    return line


def registered_hook_group(hooks_root: Any, event: str, needle: str) -> bool:
    """Whether hooks_root (a hooks.json / settings.json `hooks` map) has a
    group under event with a handler whose command contains needle (the
    adapter's `--event X # <marker>` tail). A plugin upgrade runs the new
    hook code before the user re-registers hooks, so session start checks
    this before telling the model the plugin fills context. Never raises."""
    try:
        groups = hooks_root.get(event) if isinstance(hooks_root, dict) else None
        for group in groups if isinstance(groups, list) else []:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            for h in handlers if isinstance(handlers, list) else []:
                if isinstance(h, dict) and needle in str(h.get("command") or ""):
                    return True
    except Exception:
        pass
    return False


def hooks_file_registers(path: Path, event: str, needle: str) -> bool:
    """registered_hook_group over the `hooks` map of the JSON file at path;
    False when it is missing or unreadable."""
    try:
        data = json.loads(Path(path).read_text())
    except Exception:
        return False
    return registered_hook_group(data.get("hooks") if isinstance(data, dict) else None, event, needle)


def session_start_text(wiring: Wiring, cwd: str, session_id: Optional[str], *,
                       auto_context: bool = False, include_session_line: bool = True, opener=None,
                       deadline: Optional[float] = None) -> Optional[str]:
    """The session id line and the discovery block (each only when it
    applies), joined by a blank line; None when there is neither. Nothing
    without a Cardinal MCP connection (the storyboard tools are not there).
    The PR comes from the gh cache only; the network part is bounded by
    deadline (absolute time.monotonic(), default 2 s from now).
    CARDINAL_STORYBOARD_SESSION_START=0 turns all of it off."""
    try:
        if wiring.session_start_disabled():
            return None
        conn = wiring.connection()
        if not _connected(conn):
            return None
        parts: List[str] = []
        line = session_line(session_id, wiring.cli, auto_context)
        if line and include_session_line:
            from .storyboard_offer import VISUALIZATION_OFFER
            parts.append(line + " " + VISUALIZATION_OFFER)
        if not wiring.discovery_disabled():
            try:
                block = storyboard_discovery.discover(
                    cwd,
                    conn=conn,
                    session_id=session_id if valid_session(session_id) else None,
                    state_dir=wiring.state_dir if valid_session(session_id) else None,
                    event=storyboard_discovery.SESSION_START,
                    pr_resolver=wiring.pr_resolver(gh=False),
                    opener=opener,
                    deadline=deadline,
                    client=wiring.client,
                    caps_path=wiring.caps_path,
                )
            except Exception:
                block = None
            if block:
                parts.append(block)
        return "\n\n".join(parts) if parts else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Edited files
# ---------------------------------------------------------------------------

def apply_patch_text(tool_input: Any) -> Optional[str]:
    """The patch of a Codex apply_patch call: tool_input {"command": patch}
    (captured from codex-cli 0.142 PreToolUse/PostToolUse), {"patch"} /
    {"input"} (transcript spellings), or the bare string."""
    if isinstance(tool_input, str):
        return tool_input
    if isinstance(tool_input, dict):
        for key in ("command", "patch", "input"):
            value = tool_input.get(key)
            if isinstance(value, str) and "*** Begin Patch" in value:
                return value
            if isinstance(value, list) and len(value) >= 2 and value[0] == "apply_patch" and isinstance(value[-1], str):
                return value[-1]
    return None


def apply_patch_paths(tool_input: Any) -> List[str]:
    """The files an apply_patch call names (Add / Update / Delete File and
    Move to headers), in order, deduplicated, at most MAX_PATCH_FILES."""
    patch = apply_patch_text(tool_input)
    out: List[str] = []
    if not patch:
        return out
    for line in patch.splitlines():
        m = _PATCH_HEADER_RE.match(line)
        if m:
            path = m.group(1)
            if path and path not in out:
                out.append(path)
            if len(out) >= MAX_PATCH_FILES:
                break
    return out


def apply_patch_succeeded(tool_response: Any) -> bool:
    """Whether a Codex apply_patch PostToolUse tool_response reports
    success: "Exit code: 0 … Success. Updated the following files: …"
    (captured, codex-cli 0.142). An unknown shape is not success."""
    if isinstance(tool_response, dict):
        for key in ("output", "stdout", "content", "text"):
            if isinstance(tool_response.get(key), str):
                tool_response = tool_response[key]
                break
        else:
            return False
    if not isinstance(tool_response, str):
        return False
    m = _EXIT_CODE_RE.search(tool_response)
    if m:
        return m.group(1) == "0"
    return "Success. Updated the following files" in tool_response


def record_edits(wiring: Wiring, session_id: Optional[str], file_paths: Iterable[Any],
                 cwd: Optional[str]) -> int:
    """Record each edited file (absolute, or relative to cwd) in
    storyboard_files for this session; the number recorded. Local only."""
    if not valid_session(session_id):
        return 0
    n = 0
    try:
        for path in file_paths:
            if isinstance(path, str) and path and storyboard_files.record(wiring.files_dir, session_id, path, cwd):
                n += 1
    except Exception:
        pass
    return n


# ---------------------------------------------------------------------------
# Pre-tool context stamping
# ---------------------------------------------------------------------------

def prs_of_commits(cwd: str, commits: Iterable[Any]) -> list:
    """The PRs the given commits' local messages name (subjects first)."""
    from .initiative import git

    out: list = []
    shas = [c for c in commits if isinstance(c, str) and COMMIT_RE.match(c.strip().lower())][:MAX_COMMITS]
    for sha in shas:
        msg = git(["log", "-1", "--format=%s%n%b", sha.strip().lower(), "--"], cwd)
        if not msg:
            continue
        found = storyboard_discovery.pr_from_subject(msg.split("\n", 1)[0])
        if found is None:
            for line in msg.split("\n")[1:]:
                if line.startswith("Merge pull request #"):
                    found = storyboard_discovery.pr_from_subject(line)
                    break
        if found is not None and found not in out:
            out.append(found)
    return out


def _add_prs(refs: dict, prs: list) -> bool:
    existing = refs.get("prs")
    current = list(existing) if isinstance(existing, list) else ([] if existing is None else [existing])
    seen = {str(p).lstrip("#") for p in current if isinstance(p, (int, str)) and not isinstance(p, bool)}
    added = [p for p in prs if str(p) not in seen]
    if not added:
        return False
    refs["prs"] = (current + added)[:MAX_PRS]
    return True


def stamped_input(wiring: Wiring, tool: str, tool_input: Any, session_id: Optional[str],
                  cwd: Optional[str]) -> Optional[dict]:
    """A copy of tool_input (every original key) with what was added, or
    None when nothing was. tool: the storyboard tool's short name (create,
    add_act, publish, find, link).

      - session_id (create, add_act, find): this session's id when absent.
      - context (create, add_act, publish, find): only when absent or {};
        the branch's PR from gh within GH_TIMEOUT_S on create / add_act /
        publish, from the gh cache on find. publish only when the cached
        server capability is >= 1 (an older gateway rejects the argument).
      - find with refs.commits, at capability >= 1: the PRs those commits
        name locally are added to refs.prs.
      - link: nothing (its about refs are the model's). Never `about`.
    """
    try:
        if wiring.context_disabled() or tool not in STORYBOARD_TOOLS or tool == "link":
            return None
        if not isinstance(tool_input, dict):
            return None
        out = dict(tool_input)
        changed = False
        if not (isinstance(cwd, str) and cwd and os.path.isdir(cwd)):
            cwd = os.getcwd()
        sid = session_id if valid_session(session_id) else None

        if tool in SESSION_TOOLS and out.get("session_id") in (None, "") and sid:
            out["session_id"] = sid
            changed = True

        caps = wiring.caps() if tool in ("publish", "find") else None
        ctx = out.get("context")
        ctx_missing = ctx is None or ctx == {}
        if tool in CONTEXT_TOOLS and ctx_missing and (tool != "publish" or (caps or 0) >= 1):
            stamped = wiring.collect(cwd, sid, gh=tool in GH_TOOLS)
            if stamped:
                out["context"] = stamped
                changed = True

        refs = out.get("refs")
        if tool == "find" and (caps or 0) >= 1 and isinstance(refs, dict):
            commits = refs.get("commits")
            commits = commits if isinstance(commits, list) else [commits]
            prs = prs_of_commits(cwd, commits)
            if prs:
                refs = dict(refs)
                if _add_prs(refs, prs):
                    out["refs"] = refs
                    changed = True
        return out if changed else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# cardinal-storyboard CLI
# ---------------------------------------------------------------------------

CLI_DOC = """cardinal-storyboard — local facts for Cardinal Investigation Storyboards ({agent}).

Commands:
  context [--cwd DIR] [--session-id ID] [--bare]
      Print {{"context": {{...}}}} on one line (with --bare, only the inner
      {{...}}): where this storyboard is being written. The INNER object is
      what storyboard__find, storyboard__create and storyboard__add_act take
      as `context`; never pass the wrapper. Fields (each only when
      known): repo, repo_path, branch (not main, master, develop or trunk),
      pr_number and pr_url (the branch's PR, from `gh`, cached), head_sha,
      workdir_hash, client ("{runtime}/<plugin version>"), actor_email (the
      user_email cardinal-connect stored; never git config), paths (the
      repo-relative files this session edited, with --session-id{session_env}).
      Never an absolute path. Always exits 0.

  discover [--cwd DIR] [--json]
      Print the block session start injects: the storyboards that may
      relate to this PR, branch or commit, at most 3 and 2 KB, framed as
      data written by org members. Nothing (or {{"block": null}} with
      --json) when not connected, outside a repo, or when nothing matches.
      Always exits 0; the network part has a hard 2 s deadline.
"""


def cli_main(argv: Optional[list], wiring: Wiring, *, agent: str,
             session_env: Iterable[str] = (), out=None) -> int:
    """`cardinal-storyboard` for one adapter."""
    out = sys.stdout if out is None else out
    session_env = tuple(session_env)
    env_note = f" or ${' / $'.join(session_env)}" if session_env else ""
    parser = argparse.ArgumentParser(
        prog="cardinal-storyboard",
        description=CLI_DOC.format(agent=agent, runtime=wiring.runtime, session_env=env_note),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="{context,discover}")
    context = sub.add_parser("context", help="print the context for storyboard__find / create / add_act")
    context.add_argument("--cwd", help="directory to describe (default: the current directory)")
    context.add_argument("--session-id", help="this session's id: adds the files it edited as paths")
    context.add_argument("--bare", action="store_true",
                         help="print only the context object (what the tools take as `context`)")
    discover = sub.add_parser("discover", help="print the storyboards this PR, branch or commit already has")
    discover.add_argument("--cwd", help="directory to look from (default: the current directory)")
    discover.add_argument("--json", action="store_true", help='print {"block": <text or null>}')
    args = parser.parse_args(argv)
    if args.command == "context":
        sid = args.session_id
        if not sid:
            for key in session_env:
                if wiring.environ.get(key):
                    sid = wiring.environ[key]
                    break
        try:
            ctx = wiring.collect(args.cwd or os.getcwd(), sid, gh=True)
        except Exception:
            ctx = {}
        body = ctx if args.bare else {"context": ctx}
        out.write(json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    if args.command == "discover":
        block = None
        try:
            conn = wiring.connection()
            if _connected(conn):
                block = storyboard_discovery.discover(
                    args.cwd or os.getcwd(), conn=conn, session_id=None, state_dir=None,
                    event=storyboard_discovery.SESSION_START,
                    pr_resolver=wiring.pr_resolver(gh=False),
                    deadline=time.monotonic() + storyboard_discovery.DEADLINE_S,
                    client=wiring.client, caps_path=wiring.caps_path)
        except Exception:
            block = None
        if args.json:
            out.write(json.dumps({"block": block}) + "\n")
        elif block:
            out.write(block + "\n")
        return 0
    parser.print_help()
    return 2


def launcher_main(runtime: str, agent: str, home_name: str, version_fn: Callable[[], Any],
                  cli_path: str, session_env: Iterable[str] = ()) -> None:
    """The body of an adapter's scripts/cardinal-storyboard: exits via
    os._exit (discover may leave an abandoned request thread behind its
    deadline)."""
    code = 0
    try:
        try:
            version = version_fn()
        except Exception:
            version = None
        paths = AgentPaths(home=Path(os.environ.get("HOME") or str(Path.home())) / home_name)
        wiring = Wiring(runtime, paths, version if isinstance(version, str) else None, cli=cli_path)
        code = cli_main(None, wiring, agent=agent, session_env=session_env)
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 0
    except Exception:
        code = 0
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(code)
