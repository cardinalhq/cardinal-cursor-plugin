"""Storyboard context: where a storyboard (or one of its acts) was written.

`collect(cwd, ...)` returns the labels an agent passes as `context` to
Cardinal's storyboard__create, storyboard__find and storyboard__add_act, so a
later session in the same repo, branch, PR or directory can find the
storyboard and add an act to it instead of starting a duplicate.

Harness-neutral: every adapter's launcher calls this with its own `client`,
actor email and PR resolver (Claude Code: bin/cardinal-storyboard context).

Contract (conductor packages/maestro/src/storyboard/context.ts sanitizes the
same fields again, server-side; this side only avoids sending junk):
  - Keys come from CONTEXT_FIELDS only. A key whose value is unknown or
    invalid is omitted, never sent as null.
  - Never an absolute path. `repo_path` is relative to the git toplevel
    ("." at the root); the directory itself is identified only by
    `workdir_hash` = the first 32 hex of sha256(hostname + "\\0" + realpath).
  - Never raises: a failing git, a slow `gh`, a resolver that throws, each
    just drops the fields it would have produced.
  - Labels only, never authorization: maestro decides who may update what.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
from typing import Any, Callable, Optional

from .initiative import canonical_repo, git

# The context fields maestro stores (conductor storyboard/context.ts). There is
# deliberately no cwd: an absolute path is PII and not needed for matching.
CONTEXT_FIELDS = (
    "repo", "repo_path", "branch", "pr_number", "pr_url", "head_sha",
    "workdir_hash", "client", "actor_email",
)

REPO_RE = re.compile(r"^[a-z0-9_.-]+(/[a-z0-9_.-]+)+$")
HEAD_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
WORKDIR_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
CLIENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}(/[A-Za-z0-9._+-]{1,64})?$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
BRANCH_BAD_RE = re.compile(r"[\s\x00-\x1f\x7f]")
PR_MAX = 2 ** 31 - 1

PrResolver = Callable[[str, str, str], Any]


def workdir_hash(realpath: str, hostname: Optional[str] = None) -> str:
    """First 32 hex of sha256(hostname + NUL + realpath): the same directory on
    the same machine hashes the same, and the path never leaves the machine."""
    host = hostname if hostname is not None else socket.gethostname()
    return hashlib.sha256(f"{host}\0{realpath}".encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _repo(cwd: str) -> Optional[str]:
    repo = canonical_repo(git(["remote", "get-url", "origin"], cwd))
    if not repo:
        return None
    repo = repo.strip().lower()
    return repo if len(repo) <= 200 and REPO_RE.match(repo) else None


def _repo_path(cwd: str) -> Optional[str]:
    # --show-prefix prints "" at the toplevel, which git() reports as None:
    # the caller has already established that cwd is inside a work tree.
    prefix = git(["rev-parse", "--show-prefix"], cwd) or ""
    path = prefix.strip().rstrip("/") or "."
    if (
        len(path) > 512
        or path.startswith("/")
        or "\0" in path
        or ".." in path.split("/")
    ):
        return None
    return path


def _branch(cwd: str) -> Optional[str]:
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if not branch or branch == "HEAD" or len(branch) > 255 or BRANCH_BAD_RE.search(branch):
        return None
    return branch


def _head_sha(cwd: str) -> Optional[str]:
    sha = git(["rev-parse", "HEAD"], cwd)
    return sha if sha and HEAD_SHA_RE.match(sha) else None


def _pr(resolver: PrResolver, cwd: str, repo: str, branch: str) -> tuple:
    try:
        found = resolver(cwd, repo, branch)
        number, url = found if isinstance(found, (tuple, list)) and len(found) == 2 else (None, None)
    except Exception:
        return None, None
    if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= PR_MAX:
        return None, None
    if not (isinstance(url, str) and url.startswith("https://") and len(url) <= 512):
        url = None
    return number, url


def _email(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    return email if len(email) <= 254 and EMAIL_RE.match(email) else None


def _no_paths(ctx: dict, real: str) -> dict:
    """Belt and braces for the contract: nothing that is, or carries, an
    absolute path of this machine leaves it."""
    out = {}
    real = real if len(real) > 1 else ""  # "/" itself would match every URL
    for key, value in ctx.items():
        if isinstance(value, str) and (value.startswith("/") or (real and real in value)):
            continue
        out[key] = value
    return out


def collect(
    cwd: str,
    *,
    client: Optional[str],
    actor_email: Optional[str] = None,
    hostname: Optional[str] = None,
    pr_resolver: Optional[PrResolver] = None,
) -> dict:
    """The context dict for cwd (see the module docstring). Never raises.

    pr_resolver(cwd, repo, branch) -> (number, url) looks up the branch's PR;
    None skips the lookup. decisions.resolve_pr (gh, cached) is the usual one,
    see default_pr_resolver."""
    ctx: dict = {}
    try:
        real = os.path.realpath(cwd)
    except Exception:
        real = ""
    try:
        if real:
            ctx["workdir_hash"] = workdir_hash(real, hostname)
    except Exception:
        pass
    try:
        if git(["rev-parse", "--is-inside-work-tree"], cwd) == "true":
            repo = _repo(cwd)
            branch = _branch(cwd)
            ctx.update({
                "repo": repo,
                "repo_path": _repo_path(cwd),
                "branch": branch,
                "head_sha": _head_sha(cwd),
            })
            if pr_resolver is not None and repo and branch:
                ctx["pr_number"], ctx["pr_url"] = _pr(pr_resolver, cwd, repo, branch)
    except Exception:
        pass
    if isinstance(client, str) and CLIENT_RE.match(client):
        ctx["client"] = client
    ctx["actor_email"] = _email(actor_email)
    ctx = {k: ctx[k] for k in CONTEXT_FIELDS if ctx.get(k) is not None}
    if "workdir_hash" in ctx and not WORKDIR_HASH_RE.match(ctx["workdir_hash"]):
        del ctx["workdir_hash"]
    return _no_paths(ctx, real)


def default_pr_resolver(cache_dir) -> PrResolver:
    """decisions.resolve_pr with the adapter's cache directory: `gh pr view`
    for the branch, cached per repo + branch, bounded by its timeout."""
    from . import decisions

    def resolve(cwd: str, repo: str, branch: str):
        return decisions.resolve_pr(cwd, repo, branch, cache_dir)

    return resolve
