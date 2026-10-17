"""Storyboard context: where a storyboard (or one of its acts) was written.

`collect(cwd, ...)` returns the labels an agent passes as `context` to
Cardinal's storyboard__create, storyboard__find and storyboard__add_act, so a
later session in the same repo, branch, PR or directory can find the
storyboard and add an act to it instead of starting a duplicate.

Harness-neutral: every adapter's launcher calls this with its own `client`,
actor email and PR resolver (Claude Code: bin/cardinal-storyboard context).

Contract (conductor packages/maestro/src/storyboard/context.ts sanitizes the
same fields again, server-side; this side only avoids sending junk):
  - Keys come from CONTEXT_FIELDS (plus `paths`) only. A key whose value is unknown or
    invalid is omitted, never sent as null.
  - Never an absolute path. `repo_path` is relative to the git toplevel
    ("." at the root); the directory itself is identified only by
    `workdir_hash` = the first 32 hex of sha256(hostname + "\\0" + realpath).
  - Never raises: a failing git, a slow `gh`, a resolver that throws, each
    just drops the fields it would have produced.
  - Labels only, never authorization: maestro decides who may update what.
  - `branch` is omitted on a protected branch (main, master, develop,
    trunk): find never looks a default branch up, and a storyboard written
    on main is not "about" main.
  - `paths`: the repo-relative files this session edited in this repo
    (storyboard_files), only when the caller knows the session
    (`edited_paths`). They are where the act was written from, never what it
    is about.
  - `repo_host`: the forge hostname of `origin` (github.com,
    gitlab.example.com), lowercased, no port, no userinfo. Sent only with
    `repo`, and only when the caller passes the target origin's cached
    `associations_api` >= REPO_HOST_API (storyboard_discovery.read_caps):
    every strict write path of an older maestro (create, add_act, publish,
    ensure-session-investigation) answers 400 to an unknown context key, so
    the gate lives here, for every caller. None (unknown) counts as 0.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import urllib.parse
from typing import Any, Callable, Optional

from .initiative import PROTECTED_BRANCHES, REMOTE_URL_RE, canonical_repo, git

# The context fields maestro stores (conductor storyboard/context.ts). There is
# deliberately no cwd: an absolute path is PII and not needed for matching.
CONTEXT_FIELDS = (
    "repo", "repo_host", "repo_path", "branch", "pr_number", "pr_url", "head_sha",
    "workdir_hash", "client", "actor_email",
)
# The associations_api version from which maestro accepts `repo_host`.
REPO_HOST_API = 2

REPO_RE = re.compile(r"^[a-z0-9_.-]+(/[a-z0-9_.-]+)+$")
HEAD_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
WORKDIR_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
CLIENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}(/[A-Za-z0-9._+-]{1,64})?$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
BRANCH_BAD_RE = re.compile(r"[\s\x00-\x1f\x7f]")
# A DNS label: letters, digits and '-', not at either end (maestro's rule).
HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
HOST_MAX = 253
PR_MAX = 2 ** 31 - 1
# conductor storyboard/context.ts MAX_CONTEXT_PATHS.
MAX_PATHS = 50

PrResolver = Callable[[str, str, str], Any]
# edited_paths(repo) -> the repo-relative files this session edited in repo.
EditedPaths = Callable[[str], Any]


def workdir_hash(realpath: str, hostname: Optional[str] = None) -> str:
    """First 32 hex of sha256(hostname + NUL + realpath): the same directory on
    the same machine hashes the same, and the path never leaves the machine."""
    host = hostname if hostname is not None else socket.gethostname()
    return hashlib.sha256(f"{host}\0{realpath}".encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _https_port(remote_url: str) -> bool:
    """http(s)://[user@]host:port/o/r: initiative.canonical_repo reads the
    port as the owner ("8443/o/r"), so this form is parsed with urllib.parse
    instead. canonical_repo itself is unchanged (spend attribution)."""
    m = REMOTE_URL_RE.match(remote_url)
    return bool(m) and not remote_url.startswith("git@") and remote_url[m.end(1):m.end(1) + 1] == ":"


def _parsed(remote_url: str) -> bool:
    """Whether remote_url is parsed with urllib.parse rather than
    REMOTE_URL_RE: ssh://[user@]host[:port]/o/r and http(s) with a port."""
    return remote_url.lower().startswith("ssh://") or _https_port(remote_url)


def _split(remote_url: str) -> Optional[urllib.parse.SplitResult]:
    """remote_url split, or None when it does not parse (a bad port, an
    unterminated IPv6 literal)."""
    try:
        parts = urllib.parse.urlsplit(remote_url)
        parts.port, parts.hostname  # noqa: B018 -- both raise ValueError on junk
        return parts
    except ValueError:
        return None


def repo_of_url(remote_url: Optional[str]) -> Optional[str]:
    """The canonical `owner/name` of a remote URL (initiative.canonical_repo,
    plus ssh://[user@]host[:port]/owner/name and https://host:port/owner/name
    via urllib.parse), lowercased; None when it is not a forge URL."""
    if not isinstance(remote_url, str):
        return None
    url = remote_url.strip()
    if _parsed(url):
        parts = _split(url)
        path = parts.path.strip("/") if parts is not None else ""
        if path.endswith(".git"):
            path = path[:-4]
        repo = path if "/" in path else None
    else:
        repo = canonical_repo(url)
    if not repo:
        return None
    repo = repo.strip().lower()
    return repo if len(repo) <= 200 and REPO_RE.match(repo) else None


