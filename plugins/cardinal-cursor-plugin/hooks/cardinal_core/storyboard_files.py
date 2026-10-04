"""Storyboard files: the repo-relative files a session edited.

A storyboard act records where it was written from (storyboard_context); the
files the session edited are part of that provenance (`context.paths`,
conductor storyboard/context.ts, written_from path refs). The adapter's
post-tool hook calls `record` after every successful file edit, and
`storyboard_context.collect` reads them back with `for_repo` when it stamps a
context for the same session.

Harness-neutral: the adapter passes its own state directory
(Claude Code: ~/.claude/cardinal/storyboard-files/).

Contract:
  - One JSON file per session, <state_dir>/<session>.json, mode 0600 in a
    0700 directory: {"files": [{"repo", "path"}, ...]} most recent first,
    deduplicated, at most MAX_ENTRIES; plus "roots", the git toplevel and
    repo of the directories already resolved (at most MAX_ROOTS), so the
    next edit in the same directory costs no git call.
  - Only repo-relative paths of files inside a git work tree whose origin
    is a recognisable repo are recorded: never an absolute path, never a
    path outside the toplevel.
  - Local only: nothing here touches the network. Never raises.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

from .paths import atomic_write_json_compact, read_json, safe_session

MAX_ENTRIES = 200
MAX_ROOTS = 50
MAX_PATH = 512
DEFAULT_LIMIT = 50
STATE_TTL_S = 7 * 86400


def _state_path(state_dir: Path, session_id: str) -> Path:
    return Path(state_dir) / f"{safe_session(session_id)}.json"


def _load(path: Path) -> dict:
    data = read_json(path) if path.is_file() else {}
    files = data.get("files")
    roots = data.get("roots")
    return {
        "files": [f for f in files if _valid_entry(f)] if isinstance(files, list) else [],
        "roots": roots if isinstance(roots, dict) else {},
    }


def _valid_entry(f: Any) -> bool:
    return (isinstance(f, dict) and isinstance(f.get("repo"), str) and f["repo"]
            and isinstance(f.get("path"), str) and valid_rel_path(f["path"]))


def valid_rel_path(path: Any) -> bool:
    """A repo-relative file path maestro accepts: relative, no `..`, no NUL
    or control characters, at most MAX_PATH characters."""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH:
        return False
    if path.startswith("/") or path.startswith("./") or "\\" in path:
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        return False
    parts = path.split("/")
    return all(p not in ("", ".", "..") for p in parts)


def _root_for(directory: str, roots: dict) -> Optional[tuple]:
    """(toplevel realpath, repo) of directory, from `roots` or git."""
    hit = roots.get(directory)
    if isinstance(hit, list) and len(hit) == 2 and all(isinstance(x, str) and x for x in hit):
        return hit[0], hit[1]
    from . import storyboard_context
    from .initiative import git

    top = git(["rev-parse", "--show-toplevel"], directory)
    if not top:
        return None
    repo = storyboard_context._repo(directory)
    if not repo:
        return None
    top = os.path.realpath(top)
    if len(roots) >= MAX_ROOTS:
        roots.pop(next(iter(roots)))
    roots[directory] = [top, repo]
    return top, repo


def relative_to_repo(file_path: str, cwd: Optional[str] = None, roots: Optional[dict] = None) -> Optional[tuple]:
    """(repo, repo-relative path) of file_path (absolute, or relative to
    cwd), or None outside a git work tree with a recognisable origin. The
    file need not exist yet (a Write creates it); its directory must."""
    if not isinstance(file_path, str) or not file_path or "\0" in file_path:
        return None
    path = file_path if os.path.isabs(file_path) else os.path.join(cwd or os.getcwd(), file_path)
    real = os.path.realpath(path)
    directory = os.path.dirname(real)
    if not os.path.isdir(directory):
        return None
    found = _root_for(directory, {} if roots is None else roots)
    if found is None:
        return None
    top, repo = found
    rel = os.path.relpath(real, top)
    if rel.startswith("..") or os.path.isabs(rel):
        return None
    rel = rel.replace(os.sep, "/")
    return (repo, rel) if valid_rel_path(rel) else None


def record(state_dir: Optional[Path], session_id: Optional[str], file_path: Any,
           cwd: Optional[str] = None, now: Optional[float] = None) -> bool:
    """Remember that this session edited file_path. True when recorded.
    Prunes other sessions' files older than STATE_TTL_S. Never raises."""
    try:
        if state_dir is None or not isinstance(session_id, str) or not session_id:
            return False
        state_dir = Path(state_dir)
        path = _state_path(state_dir, session_id)
        state = _load(path)
        found = relative_to_repo(file_path, cwd, state["roots"])
        if found is None:
            return False
        repo, rel = found
        entry = {"repo": repo, "path": rel}
        files = [entry] + [f for f in state["files"] if f != entry]
        state["files"] = files[:MAX_ENTRIES]
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(state_dir, 0o700)
        except OSError:
            pass
        atomic_write_json_compact(path, state)  # mkstemp: created 0600
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _prune(state_dir, path, time.time() if now is None else now)
        return True
    except Exception:
        return False


def _prune(state_dir: Path, keep: Path, now: float) -> None:
    try:
        for old in state_dir.iterdir():
            if old == keep:
                continue
            try:
                if now - old.stat().st_mtime > STATE_TTL_S:
                    old.unlink()
            except OSError:
                pass
    except OSError:
        pass


def for_repo(state_dir: Optional[Path], session_id: Optional[str], repo: Optional[str],
             limit: int = DEFAULT_LIMIT) -> list:
    """The paths this session edited in `repo`, most recent first, at most
    `limit`. [] when unknown. Never raises."""
    try:
        if state_dir is None or not isinstance(session_id, str) or not session_id or not repo:
            return []
        files = _load(_state_path(Path(state_dir), session_id))["files"]
        return [f["path"] for f in files if f["repo"] == repo][:max(0, limit)]
    except Exception:
        return []
