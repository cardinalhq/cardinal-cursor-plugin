"""Decision telemetry: the shared half of `cardinal.decision`.

An agent records a decision while it works: the question the work
raised, the option it chose, why, and the options it rejected. Every
decision is tagged so it can be found again by engineer, session, repo,
branch, PR, and the code it concerns. This module owns what every
adapter shares:

- validating and capping the fields (a decision is a short record, not
  a transcript excerpt);
- parsing anchors into the invariant-harness Anchor shape
  (`{kind, identifier, path}`, invariant_harness/schemas/anchor.py);
- deriving D18 code-cluster ids from anchor paths (ported from
  invariant_harness/clustering_d18.py). Clusters are computed from the
  committed tree at HEAD, so the same commit yields the same ids on
  every machine; ids are only comparable within one `cluster_scheme`;
- resolving the branch's PR with `gh`, cached per repo + branch;
- the per-session ledger the prompt hook shows the agent so new
  decisions can link to earlier ones;
- the opt-in switch (off by default);
- the OTLP log attributes lakerunner's agent-sessions processor reads
  (dots become underscores on ingest).

Adapters own the CLI, prompt wording, and emission. Python 3.9
compatible (py39-support.yml imports every core module).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional, Sequence

from .initiative import PROTECTED_BRANCHES, git
from .paths import atomic_write_json_compact, read_json, safe_session

DECISION_EVENT = "cardinal.decision"

# Env var (process env or the adapter's settings env block) that forces
# capture on ("1"/"true"/"on") or off ("0"/"false"/"off"), overriding
# the user's `on`/`off` choice.
ENABLE_ENV = "CARDINAL_DECISIONS"

DECIDED_BY = ("user", "agent")
LINK_RELATIONS = ("follows_from", "refines", "supersedes")
# invariant_harness/schemas/anchor.py AnchorKind, verbatim.
ANCHOR_KINDS = (
    "symbol", "interface", "schema", "config", "workflow", "benchmark",
    "directory", "file",
)
_PATH_KINDS = frozenset({"file", "directory"})

DECISION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

MAX_QUESTION = 300
MAX_CHOICE = 200
MAX_RATIONALE = 1000
MAX_ALTERNATIVE = 200
MAX_ALTERNATIVES = 8
MAX_ANCHORS = 20
MAX_LINKS = 8
MAX_CLUSTERS = 20


class DecisionError(ValueError):
    """Invalid decision input. The message is shown to the agent verbatim."""


# --- fields -----------------------------------------------------------------


def slugify(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "decision"


def clip(value: Optional[str], limit: int) -> Optional[str]:
    """Whitespace-collapsed, length-capped text; None when empty."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _normalize_id(value: str, flag: str) -> str:
    ident = value.strip().lower()
    if not DECISION_ID_RE.match(ident):
        raise DecisionError(
            f"{flag} {value!r} is not a decision id "
            "(lowercase letters, digits, '.', '_', '-'; max 64 chars)"
        )
    return ident


def _unique_id(base: str, existing: dict[str, str], choice: str) -> str:
    """An auto-derived id must not silently replace a different earlier
    decision; suffix -2, -3, ... until free (or it names the same choice)."""
    candidate, n = base, 1
    while candidate in existing and existing[candidate] != choice:
        n += 1
        candidate = f"{base[:60]}-{n}"
    return candidate


