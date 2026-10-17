"""Additive session repository provenance. Hooks stage labels; the poller sends them.

Never parse shell source or upload local paths. Replaying the existing edited-file
ledger repairs older sessions, including sessions launched outside Git.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import investigation_events as ie
from .paths import AgentPaths, read_json
from .storyboard_context import REPO_RE

MAX_REPOS = 32
RUNTIMES = ("claude", "codex", "cursor", "gemini")
OFF = ("0", "false", "off", "no")


def disabled(home, runtime, env=None):
    env = os.environ if env is None else env
    value = env.get("CARDINAL_STORYBOARD_CONTEXT")
    if value is None and runtime == "claude":
        settings = read_json(Path(home) / ".claude/settings.json").get("env", {})
        value = settings.get("CARDINAL_STORYBOARD_CONTEXT") if isinstance(settings, dict) else None
    return isinstance(value, str) and value.strip().lower() in OFF


def path(home, sid):
    return ie.sessions_dir(home) / f"{ie.binding_path(home, sid).stem}.repos.json"


def pending(home, sid, org):
    saved = read_json(path(home, sid))
    values = saved.get("repos", []) if saved.get("org") == org else []
    return [c for c in values if isinstance(c, dict) and valid_repo(c.get("repo"))][:MAX_REPOS]


def valid_repo(repo):
    return isinstance(repo, str) and len(repo) <= 200 and REPO_RE.fullmatch(repo) is not None


def stage(home, sid, context, *, runtime, env=None):
    """Stage once per repo on this author's bound session. Local I/O only."""
    from . import investigation_bootstrap as boot
    try:
        if runtime not in RUNTIMES or not ie.valid_session(sid) or disabled(home, runtime, env):
            return False
        if not valid_repo(context.get("repo")):
            return False
        with ie.locked(home, sid, wait=0.2) as locked:
            b = ie.read_binding(home, sid)
            if not locked or not b or b.get("is_author") is False:
                return False
            contexts = pending(home, sid, b.get("org"))
            if any(c["repo"] == context["repo"] for c in contexts) or len(contexts) >= MAX_REPOS:
                return False
            # Only allow path-free provenance fields, never arbitrary tool input.
            from .storyboard_context import CONTEXT_FIELDS
            clean = {k: context[k] for k in CONTEXT_FIELDS if context.get(k) is not None}
            contexts.append(clean)
            ie.write_json(path(home, sid), {"org": b.get("org"), "repos": contexts})
            boot.refresh_path(home, sid).touch()
            return True
    except Exception:
        return False


def replay(home, sid, runtime, client, *, env=None):
    """Retroactively stage every repo in this runtime's saved edit ledger."""
    files = AgentPaths(home=Path(home) / f".{runtime}").runtime_dir / "storyboard-files"
    data = read_json(files / f"{sid}.json")
    for entry in data.get("files", [])[:200]:
        if isinstance(entry, dict):
            stage(home, sid, {"repo": entry.get("repo"), "client": client}, runtime=runtime, env=env)


def observe(call, home, *, env=None):
    """Explicit tool working directories and file arguments; no shell guessing."""
    runtime = "claude" if call.runtime == "claude-code" else call.runtime
    if runtime not in ("claude", "codex", "cursor", "gemini") or disabled(home, runtime, env):
        return
    if not ie.read_binding(home, call.session_id or "") or call.error:
        return
    args = call.tool_input if isinstance(call.tool_input, dict) else {}
    directories = [args.get("workdir"), args.get("cwd"), call.cwd]
    for key in ("file_path", "notebook_path", "path"):
        value = args.get(key)
        if isinstance(value, str) and value:
            directories.insert(0, os.path.dirname(os.path.join(call.cwd or os.getcwd(), value)))
    from . import storyboard_context
    for directory in dict.fromkeys(d for d in directories if isinstance(d, str) and d):
        # No associations_api: no repo_host from here. (`investigation repos
        # --cwd` stages wiring.collect, which may carry it: the bootstrap strips
        # it at send time unless the origin is at associations_api >= 2.)
        context = storyboard_context.collect(directory, client=call.client or runtime)
        stage(home, call.session_id, context, runtime=runtime, env=env)