def host_of_url(remote_url: Optional[str]) -> Optional[str]:
    """The forge hostname of a remote URL: `git@host:o/r` and
    `https://host/o/r` (REMOTE_URL_RE group 1), `ssh://[user@]host[:port]/o/r`
    and `https://host:port/o/r` (urllib.parse). Lowercased, without port or
    userinfo; None unless it is a hostname of [a-z0-9-] labels, at most
    HOST_MAX characters."""
    if not isinstance(remote_url, str):
        return None
    url = remote_url.strip()
    if _parsed(url):
        parts = _split(url)
        host = parts.hostname if parts is not None else None  # no userinfo, no port, lowercased
    else:
        m = REMOTE_URL_RE.match(url)
        host = m.group(1) if m else None
        if host is not None:
            host = host.rsplit("@", 1)[-1]  # https://user@host/o/r
    if not host:
        return None
    host = host.lower().rstrip(".")
    if not host or len(host) > HOST_MAX:
        return None
    return host if all(HOST_LABEL_RE.match(label) for label in host.split(".")) else None


def _repo(cwd: str) -> Optional[str]:
    return repo_of_url(git(["remote", "get-url", "origin"], cwd))


def _repo_host(cwd: str) -> Optional[str]:
    return host_of_url(git(["remote", "get-url", "origin"], cwd))


def sends_repo_host(associations_api: Any) -> bool:
    """Whether the target maestro accepts `repo_host` (associations_api >=
    REPO_HOST_API). None / unknown / malformed counts as 0."""
    return (isinstance(associations_api, int) and not isinstance(associations_api, bool)
            and associations_api >= REPO_HOST_API)


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


def _paths(edited_paths: EditedPaths, repo: str) -> Optional[list]:
    from .storyboard_files import valid_rel_path

    try:
        found = edited_paths(repo)
    except Exception:
        return None
    if not isinstance(found, (list, tuple)):
        return None
    out: list = []
    for p in found:
        if valid_rel_path(p) and p not in out:
            out.append(p)
        if len(out) == MAX_PATHS:
            break
    return out or None


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
    edited_paths: Optional[EditedPaths] = None,
    associations_api: Optional[int] = None,
) -> dict:
    """The context dict for cwd (see the module docstring). Never raises.

    pr_resolver(cwd, repo, branch) -> (number, url) looks up the branch's PR;
    None skips the lookup. decisions.resolve_pr (gh, cached) is the usual one,
    see default_pr_resolver. edited_paths(repo) -> [repo-relative path]: the
    files this session edited (storyboard_files.for_repo); None (the session
    is unknown) sends no `paths`. associations_api: the cached server
    capability of the origin this context is sent to
    (storyboard_discovery.read_caps); `repo_host` only at >= REPO_HOST_API,
    None (unknown) never."""
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
            if branch in PROTECTED_BRANCHES:
                branch = None
            ctx.update({
                "repo": repo,
                "repo_host": _repo_host(cwd) if repo and sends_repo_host(associations_api) else None,
                "repo_path": _repo_path(cwd),
                "branch": branch,
                "head_sha": _head_sha(cwd),
            })
            if pr_resolver is not None and repo and branch:
                ctx["pr_number"], ctx["pr_url"] = _pr(pr_resolver, cwd, repo, branch)
    except Exception:
        pass
    paths = None
    if edited_paths is not None and isinstance(ctx.get("repo"), str):
        paths = _paths(edited_paths, ctx["repo"])
    if isinstance(client, str) and CLIENT_RE.match(client):
        ctx["client"] = client
    ctx["actor_email"] = _email(actor_email)
    ctx = {k: ctx[k] for k in CONTEXT_FIELDS if ctx.get(k) is not None}
    if "workdir_hash" in ctx and not WORKDIR_HASH_RE.match(ctx["workdir_hash"]):
        del ctx["workdir_hash"]
    out = _no_paths(ctx, real)
    if paths and "repo" in out:
        out["paths"] = paths
    return out


def default_pr_resolver(cache_dir) -> PrResolver:
    """decisions.resolve_pr with the adapter's cache directory: `gh pr view`
    for the branch, cached per repo + branch, bounded by its timeout."""
    from . import decisions

    def resolve(cwd: str, repo: str, branch: str):
        return decisions.resolve_pr(cwd, repo, branch, cache_dir)

    return resolve


def cache_only_pr_resolver(cache_dir, now=None) -> PrResolver:
    """The branch's PR from decisions.resolve_pr's cache (prs.json), within
    the same TTLs, and never `gh`: for a hook that must not spend seconds on
    a subprocess (storyboard_discovery). A miss or a stale entry is no PR."""
    import time
    from pathlib import Path

    from . import decisions

    def resolve(cwd: str, repo: str, branch: str):  # noqa: ARG001
        if not repo or not branch or branch in decisions.PROTECTED_BRANCHES:
            return None, None
        entry = decisions.read_json(Path(cache_dir) / "prs.json").get(f"{repo}#{branch}")
        if not isinstance(entry, dict):
            return None, None
        try:
            age = (time.time() if now is None else now) - float(entry.get("at") or 0)
        except (TypeError, ValueError):
            return None, None
        ttl = decisions.PR_CACHE_TTL_SEC if entry.get("number") else decisions.PR_NEGATIVE_TTL_SEC
        if not 0 <= age < ttl:
            return None, None
        return entry.get("number"), entry.get("url")

    return resolve