def build_decision(
    *,
    choice: Optional[str],
    question: Optional[str] = None,
    rationale: Optional[str] = None,
    decided_by: str = "agent",
    alternatives: Iterable[str] = (),
    follows_from: Iterable[str] = (),
    refines: Iterable[str] = (),
    supersedes: Iterable[str] = (),
    anchors: Sequence[dict[str, str]] = (),
    decision_id: Optional[str] = None,
    existing: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Validate and cap one decision. `existing` is the session ledger:
    an explicit `decision_id` that is already there is a deliberate
    revision; an auto-derived one is made unique."""
    choice_text = clip(choice, MAX_CHOICE)
    if not choice_text:
        raise DecisionError("--choice is required: the option that was chosen, in a few words")
    if decided_by not in DECIDED_BY:
        raise DecisionError(f"--by must be one of {', '.join(DECIDED_BY)}")

    by_id = {str(e.get("id")): str(e.get("choice")) for e in existing if e.get("id")}
    if decision_id:
        ident = _normalize_id(decision_id, "--id")
    else:
        ident = _unique_id(slugify(choice_text), by_id, choice_text)

    links: list[dict[str, str]] = []
    for relation, targets in (
        ("follows_from", follows_from), ("refines", refines), ("supersedes", supersedes),
    ):
        for target in targets:
            to = _normalize_id(target, f"--{relation.replace('_from', 's').replace('_', '-')}")
            if to == ident:
                raise DecisionError("a decision cannot link to itself")
            link = {"relation": relation, "to": to}
            if link not in links:
                links.append(link)

    alts = [a for a in (clip(x, MAX_ALTERNATIVE) for x in alternatives) if a]
    return {
        "id": ident,
        "question": clip(question, MAX_QUESTION),
        "choice": choice_text,
        "rationale": clip(rationale, MAX_RATIONALE),
        "decided_by": decided_by,
        "alternatives": alts[:MAX_ALTERNATIVES],
        "links": links[:MAX_LINKS],
        "anchors": list(anchors)[:MAX_ANCHORS],
    }


# --- anchors ----------------------------------------------------------------


def repo_relative(path: str, repo_root: Optional[str], cwd: str) -> str:
    """Repo-root-relative POSIX path. A path outside the repo (or with no
    repo) is kept as given, normalized."""
    raw = path.strip()
    absolute = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(cwd, raw))
    if repo_root:
        rel = os.path.relpath(absolute, os.path.realpath(repo_root))
        if not rel.startswith(".."):
            return "" if rel == "." else rel.replace(os.sep, "/")
    return os.path.normpath(raw).replace(os.sep, "/")


def parse_anchor(spec: str, repo_root: Optional[str], cwd: str) -> dict[str, str]:
    """One --anchor value → {kind, identifier[, path]}.

    Accepted forms:
      path/to/file.py              file anchor
      path/to/dir/                 directory anchor
      path/to/file.py::Name        symbol anchor located in that file
      <kind>:<identifier>          any AnchorKind, e.g. config:LAKERUNNER_X
      <kind>:<identifier>@<path>   a non-path kind located in a file
      file:<path> / directory:<path>
    """
    text = (spec or "").strip()
    if not text:
        raise DecisionError("--anchor must not be empty")
    kind, sep, rest = text.partition(":")
    if sep and kind in ANCHOR_KINDS and not rest.startswith(":"):
        rest = rest.strip()
        if not rest:
            raise DecisionError(f"--anchor {spec!r}: missing identifier after '{kind}:'")
        if kind in _PATH_KINDS:
            path = repo_relative(rest, repo_root, cwd)
            return {"kind": kind, "identifier": path, "path": path}
        identifier, at, where = rest.partition("@")
        anchor = {"kind": kind, "identifier": identifier.strip()}
        if at and where.strip():
            anchor["path"] = repo_relative(where, repo_root, cwd)
        return anchor
    if "::" in text:
        where, _, symbol = text.partition("::")
        path = repo_relative(where, repo_root, cwd)
        return {"kind": "symbol", "identifier": symbol.strip() or path, "path": path}
    kind = "directory" if text.endswith("/") else "file"
    path = repo_relative(text, repo_root, cwd)
    return {"kind": kind, "identifier": path, "path": path}


# --- D18 code clusters (ported from invariant_harness/clustering_d18.py) ----

D18_CAP = 100
D18_MIN = 10
_D18_SMALL_REPO_FILES = 300
D18_VERSION = "d18-v1"

# invariant_harness/config.py + repository.py source classification.
_CODE_EXTENSIONS = frozenset({
    ".py", ".pyi", ".go", ".rs", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".java", ".kt", ".scala", ".c", ".h", ".cc", ".cpp", ".hpp", ".rb", ".php",
    ".swift", ".sh", ".bash", ".sql",
})
_CONFIG_EXTENSIONS = frozenset({".yaml", ".yml", ".toml", ".ini", ".cfg", ".env"})
_DOC_FILENAMES = frozenset({
    "README.md", "README.rst", "README", "ARCHITECTURE.md", "DESIGN.md", "SPEC.md",
    "THESIS.md", "PLAN.md", "CONFIGURATION.md", "CONFIG.md", "CONTRIBUTING.md",
    "SECURITY.md", "GOVERNANCE.md", "CHANGELOG.md", "CHANGES.md", "AGENTS.md",
    "CLAUDE.md", "ROADMAP.md",
})
_BUILD_FILENAMES = frozenset({
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile",
    "go.mod", "go.work", "package.json", "tsconfig.json", "Cargo.toml", "pom.xml",
    "build.gradle", "build.gradle.kts", "settings.gradle", "Makefile", "makefile",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
})
_IGNORED_DIR_NAMES = frozenset({
    ".git", ".hg", ".svn", "node_modules", "vendor", "third_party", "third-party",
    "dist", "build", "out", "target", "bin", "obj", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".venv", "venv", "env", ".idea", ".vscode",
    "coverage", ".coverage", "htmlcov", ".next", ".nuxt", ".cache", ".turbo",
    "generated", "gen", "_generated", ".gocache", ".pnpm-store", "testdata", "runs",
})
_ALLOWED_DOT_DIRS = frozenset({".github", ".circleci", ".gitlab"})
_IGNORED_FILE_NAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "go.sum", "Cargo.lock",
    "poetry.lock", "Pipfile.lock", "composer.lock", ".DS_Store",
})
_IGNORED_FILE_SUFFIXES = (
    ".lock", ".min.js", ".min.css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".ico",
    ".svg", ".webp", ".pdf", ".zip", ".tar", ".tar.gz", ".tgz", ".gz", ".mp3",
    ".mp4", ".mov", ".wav", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".pyc",
    ".pyo", ".class", ".o", ".so", ".dylib", ".dll", ".exe", ".jar", ".parquet",
    ".arrow", ".bin",
)
_NON_SOURCE_SEGMENTS = frozenset({
    "test", "tests", "__tests__", "spec", "specs",
    "bench", "benchmarks", "benches",
    "example", "examples", "demo", "demos", "sample", "samples",
})


def is_source_file(rel: str) -> bool:
    """invariant-harness's `role == "source"` test on a repo-relative path
    (the harness classifies absolute paths, which lets a parent directory
    named e.g. `test` reclassify a whole repo; relative paths avoid that)."""
    p = PurePosixPath(rel)
    for directory in p.parts[:-1]:
        if directory in _IGNORED_DIR_NAMES or (
            directory.startswith(".") and directory not in _ALLOWED_DOT_DIRS
        ):
            return False
    name, suffix = p.name, p.suffix.lower()
    lowered = name.lower()
    if name in _IGNORED_FILE_NAMES or any(lowered.endswith(s) for s in _IGNORED_FILE_SUFFIXES):
        return False
    parts_lower = {part.lower() for part in p.parts}
    if name in _DOC_FILENAMES or (suffix in {".md", ".rst"} and "test" not in parts_lower):
        return False
    if name in _BUILD_FILENAMES or suffix in _CONFIG_EXTENSIONS:
        return False
    if suffix not in _CODE_EXTENSIONS:
        return False
    return not (parts_lower & _NON_SOURCE_SEGMENTS)


class _Node:
    __slots__ = ("children", "count", "is_file")

    def __init__(self) -> None:
        self.children: dict[str, _Node] = {}
        self.count = 0
        self.is_file = False


def _trie(files: Iterable[str]) -> _Node:
    root = _Node()
    for rel in files:
        node = root
        node.count += 1
        for segment in rel.split("/"):
            child = node.children.get(segment)
            if child is None:
                child = node.children[segment] = _Node()
            node = child
            node.count += 1
        node.is_file = True
    return root


def d18_autoscale(min_: int, cap: int, n_files: int) -> tuple[int, int]:
    """Scale `min` down for small repos (clustering_d18._autoscale)."""
    if n_files >= _D18_SMALL_REPO_FILES:
        return min_, cap
    if n_files >= 60:
        return max(1, min_ // 2), cap
    return 1, max(20, cap // 2)


def d18_domains(files: Sequence[str], cap: int = D18_CAP, min_: int = D18_MIN) -> list[dict[str, Any]]:
    """clustering_d18._select_domains without the per-domain file lists:
    [{name, kind, covers, file_count}], file_count DESC then name ASC."""
    root = _trie(files)
    domains: list[dict[str, Any]] = []

    def remainder(prefix: str, swept: list[tuple[str, _Node]]) -> None:
        count = sum(node.count for _, node in swept)
        if count < min_:
            return
        covers: list[str] = []
        if any(node.is_file and not node.children for _, node in swept):
            covers.append(f"{prefix}/*" if prefix else "*")
        for segment, node in sorted((s for s in swept if s[1].children), key=lambda s: s[0]):
            covers.append(f"{prefix}/{segment}/**" if prefix else f"{segment}/**")
        domains.append({
            "name": f"{prefix} (misc)" if prefix else "(misc)",
            "kind": "remainder", "covers": covers, "file_count": count,
        })

    def walk(node: _Node, prefix: str) -> None:
        if node.count < min_:
            return
        if node.count <= cap:
            leaf = node.is_file and not node.children
            domains.append({
                "name": prefix, "kind": "leaf" if leaf else "dir",
                "covers": [prefix] if leaf else [f"{prefix}/**"],
                "file_count": node.count,
            })
            return
        swept: list[tuple[str, _Node]] = []
        for segment, child in node.children.items():
            if child.count < min_:
                swept.append((segment, child))
            else:
                walk(child, f"{prefix}/{segment}")
        remainder(prefix, swept)

    root_swept: list[tuple[str, _Node]] = []
    for segment, child in root.children.items():
        if child.count < min_:
            root_swept.append((segment, child))
        else:
            walk(child, segment)
    remainder("", root_swept)

    domains.sort(key=lambda d: (-d["file_count"], d["name"]))
    return domains


def _cover_matches(cover: str, path: str) -> bool:
    if cover.endswith("/**"):
        return path.startswith(cover[:-2])
    if cover == "*":
        return "/" not in path
    if cover.endswith("/*"):
        return PurePosixPath(path).parent.as_posix() == cover[:-2]
    return path == cover


def match_clusters(domains: Sequence[dict[str, Any]], path: str, is_dir: bool = False) -> list[str]:
    """Cluster ids for one anchor path. A file belongs to at most one
    domain (D18 domains are disjoint); a directory maps to every domain
    that contains it or lives inside it. The repo root maps to nothing —
    it would tag every cluster."""
    path = path.strip("/")
    if not path:
        return []
    if not is_dir:
        for domain in domains:
            if any(_cover_matches(c, path) for c in domain["covers"]):
                return [f"d18:{domain['name']}"]
        return []
    probe = f"{path}/\x00"
    out: list[str] = []
    for domain in domains:
        base = domain["name"]
        if base.endswith(" (misc)"):
            base = base[: -len(" (misc)")]
        elif base == "(misc)":
            base = ""
        inside = bool(base) and (base == path or base.startswith(path + "/"))
        if inside or any(_cover_matches(c, probe) for c in domain["covers"]):
            out.append(f"d18:{domain['name']}")
    return out


def load_domains(
    repo_root: str, head_sha: str, cache_dir: Path, timeout: float = 5.0,
) -> Optional[tuple[list[dict[str, Any]], str]]:
    """D18 domains + scheme for the committed tree at `head_sha`, cached
    per repo + commit. None when the tree can't be listed."""
    repo_key = hashlib.sha1(os.path.realpath(repo_root).encode()).hexdigest()[:12]
    cache_path = cache_dir / f"d18-{repo_key}-{head_sha}.json"
    cached = read_json(cache_path)
    if isinstance(cached.get("domains"), list) and isinstance(cached.get("scheme"), str):
        return cached["domains"], cached["scheme"]

    listing = git(["ls-tree", "-r", "--name-only", head_sha], repo_root, timeout=timeout)
    if listing is None:
        return None
    files = [f for f in listing.splitlines() if f and is_source_file(f)]
    min_, cap = d18_autoscale(D18_MIN, D18_CAP, len(files))
    scheme = f"{D18_VERSION}:cap={cap},min={min_}"
    domains = d18_domains(files, cap=cap, min_=min_) if files else []
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json_compact(cache_path, {"scheme": scheme, "domains": domains})
        stale = sorted(cache_dir.glob(f"d18-{repo_key}-*.json"), key=lambda p: p.stat().st_mtime)
        for old in stale[:-5]:
            old.unlink()
    except OSError:
        pass
    return domains, scheme


def code_clusters(
    anchors: Sequence[dict[str, str]],
    repo_root: Optional[str],
    head_sha: Optional[str],
    cache_dir: Path,
) -> tuple[list[str], Optional[str]]:
    """(cluster ids, scheme) for the anchors that carry a path."""
    paths = [(a["path"], a.get("kind") == "directory") for a in anchors if a.get("path") is not None]
    if not paths or not repo_root or not head_sha:
        return [], None
    loaded = load_domains(repo_root, head_sha, cache_dir)
    if loaded is None:
        return [], None
    domains, scheme = loaded
    ids: list[str] = []
    for path, is_dir in paths:
        for cluster_id in match_clusters(domains, path, is_dir):
            if cluster_id not in ids:
                ids.append(cluster_id)
    return ids[:MAX_CLUSTERS], scheme


# --- PR resolution ----------------------------------------------------------

PR_CACHE_TTL_SEC = 600
PR_NEGATIVE_TTL_SEC = 120


def resolve_pr(
    cwd: str,
    repo: Optional[str],
    branch: Optional[str],
    cache_dir: Path,
    *,
    now: Optional[float] = None,
    timeout: float = 4.0,
) -> tuple[Optional[int], Optional[str]]:
    """(number, url) of the branch's most recent PR via `gh`, or (None,
    None). Cached per repo + branch; a miss is re-checked sooner than a
    hit so a PR opened mid-session is picked up."""
    if not repo or not branch or branch == "HEAD" or branch in PROTECTED_BRANCHES:
        return None, None
    now = time.time() if now is None else now
    cache_path = cache_dir / "prs.json"
    cache = read_json(cache_path)
    key = f"{repo}#{branch}"
    entry = cache.get(key)
    if isinstance(entry, dict):
        ttl = PR_CACHE_TTL_SEC if entry.get("number") else PR_NEGATIVE_TTL_SEC
        if now - float(entry.get("at") or 0) < ttl:
            return entry.get("number"), entry.get("url")

    number, url = _gh_pr_view(cwd, branch, timeout)
    cache[key] = {"at": now, "number": number, "url": url}
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json_compact(cache_path, cache)
    except OSError:
        pass
    return number, url


def _gh_pr_view(cwd: str, branch: str, timeout: float) -> tuple[Optional[int], Optional[str]]:
    try:
        out = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "number,url"],
            cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if out.returncode != 0:
        return None, None
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return None, None
    number = data.get("number") if isinstance(data, dict) else None
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return None, None
    url = data.get("url")
    return number, url if isinstance(url, str) and url else None


