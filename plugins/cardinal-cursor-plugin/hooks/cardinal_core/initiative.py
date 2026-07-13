"""Initiative resolution from git facts — pure logic, no I/O beyond git.

One branch = one initiative. Branch names following the
`<type-prefix>/<kebab-name>` convention classify exactly; protected
branches are research/scoping; everything else gets a stable name with
type 'feature'.

Kept in lockstep with conductor's normalizeInitiativeName — this is now
the single client-side copy (spec §Core module inventory).
"""

from __future__ import annotations

import re
import subprocess

PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "trunk"})

# Noise words that appear between `worktree-` and the real name in
# EnterWorktree-style branches.
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

REMOTE_URL_RE = re.compile(r"(?:git@|https?://)([^:/]+)[:/]([^/]+)/(.+?)(?:\.git)?/?$")

COMMAND_RE = re.compile(r"^\s*/([A-Za-z0-9][\w:-]*)")
COMMAND_TAG_RE = re.compile(r"<command-name>\s*/?([\w:-]+)\s*</command-name>")


def strip_worktree_noise(name: str) -> str:
    """worktree-fix-1018-github-app-repo-picker → github-app-repo-picker.
    Conservative: non-worktree names pass through verbatim; if nothing
    real remains after the head, keep the original."""
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
    """(initiative_name, initiative_type) for a branch name."""
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


def canonical_repo(remote_url: str | None) -> str | None:
    """'git@github.com:org/name.git' → 'org/name'."""
    if not remote_url:
        return None
    m = REMOTE_URL_RE.match(remote_url.strip())
    if not m:
        return None
    name = re.sub(r"\.git$", "", m.group(3))
    return f"{m.group(2)}/{name}" if m.group(2) and name else None


def detect_command(prompt: object) -> str | None:
    """'/code-review --fix' → 'code-review'. Accepts the raw typed form
    (anchored at start) and the expanded <command-name> tag form."""
    if not isinstance(prompt, str):
        return None
    m = COMMAND_RE.match(prompt)
    if m:
        return m.group(1)
    m = COMMAND_TAG_RE.search(prompt)
    if m:
        return m.group(1)
    return None


def git(args: list[str], cwd: str, timeout: float = 1.0) -> str | None:
    """Best-effort git invocation; None on any failure."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def is_git_repo(cwd: str) -> bool:
    return git(["rev-parse", "--is-inside-work-tree"], cwd) == "true"


def git_facts(cwd: str) -> tuple[str | None, str | None]:
    """(repo 'org/name', branch) for cwd — best-effort."""
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    remote = git(["remote", "get-url", "origin"], cwd)
    return canonical_repo(remote), branch