# --- opt-in + session ledger ------------------------------------------------


def decisions_dir(runtime_dir: Path) -> Path:
    return runtime_dir / "decisions"


def cache_dir(runtime_dir: Path) -> Path:
    return decisions_dir(runtime_dir) / "cache"


def _config_path(runtime_dir: Path) -> Path:
    return decisions_dir(runtime_dir) / "config.json"


def parse_override(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in ("1", "true", "on", "yes"):
        return True
    if lowered in ("0", "false", "off", "no"):
        return False
    return None


def is_enabled(runtime_dir: Path, override: Optional[str] = None) -> bool:
    """Capture is off unless the user turned it on; ENABLE_ENV wins."""
    forced = parse_override(override)
    if forced is not None:
        return forced
    return read_json(_config_path(runtime_dir)).get("enabled") is True


def set_enabled(runtime_dir: Path, enabled: bool) -> None:
    path = _config_path(runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json_compact(path, {"enabled": enabled})


def ledger_path(runtime_dir: Path, session_id: str) -> Path:
    return decisions_dir(runtime_dir) / "sessions" / f"{safe_session(session_id)}.json"


def read_ledger(runtime_dir: Path, session_id: str) -> list[dict[str, Any]]:
    entries = read_json(ledger_path(runtime_dir, session_id)).get("decisions")
    return [e for e in entries if isinstance(e, dict) and e.get("id")] if isinstance(entries, list) else []


def record_in_ledger(runtime_dir: Path, session_id: str, decision: dict[str, Any]) -> list[dict[str, Any]]:
    """Upsert the decision by id and mark what it supersedes."""
    entries = [e for e in read_ledger(runtime_dir, session_id) if e["id"] != decision["id"]]
    replaced = {link["to"] for link in decision["links"] if link["relation"] == "supersedes"}
    for entry in entries:
        if entry["id"] in replaced:
            entry["superseded_by"] = decision["id"]
    entries.append({
        "id": decision["id"], "choice": decision["choice"],
        "question": decision.get("question"), "at": int(time.time()),
    })
    path = ledger_path(runtime_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json_compact(path, {"decisions": entries})
    return entries


def render_ledger(entries: Sequence[dict[str, Any]], limit: int = 15) -> str:
    """One line per decision, current ones first, newest last."""
    if not entries:
        return "(none yet)"
    current = [e for e in entries if not e.get("superseded_by")]
    superseded = [e for e in entries if e.get("superseded_by")]
    shown = (current + superseded)[-limit:] if len(entries) > limit else current + superseded
    lines = []
    for e in shown:
        suffix = f" [superseded by {e['superseded_by']}]" if e.get("superseded_by") else ""
        lines.append(f"- {e['id']}: {e.get('choice', '')}{suffix}")
    return "\n".join(lines)


# --- OTLP attributes --------------------------------------------------------


def decision_attributes(
    *,
    session_id: str,
    decision: dict[str, Any],
    code_clusters: Sequence[str] = (),
    cluster_scheme: Optional[str] = None,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    head_sha: Optional[str] = None,
    pr_number: Optional[int] = None,
    pr_url: Optional[str] = None,
) -> dict[str, Any]:
    """Log-record attributes for one decision. List-valued fields are
    JSON-encoded (OTLP tags reach the processor as flat strings); empty
    values are dropped by otlp.log_record."""

    def encoded(values: Sequence[Any]) -> Optional[str]:
        return json.dumps(list(values), separators=(",", ":")) if values else None

    return {
        "session_id": session_id,
        "cardinal.decision.id": decision["id"],
        "cardinal.decision.question": decision.get("question"),
        "cardinal.decision.choice": decision["choice"],
        "cardinal.decision.rationale": decision.get("rationale"),
        "cardinal.decision.decided_by": decision.get("decided_by"),
        "cardinal.decision.alternatives": encoded(decision.get("alternatives") or ()),
        "cardinal.decision.links": encoded(decision.get("links") or ()),
        "cardinal.decision.anchors": encoded(decision.get("anchors") or ()),
        "cardinal.decision.code_clusters": encoded(code_clusters),
        "cardinal.decision.cluster_scheme": cluster_scheme if code_clusters else None,
        "cardinal.repo": repo,
        "cardinal.branch": branch,
        "cardinal.head_sha": head_sha,
        "cardinal.pr_number": pr_number,
        "cardinal.pr_url": pr_url,
    }
