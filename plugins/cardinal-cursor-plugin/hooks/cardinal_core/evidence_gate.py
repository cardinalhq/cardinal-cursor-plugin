"""The sensitivity gate for generic evidence capture.

Every tool call's raw input is checked here before anything about the call is
kept. The gate is tool-neutral: it looks at every string (and every key) in
`tool_input`, whatever the tool is, and never at the tool's name, except
through the user's own `deny.tools` rule. A verdict of Withheld means the call
touched something sensitive: the capture pipeline then keeps a stub (tool,
source, status, the rule that fired) with NO arguments and NO result, so the
agent can say why it cannot cite the call instead of the call vanishing.

Rule families (stable ids; a user may allow-list an id):
  path.*   a sensitive file or directory named anywhere in any string
           (.env, credentials, private keys, ~/.ssh, ~/.aws, kubeconfig,
           .netrc, keychains, agent credential files and transcripts,
           /proc/*/environ, ...). Relative paths resolve against the call's
           cwd; $HOME / ~user expand; a path that exists is also followed
           through symlinks; globs and {a,b} braces are expanded; matching
           is case- and Unicode-folded, and credential stores match under
           any home directory.
  cmd.*    a secret-dumping command, matched on parsed argv (so wrappers
           such as sudo, env, timeout, `bash -c '...'`, eval, xargs, $(...),
           backticks, here-strings, `kubectl exec -- ...`, `docker exec`,
           `ssh host ...` are seen through): printenv, ps e, aws sts /
           secrets manager, kubectl get secret, gh auth token, gcloud auth
           print-*, vault/op read, security find-*-password, echo $SECRET,
           an inline program that prints the environment, a base64-decoded
           command, `config get <secret key>`, ...
           Every unquoted argv word also goes through the path rules.
  url.* / header.* / arg.* / env.*
           a URL or request carrying a credential (userinfo, a credential
           query parameter, an Authorization/Cookie/API-key header, curl -u,
           --password, PGPASSWORD=...).
  user.*   the user's own rules (~/.cardinal/evidence-rules.json; a
           project's <cwd>/.cardinal/evidence-rules.json may only deny).

A verdict never carries the matched text: `hint` is the rule's pattern.
The gate fails closed: an input it cannot finish walking (too deep, too many
leaves) is withheld (gate.bounds), never captured unchecked.

Standard library only, Python 3.9.
"""

from __future__ import annotations

import fnmatch
import glob as _globmod
import json
import os
import posixpath
import re
import shlex
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import unquote_plus

MAX_DEPTH = 32
MAX_LEAVES = 5000
MAX_STRING = 64 << 10
# Strings holding prose or file content (a newline, or longer than this):
# only explicit paths (with a "/" or a leading "~") count there, not a bare
# word such as ".env" a README mentions.
PROSE_CHARS = 1024
MAX_REALPATH = 256
MAX_LINKS = 256
MAX_SEGMENTS = 256
MAX_RULES_BYTES = 64 << 10
RULES_FILE = "evidence-rules.json"

REASON_PATH = "sensitive_path"
REASON_COMMAND = "secret_command"
REASON_URL = "credential_url"
REASON_HEADER = "credential_header"
REASON_USER = "user_rule"
REASON_UNREADABLE = "unreadable"

REASON_TEXT = {
    REASON_PATH: "sensitive path",
    REASON_COMMAND: "secret command",
    REASON_URL: "credential in a URL",
    REASON_HEADER: "credential in a request",
    REASON_USER: "your evidence rule",
    REASON_UNREADABLE: "could not be checked",
}


@dataclass(frozen=True)
class Withheld:
    reason: str
    rule: str
    hint: str

    def as_dict(self) -> dict:
        return {"reason": self.reason, "rule": self.rule, "hint": self.hint}


# ---------------------------------------------------------------------------
# Rules files
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rules:
    deny_tools: tuple = ()
    deny_paths: tuple = ()
    deny_commands: tuple = ()      # compiled regexps
    allow_rules: frozenset = frozenset()
    allow_paths: tuple = ()
    warnings: tuple = ()


def _read_rules_file(path: Path) -> tuple:
    """(dict or None, warning or None). A missing file is (None, None)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, f"{path}: {type(e).__name__}"
    if not stat.S_ISREG(st.st_mode):
        return None, f"{path}: not a regular file"
    if st.st_size > MAX_RULES_BYTES:
        return None, f"{path}: larger than {MAX_RULES_BYTES} bytes"
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return None, f"{path}: not owned by you"
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read(MAX_RULES_BYTES + 1).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None, f"{path}: not valid JSON"
    if not isinstance(data, dict):
        return None, f"{path}: not a JSON object"
    return data, None


def _strs(v) -> list:
    return [s for s in v if isinstance(s, str) and s][:256] if isinstance(v, list) else []


def load_rules(home: Optional[Path], cwd: Optional[str] = None) -> Rules:
    """The user's rules (~/.cardinal/evidence-rules.json: deny and allow) plus
    a project's (<cwd>/.cardinal/evidence-rules.json: deny only, so a cloned
    repo cannot switch secret protection off). A malformed file is ignored
    with a warning; the built-in rules always apply."""
    deny_tools, deny_paths, deny_cmds, allow_rules, allow_paths, warnings = [], [], [], set(), [], []
    sources = []
    if home is not None:
        sources.append((Path(home) / ".cardinal" / RULES_FILE, True))
    if cwd:
        try:
            p = Path(cwd) / ".cardinal" / RULES_FILE
            if home is None or p.resolve() != (Path(home) / ".cardinal" / RULES_FILE).resolve():
                sources.append((p, False))
        except (OSError, RuntimeError):
            pass
    for path, may_allow in sources:
        data, warn = _read_rules_file(path)
        if warn:
            warnings.append(warn)
        if not data:
            continue
        deny = data.get("deny") if isinstance(data.get("deny"), dict) else {}
        deny_tools += _strs(deny.get("tools"))
        deny_paths += _strs(deny.get("paths"))
        for rx in _strs(deny.get("commands")):
            try:
                deny_cmds.append(re.compile(rx[:1024]))
            except re.error:
                warnings.append(f"{path}: bad deny.commands pattern")
        if may_allow:
            allow = data.get("allow") if isinstance(data.get("allow"), dict) else {}
            allow_rules.update(_strs(allow.get("rules")))
            allow_paths += _strs(allow.get("paths"))
    return Rules(tuple(deny_tools), tuple(deny_paths), tuple(deny_cmds), frozenset(allow_rules), tuple(allow_paths),
                 tuple(warnings))


# ---------------------------------------------------------------------------
# Path rules
# ---------------------------------------------------------------------------

_SOURCE_EXT = frozenset((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rs", ".java", ".kt", ".rb",
                         ".php", ".cs", ".c", ".h", ".cc", ".cpp", ".swift", ".md", ".mdx", ".rst", ".html", ".css",
                         ".scss", ".test", ".snap", ".proto", ".sql"))
_KEY_EXT = frozenset((".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk"))
_SSH_KEY_PREFIX = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
_SECRETS_BASE = frozenset("secret" + e for e in ("", ".json", ".yaml", ".yml", ".toml", ".env", ".txt")) | \
    frozenset("secrets" + e for e in ("", ".json", ".yaml", ".yml", ".toml", ".env", ".txt"))
_CLIENT_CRED_BASE = frozenset((".netrc", "_netrc", ".git-credentials", ".pgpass", ".my.cnf", ".npmrc", ".pypirc",
                               ".vault-token", "cardinal-secrets.json"))
_HOME_TREES = (
    (".ssh", "path.ssh", "~/.ssh/**"),
    (".aws", "path.aws", "~/.aws/**"),
    (".gnupg", "path.gnupg", "~/.gnupg/**"),
    (".azure", "path.azure", "~/.azure/**"),
    (".password-store", "path.password-store", "~/.password-store/**"),
    (".config/gcloud", "path.gcloud", "~/.config/gcloud/**"),
    ("Library/Keychains", "path.keychain", "~/Library/Keychains/**"),
    (".cardinal", "path.agent-secrets", "~/.cardinal/**"),
)
_HOME_FILES = (
    (".kube/config", "path.kube", "~/.kube/config"),
    (".docker/config.json", "path.docker", "~/.docker/config.json"),
    (".config/gh/hosts.yml", "path.gh", "~/.config/gh/hosts.yml"),
    (".claude.json", "path.agent-secrets", "~/.claude.json"),
    (".claude/.credentials.json", "path.agent-secrets", "~/.claude/.credentials.json"),
    (".codex/auth.json", "path.agent-secrets", "~/.codex/auth.json"),
    (".gemini/oauth_creds.json", "path.agent-secrets", "~/.gemini/oauth_creds.json"),
)


def basename_rule(base: str) -> Optional[tuple]:
    """(rule id, hint) for a sensitive file name, else None."""
    b = base.lower()
    if not b or b in (".", ".."):
        return None
    if b == ".env" or b.startswith(".env.") or b == ".envrc" or (b.endswith(".env") and len(b) > 4):
        return "path.dotenv", ".env*"
    if b in _CLIENT_CRED_BASE:
        return "path.netrc", base if b != "cardinal-secrets.json" else "cardinal-secrets.json"
    ext = posixpath.splitext(b)[1]
    if b.startswith(_SSH_KEY_PREFIX) and not b.endswith(".pub"):
        return "path.private-key", "id_*"
    if ext in _KEY_EXT:
        return "path.private-key", "*" + ext
    if b.endswith((".keychain", ".keychain-db")):
        return "path.keychain", "*.keychain"
    if "credential" in b and ext not in _SOURCE_EXT:
        return "path.credentials", "*credential*"
    if (b in _SECRETS_BASE or b.endswith((".secret", ".secrets")) or ext == ".tfvars"
            or b.startswith("terraform.tfstate")):
        return "path.secrets", "secrets / *.tfvars / terraform.tfstate"
    if "kubeconfig" in b:
        return "path.kube", "*kubeconfig*"
    return None


def _fold(s: str) -> str:
    """Compare paths the way a case-insensitive, normalization-insensitive
    file system (macOS APFS, Windows) would: ~/.KUBE/Config opens
    ~/.kube/config there. Folding only ever widens a match."""
    try:
        return unicodedata.normalize("NFKC", s).casefold()
    except (TypeError, ValueError):
        return s.lower()


# Credential stores under ANY home directory (this user's, another user's,
# /root, or an unexpanded $HOME): matched as a path-component sequence
# anywhere in the path, case-folded.
_ANY_HOME_TREES = tuple((f"/{_fold(rel)}/", rule, hint) for rel, rule, hint in _HOME_TREES if rel != ".cardinal") + (
    ("/.kube/", "path.kube", "~/.kube/**"),
)
_ANY_HOME_FILES = tuple(("/" + _fold(rel), rule, hint) for rel, rule, hint in _HOME_FILES)
# Directory names whose files are secrets (k8s/secrets/db.yaml), unless the
# file is source code or docs (src/secrets/index.ts).
_SECRET_DIRS = frozenset(("secret", "secrets", ".secrets"))
# Agent transcripts and their spilled tool outputs replay every earlier
# tool result verbatim, including ones this gate withheld.
_TRANSCRIPT_TREES = (
    (".claude/projects", "~/.claude/projects/**"),
    (".codex/sessions", "~/.codex/sessions/**"),
    (".gemini/tmp", "~/.gemini/tmp/**"),
)
_TRANSCRIPT_FILES = (".claude/history.jsonl", ".codex/history.jsonl")


def full_path_rule(p: str, home: Optional[str]) -> Optional[tuple]:
    """(rule id, hint) for a sensitive absolute path, else None. Matched
    case-folded, and credential stores under any home directory count."""
    f = _fold(p)
    base = posixpath.basename(f)
    hit = basename_rule(base)
    if hit:
        return hit
    if f.startswith("/proc/") and f.endswith("/environ"):
        return "path.proc-environ", "/proc/*/environ"
    if f == "/etc/shadow" or f.startswith("/etc/sudoers"):
        return "path.proc-environ", "/etc/shadow, /etc/sudoers*"
    if f == "/var/run/secrets" or f.startswith("/var/run/secrets/") or f.startswith("/run/secrets/"):
        return "path.kube", "/var/run/secrets/**"
    fs = f + "/"
    for suffix, rule, hint in _ANY_HOME_FILES:
        if f.endswith(suffix):
            return rule, hint
    if "/.claude/settings" in f and f.endswith(".json") and "/" not in f[f.rindex("/.claude/settings") + 9:]:
        return "path.agent-secrets", "~/.claude/settings*.json"
    for seg, rule, hint in _ANY_HOME_TREES:
        if seg in fs:
            if rule == "path.ssh" and (base.endswith(".pub") or base in ("known_hosts", "known_hosts.old")):
                continue
            return rule, hint
    parts = f.split("/")
    if any(d in _SECRET_DIRS for d in parts[:-1]) and posixpath.splitext(base)[1] not in _SOURCE_EXT:
        return "path.secrets", "secrets/**"
    if home and home != "/":
        h = _fold(home).rstrip("/")
        if f == h + "/.cardinal" or f.startswith(h + "/.cardinal/"):
            return "path.agent-secrets", "~/.cardinal/**"
        for rel, hint in _TRANSCRIPT_TREES:
            if f.startswith(h + "/" + rel + "/"):
                return "path.agent-transcripts", hint
        for rel in _TRANSCRIPT_FILES:
            if f == h + "/" + rel:
                return "path.agent-transcripts", "~/" + rel
    return None


_TOKEN_SPLIT = re.compile(r"[\s\"'`=,;|&<>()\[\]{}]+")


def _path_like(tok: str, prose: bool, cwd: Optional[str], budget: "_Budget") -> bool:
    """A token that names a path: it has a "/" or starts with "~"; outside
    prose, also a dotfile (".env"), a sensitive name with an extension
    ("credentials.json", "server.key") or an ssh key name. A bare word
    ("secrets", "credential") counts only when a file of that name exists in
    cwd, so `kubectl get secrets` or `git credential fill` is left to the
    command rules and `git commit -m "fix secret handling"` is not a path.
    A bare word that is a symlink in cwd counts too (notes.txt -> .env)."""
    if "/" in tok or tok.startswith("~"):
        return True
    if prose:
        return False
    if tok.startswith("."):
        return True
    hit = basename_rule(tok)
    weak = hit is not None and hit[0] == "path.dotenv" and not tok.lower().startswith(".env")
    if hit is None or weak:
        # A bare `name.env` is as often code (process.env, import.meta.env)
        # as a file, so it counts only when that file exists; any other
        # bare word counts when it is a symlink (the target is checked).
        if not cwd or budget.links >= MAX_LINKS or len(tok) > 255 or not _BARE_NAME.match(tok):
            return False
        budget.links += 1
        try:
            q = os.path.join(cwd, tok)
            return os.path.lexists(q) if weak else os.path.islink(q)
        except (OSError, ValueError):
            return False
    if "." in tok or tok.lower().startswith(_SSH_KEY_PREFIX):
        return True
    if not cwd or budget.realpaths >= MAX_REALPATH:
        return False
    budget.realpaths += 1
    try:
        return os.path.lexists(os.path.join(cwd, tok))
    except (OSError, ValueError):
        return False


_BARE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+@-]*$")
_HOME_VAR = re.compile(r"\$\{HOME\}|\$HOME\b|\$\{USERPROFILE\}|\$USERPROFILE\b")


def _norm(tok: str, cwd: Optional[str], home: Optional[str]) -> Optional[str]:
    t = tok.rstrip(":.")
    if not t or "://" in t:
        return None
    if t.startswith("~"):
        if t == "~" or t.startswith("~/"):
            if not home:
                return None
            t = home.rstrip("/") + t[1:]
        else:
            # ~user/...: that user's home. Where it cannot be looked up, a
            # stand-in root keeps the any-home rules (/.aws/, /.kube/config)
            # working.
            try:
                e = os.path.expanduser(t)
            except (KeyError, ValueError, OSError):
                e = t
            t = e if e.startswith("/") else "/" + t
    if not t.startswith("/"):
        if not cwd:
            return posixpath.normpath(t)
        t = posixpath.join(cwd, t)
    return posixpath.normpath(t)


class _Budget:
    def __init__(self):
        self.realpaths = 0
        self.links = 0
        self.globs = 0


def _check_path(p: str, home: Optional[str], rules: Rules, budget: _Budget) -> Optional[Withheld]:
    if any(_glob(p, g) for g in rules.allow_paths):
        return None
    hit = full_path_rule(p, home)
    if hit is None and budget.realpaths < MAX_REALPATH and p.startswith("/"):
        try:
            if os.path.lexists(p):
                budget.realpaths += 1
                rp = os.path.realpath(p)
                if rp != p:
                    hit = full_path_rule(rp, home)
                    if hit is None and home:
                        try:
                            rh = os.path.realpath(home)
                            if rh != home:
                                hit = full_path_rule(rp, rh)
                        except OSError:
                            pass
        except (OSError, ValueError):
            hit = None
    if hit and hit[0] not in rules.allow_rules:
        return Withheld(REASON_PATH, hit[0], hit[1])
    return None


def _glob(p: str, pattern: str) -> bool:
    """fnmatch where "**/" also matches no directory at all."""
    if fnmatch.fnmatchcase(p, pattern):
        return True
    if "**/" in pattern and fnmatch.fnmatchcase(p, pattern.replace("**/", "")):
        return True
    return not pattern.startswith(("/", "*", "~")) and fnmatch.fnmatchcase(posixpath.basename(p), pattern)


# Shell glob patterns: a pattern such as `.env*`, `.e?v` or `~/.kube/conf*`
# names a sensitive file without spelling it. Checked two ways: against
# these well-known names (when the pattern's file part starts with a literal,
# so `dist/*` or `*.json` is not assumed to hit), and against the files it
# matches on disk (bounded).
_GLOB_CHARS = frozenset("*?[")
_GLOB_CANDIDATES = (
    ".env", ".env.local", ".env.production", ".envrc", "credentials", "credentials.json", "secrets.json",
    "secrets.yaml", "secrets.yml", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "server.key", "key.pem",
    "cert.p12", ".netrc", ".git-credentials", ".pgpass", ".my.cnf", ".npmrc", ".pypirc", ".vault-token",
    "config", "config.json", "hosts.yml", "auth.json", "oauth_creds.json", ".credentials.json",
    "terraform.tfstate", "prod.tfvars", "kubeconfig", "login.keychain-db", "environ", "shadow",
)
MAX_GLOBS = 8
MAX_GLOB_MATCHES = 64


def _glob_paths(p: str, budget: _Budget) -> Iterator[str]:
    """Concrete paths a glob pattern may name (candidates, then real files)."""
    d, b = posixpath.split(p)
    if b and b[0] not in _GLOB_CHARS:
        for cand in _GLOB_CANDIDATES:
            if fnmatch.fnmatch(cand, b):
                yield posixpath.join(d, cand)
    if budget.globs >= MAX_GLOBS or not p.startswith("/"):
        return
    budget.globs += 1
    try:
        for i, m in enumerate(_globmod.iglob(p)):
            if i >= MAX_GLOB_MATCHES:
                break
            yield m
    except (OSError, ValueError, RecursionError):
        return


_BRACE = re.compile(r"\{([^{}\s]*,[^{}\s]*)\}")
MAX_BRACE_WORDS = 64


def _brace_words(s: str) -> list:
    """Brace expansions of every word with {a,b} in it (bounded):
    ~/.kube/{config,x} -> ~/.kube/config, ~/.kube/x."""
    out = []
    if "{" not in s or "," not in s:
        return out
    for word in s.split():
        if "{" not in word or len(out) >= MAX_BRACE_WORDS:
            continue
        cur = [word.strip("\"'")]
        for _ in range(4):
            nxt = []
            changed = False
            for w in cur:
                m = _BRACE.search(w)
                if not m:
                    nxt.append(w)
                    continue
                changed = True
                for alt in m.group(1).split(","):
                    nxt.append(w[:m.start()] + alt + w[m.end():])
            cur = nxt[:MAX_BRACE_WORDS]
            if not changed:
                break
        out.extend(w for w in cur if w != word)
    return out[:MAX_BRACE_WORDS]


def paths_in(s: str, cwd: Optional[str], home: Optional[str], budget: Optional["_Budget"] = None) -> Iterator[str]:
    prose = "\n" in s.strip() or len(s) > PROSE_CHARS
    budget = budget or _Budget()
    if home and "$" in s:
        s = _HOME_VAR.sub(lambda _m: home.rstrip("/"), s)
    toks = _TOKEN_SPLIT.split(s)
    if not prose:
        toks = toks + _brace_words(s)
    for tok in toks:
        if tok and _path_like(tok, prose, cwd, budget):
            p = _norm(tok, cwd, home)
            if p:
                yield p


def check_paths(s: str, cwd: Optional[str], home: Optional[str], rules: Rules, budget: _Budget) -> Optional[Withheld]:
    for p in paths_in(s, cwd, home, budget):
        concrete = [p]
        if any(c in _GLOB_CHARS for c in p):
            concrete.extend(_glob_paths(p, budget))
        for q in concrete:
            for g in rules.deny_paths:
                if _glob(q, g) or (home and _glob(q, g.replace("~", home.rstrip("/"), 1))):
                    return Withheld(REASON_USER, "user.deny-path", g)
            w = _check_path(q, home, rules, budget)
            if w:
                return w
    return None


# ---------------------------------------------------------------------------
# Command rules
# ---------------------------------------------------------------------------

_SHELLS = frozenset(("bash", "sh", "zsh", "dash", "ksh", "fish", "ash"))
_WRAPPERS_NOARG = frozenset(("command", "exec", "nohup", "time", "builtin", "noglob", "stdbuf", "caffeinate",
                             "unbuffer", "chronic"))
_SUB_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`|<\(([^()]*)\)")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PASSWORD_ENV = frozenset(("PGPASSWORD", "MYSQL_PWD", "REDISCLI_AUTH", "SSHPASS"))


def _split_ops(script: str) -> list:
    """Simple commands of a script: split on ; && || | & and newlines outside
    quotes."""
    out, cur, q, i, n = [], [], None, 0, len(script)
    while i < n:
        c = script[i]
        if q:
            cur.append(c)
            if c == "\\" and q == '"' and i + 1 < n:
                cur.append(script[i + 1])
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c in "'\"":
            q = c
            cur.append(c)
        elif c == "\\" and i + 1 < n:
            cur.append(c)
            cur.append(script[i + 1])
            i += 2
            continue
        elif c in ";|&\n":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return [s.strip() for s in out if s.strip()]


def _argv(seg: str) -> list:
    try:
        return shlex.split(seg, comments=True, posix=True)
    except ValueError:
        return seg.split()


def _strip_wrappers(argv: list) -> tuple:
    """(argv with wrappers and leading VAR=val assignments dropped,
    [assignments], env_dump). env_dump: `env` with no command operand."""
    assigns = []
    i = 0
    n = len(argv)
    while i < n:
        a = argv[i]
        base = posixpath.basename(a)
        if _ASSIGN_RE.match(a):
            assigns.append(a)
            i += 1
            continue
        if base == "sudo" or base == "doas":
            i += 1
            while i < n and argv[i].startswith("-"):
                if argv[i] in ("-u", "-g", "-C", "-h", "-p", "-U", "-r", "-t", "-D"):
                    i += 1
                i += 1
            continue
        if base in _WRAPPERS_NOARG:
            i += 1
            while i < n and argv[i].startswith("-"):
                i += 1
            continue
        if base in ("timeout", "gtimeout"):
            i += 1
            while i < n and argv[i].startswith("-"):
                if argv[i] in ("-s", "-k", "--signal", "--kill-after"):
                    i += 1
                i += 1
            i += 1  # the duration
            continue
        if base == "nice":
            i += 1
            while i < n and argv[i].startswith("-"):
                if argv[i] == "-n":
                    i += 1
                i += 1
            continue
        if base == "env":
            i += 1
            while i < n and (argv[i].startswith("-") or _ASSIGN_RE.match(argv[i])):
                if argv[i] in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
                    if argv[i] in ("-S", "--split-string") and i + 1 < n:
                        return _strip_wrappers(_argv(argv[i + 1]) + argv[i + 2:])
                    i += 1
                elif _ASSIGN_RE.match(argv[i]):
                    assigns.append(argv[i])
                i += 1
            if i >= n:
                return [], assigns, True
            continue
        break
    return argv[i:], assigns, False


def _positional(args: list) -> list:
    return [a for a in args if not a.startswith("-")]


def _follows(pos: list, *words) -> bool:
    """True when `words` appear in pos, in order and adjacent."""
    k = len(words)
    for i in range(len(pos) - k + 1):
        if all(pos[i + j] == words[j] for j in range(k)):
            return True
    return False


def _after(pos: list, word: str) -> Optional[str]:
    try:
        i = pos.index(word)
    except ValueError:
        return None
    return pos[i + 1] if i + 1 < len(pos) else None


_SECRET_NAME_TAIL = re.compile(
    r"(?:^|_)(?:KEY|KEYS|TOKEN|TOKENS|SECRET|SECRETS|PAT|PASS|PASSWD|PASSWORD|PWD|CREDS?|CREDENTIALS?|APIKEY|DSN)$")
_SECRET_NAME_EXTRA = frozenset(("private_token", "private-token", "oauth_token", "session_token", "jwt", "sig",
                                "client-key-data", "client_key_data", "private_key", "private-key", "signature"))


def secretish_name(name: str) -> bool:
    """A variable, parameter or field name that holds a secret, judged more
    widely than the gateway's credential-key rule (which a leaf of any
    receipt already gets): an UPPER_SNAKE env name ending in _KEY / _TOKEN /
    _SECRET / _PAT / _PASS... (OPENAI_KEY, STRIPE_SECRET; not sort_key or
    page_token), a key-file field (tls.key, client-key-data), or
    private_token / jwt / sig."""
    from . import evidence

    if not isinstance(name, str) or not name or len(name) > 256:
        return False
    if name in ("PWD", "OLDPWD"):  # the shell's working directory
        return False
    if evidence.is_credential_key(name):
        return True
    if _SECRET_NAME_TAIL.search(name.upper()) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return name.isupper()
    lower = name.lower()
    return lower in _SECRET_NAME_EXTRA or lower.endswith((".key", ".pem", ".p12", ".pfx", ".jks"))


_VAR_REF = re.compile(r"\$\{?!?([A-Za-z_][A-Za-z0-9_]*)")


def _secret_var_ref(word: str) -> bool:
    """$SECRET / ${SECRET} in a word (an echo of a secret's value)."""
    return "$" in word and any(secretish_name(m.group(1)) for m in _VAR_REF.finditer(word))


# An interpreter's inline program that prints the whole environment, or a
# secret-named variable from it (node -e, python -c, ruby -e, perl -e, ...).
_INTERPRETERS = re.compile(r"(?:^|[\s;&|(`/])(?:node|nodejs|python[0-9.]*|ruby|perl|deno|bun|php|irb|pwsh)\b")
_ENV_DUMP_CODE = re.compile(
    r"process\.env\b(?!\s*(?:\.|\[|\?\.))"
    r"|os\.environ\b(?!\s*(?:\[|\.get\b|\.setdefault\b|\.pop\b))"
    r"|%ENV\b|\bENV\.(?:to_h|to_a|each|inspect|keys|values|map|select|sort|dup)\b"
    r"|\b(?:p|pp|puts|print)\s*\(?\s*ENV\b(?!\s*\[|\.fetch)"
    r"|Deno\.env\.toObject\b|\bgetenv\s*\(\s*\)|\$env:|Get-ChildItem\s+env:")
_ENV_READ_CODE = re.compile(
    r"(?:process\.env(?:\.|\[\s*[\"'`])|os\.environ(?:\.get)?\s*[\[(]\s*[\"']|os\.getenv\s*\(\s*[\"']"
    r"|ENV(?:\.fetch\s*\(|\[)\s*[\"']|\$ENV\{\s*[\"']?|Deno\.env\.get\s*\(\s*[\"']|getenv\s*\(\s*[\"'])"
    r"([A-Za-z_][A-Za-z0-9_]*)")


def env_code_rule(s: str) -> Optional[tuple]:
    if not _INTERPRETERS.search(s):
        return None
    if _ENV_DUMP_CODE.search(s):
        return REASON_COMMAND, "cmd.env-dump", "env / printenv / export -p"
    for m in _ENV_READ_CODE.finditer(s):
        if secretish_name(m.group(1)):
            return REASON_COMMAND, "cmd.secret-var", "echo $SECRET_VAR"
    return None


# A decoded string run as a command or used as an argument: `$(echo … |
# base64 -d)`, `… | base64 -d | sh`. What it names cannot be checked.
_DECODE = r"(?:base64\s+(?:[^|;&]*\s)?(?:-d|-D|--decode)\b|xxd\s+(?:[^|;&]*\s)?-r\b|openssl\s+(?:enc|base64)\b[^|;&]*\s-d\b)"
_DECODED_EXEC = re.compile(r"\$\([^()]*" + _DECODE + r"|`[^`]*" + _DECODE + r"|" + _DECODE +
                           r"[^;&]*\|\s*(?:sudo\s+)?(?:(?:ba|z|da|k)?sh|eval|source|xargs)\b")


def command_rule(argv: list, assigns: list, env_dump: bool) -> Optional[tuple]:
    """(reason, rule id, hint) for one simple command, else None."""
    for a in assigns:
        name, _, val = a.partition("=")
        if name in _PASSWORD_ENV and val:
            return REASON_HEADER, "env.password", name + "=..."
    if env_dump:
        return REASON_COMMAND, "cmd.env-dump", "env / printenv / export -p"
    if not argv:
        return None
    cmd = posixpath.basename(argv[0]).lower()
    args = argv[1:]
    pos = _positional(args)
    lower = [a.lower() for a in pos]
    if cmd == "printenv":
        return REASON_COMMAND, "cmd.env-dump", "env / printenv / export -p"
    if cmd == "ps" and any((not a.startswith("-") and "e" in a) or (a.startswith("-") and "E" in a) for a in args):
        # BSD-style `ps eww` / `ps auxe`, macOS `ps -E`: every process's environment.
        return REASON_COMMAND, "cmd.env-dump", "ps e / ps -E"
    if cmd in ("echo", "printf", "print") and any(_secret_var_ref(a) for a in args):
        return REASON_COMMAND, "cmd.secret-var", "echo $SECRET_VAR"
    if cmd in ("declare", "typeset") and "-p" in args and any(secretish_name(a) for a in pos):
        return REASON_COMMAND, "cmd.secret-var", "echo $SECRET_VAR"
    if cmd in ("export", "declare", "typeset", "set") and all(a in ("-p", "-x", "-px", "-xp") for a in args):
        if cmd in ("export", "set") or "-x" in args or "-p" in args or "-px" in args or "-xp" in args:
            return REASON_COMMAND, "cmd.env-dump", "env / printenv / export -p"
    if cmd == "aws":
        if _after(lower, "sts") in ("get-session-token", "assume-role", "assume-role-with-saml",
                                    "assume-role-with-web-identity", "get-federation-token"):
            return REASON_COMMAND, "cmd.aws", "aws sts get-session-token / assume-role"
        op = _after(lower, "configure")
        if op == "export-credentials" or (op == "get" and any(("key" in a or "secret" in a or "token" in a)
                                                              for a in lower[lower.index("get") + 1:])):
            return REASON_COMMAND, "cmd.aws", "aws configure export-credentials"
        if _after(lower, "secretsmanager") in ("get-secret-value", "batch-get-secret-value"):
            return REASON_COMMAND, "cmd.aws", "aws secretsmanager get-secret-value"
        if (_after(lower, "ssm") in ("get-parameter", "get-parameters", "get-parameters-by-path")
                and "--with-decryption" in args):
            return REASON_COMMAND, "cmd.aws", "aws ssm get-parameter --with-decryption"
        if _after(lower, "ecr") == "get-login-password" or _after(lower, "ecr-public") == "get-login-password":
            return REASON_COMMAND, "cmd.aws", "aws ecr get-login-password"
        if _follows(lower, "iam", "create-access-key"):
            return REASON_COMMAND, "cmd.aws", "aws iam create-access-key"
    if cmd in ("kubectl", "oc", "kubecolor", "k", "kc"):
        if pos and any(v in lower for v in ("get", "describe", "edit")):
            for a in lower:
                for r in a.split(","):
                    if r in ("secret", "secrets") or r.startswith(("secret/", "secrets/")):
                        return REASON_COMMAND, "cmd.kube-secret", "kubectl get secret"
        if _follows(lower, "config", "view") and any(a.startswith(("--raw", "--flatten")) for a in args):
            return REASON_COMMAND, "cmd.kube-secret", "kubectl config view --raw"
        if _follows(lower, "create", "token"):
            return REASON_COMMAND, "cmd.kube-secret", "kubectl create token"
    if cmd == "gh" and _follows(lower, "auth", "token"):
        return REASON_COMMAND, "cmd.gh-token", "gh auth token"
    if cmd == "gh" and _follows(lower, "auth", "status") and ("-t" in args or "--show-token" in args):
        return REASON_COMMAND, "cmd.gh-token", "gh auth status --show-token"
    if cmd == "gcloud":
        if "auth" in lower and any(a.startswith("print-") and a.endswith("token") for a in lower):
            return REASON_COMMAND, "cmd.gcloud", "gcloud auth print-*-token"
        if _follows(lower, "secrets", "versions", "access"):
            return REASON_COMMAND, "cmd.gcloud", "gcloud secrets versions access"
    if cmd == "az":
        if _follows(lower, "account", "get-access-token"):
            return REASON_COMMAND, "cmd.az", "az account get-access-token"
        if _after(lower, "secret") in ("show", "download") and "keyvault" in lower:
            return REASON_COMMAND, "cmd.az", "az keyvault secret show"
    if cmd == "vault":
        if (lower[:1] == ["read"] or _follows(lower, "kv", "get") or _follows(lower, "token", "lookup")
                or _follows(lower, "print", "token")):
            return REASON_COMMAND, "cmd.vault", "vault read / kv get"
    if cmd == "op" and (lower[:1] == ["read"] or _follows(lower, "item", "get")):
        return REASON_COMMAND, "cmd.vault", "op read / op item get"
    if cmd == "security" and lower[:1] and lower[0] in ("find-generic-password", "find-internet-password",
                                                       "dump-keychain"):
        return REASON_COMMAND, "cmd.keychain", "security find-*-password"
    if cmd == "git":
        if _follows(lower, "credential", "fill"):
            return REASON_COMMAND, "cmd.git-credential", "git credential fill"
        if "config" in lower and any(a.startswith("--get") or a in ("-l", "--list") for a in args):
            if any(("credential" in a or "token" in a or "password" in a) for a in lower):
                return REASON_COMMAND, "cmd.git-credential", "git config --get *credential*"
    if "config" in lower and ("get" in lower or any(a.startswith("--get") for a in args)):
        # npm config get //registry/:_authToken, git config --get x.token,
        # pip/poetry/yarn config get ...password
        for a in pos[pos.index("config") + 1:] if "config" in pos else []:
            leaf = re.split(r"[:/.]", a)[-1]
            al = a.lower()
            if secretish_name(leaf) or any(w in al for w in ("token", "password", "secret", "_auth", "credential")):
                return REASON_COMMAND, "cmd.config-secret", "<tool> config get <secret key>"
    if cmd == "heroku" and lower[:1] == ["auth:token"]:
        return REASON_COMMAND, "cmd.misc-token", "heroku auth:token"
    if cmd in ("npm", "pnpm", "yarn") and lower[:1] == ["token"]:
        return REASON_COMMAND, "cmd.misc-token", "npm token"
    if cmd == "doctl" and lower[:1] == ["auth"]:
        return REASON_COMMAND, "cmd.misc-token", "doctl auth"
    if cmd in ("sops", "gpg", "gpg2", "age") and any(a in ("-d", "--decrypt") for a in args):
        return REASON_COMMAND, "cmd.misc-token", cmd + " --decrypt"
    if cmd in ("curl", "wget", "http", "https", "xh"):
        for j, a in enumerate(args):
            if a in ("-u", "--user", "--proxy-user", "-U") and j + 1 < len(args) and ":" in args[j + 1]:
                return REASON_HEADER, "arg.userpass", cmd + " -u user:pass"
            if a.startswith(("--user=", "-u")) and ":" in a and not a.startswith("--user-agent"):
                return REASON_HEADER, "arg.userpass", cmd + " -u user:pass"
            if a.startswith(("--password", "--http-password", "--proxy-password")):
                return REASON_HEADER, "arg.password", "--password"
    if cmd in ("mysql", "mysqldump", "mariadb", "mysqladmin"):
        if any(a.startswith("-p") and len(a) > 2 for a in args) or any(a.startswith("--password=") for a in args):
            return REASON_HEADER, "arg.password", "mysql -p<password>"
    if any(a.startswith("--password=") and len(a) > len("--password=") for a in args):
        return REASON_HEADER, "arg.password", "--password"
    return None


def scripts_in(s: str) -> list:
    """The script and every $(...), `...` and <(...) inside it, innermost
    first, each with the substitution replaced by a placeholder word."""
    out = []
    cur = s
    for _ in range(64):
        found = False

        def repl(m):
            nonlocal found
            found = True
            out.append(m.group(1) if m.group(1) is not None else (m.group(2) if m.group(2) is not None
                                                                  else m.group(3)))
            return " _sub_ "

        cur = _SUB_RE.sub(repl, cur)
        if not found:
            break
    out.append(cur)
    return out


_SSH_VALUE_FLAGS = frozenset(("-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o",
                              "-p", "-Q", "-R", "-S", "-W", "-w", "-B"))
_EXEC_VALUE_FLAGS = frozenset(("-e", "--env", "-u", "--user", "-w", "--workdir", "--env-file", "--name", "-v",
                               "--volume", "-p", "--publish", "--network", "--entrypoint", "--platform", "-l",
                               "--label", "--mount", "-h", "--hostname", "--pull", "--detach-keys"))


# Commands that read (or copy, send, decode) the files they are given.
_FILE_READERS = frozenset((
    "cat", "bat", "less", "more", "head", "tail", "source", ".", "cp", "mv", "rsync", "scp", "tar", "zip", "gzip",
    "base64", "xxd", "od", "hexdump", "strings", "grep", "egrep", "fgrep", "rg", "ag", "awk", "sed", "cut", "sort",
    "uniq", "nl", "tac", "tee", "diff", "cmp", "jq", "yq", "vim", "vi", "nano", "view", "open", "pbcopy", "wc",
    "dd", "openssl", "gpg", "xargs", "find", "curl", "wget", "python", "python3", "node", "ruby", "perl", "bash",
    "sh", "zsh", "column", "fold", "fmt", "paste", "join", "iconv", "file", "stat", "install", "ln", "readlink",
))


def _inner_commands(argv: list) -> list:
    """Commands another command runs: `sh -c X`, `su -c X`, `eval X`,
    `xargs X`, `sh <<< X`, `kubectl exec … -- X`, `docker exec C X`,
    `ssh host X`, and anything after a `--`."""
    cmd = posixpath.basename(argv[0]).lower()
    out = []
    if cmd in _SHELLS or cmd in ("su", "runuser", "script"):
        for j, a in enumerate(argv[1:], 1):
            if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
                if j + 1 < len(argv):
                    out.append(argv[j + 1])
                break
            if a == "--command" and j + 1 < len(argv):
                out.append(argv[j + 1])
                break
    if cmd in _SHELLS or cmd in ("eval", "source", "."):
        for j, a in enumerate(argv[1:], 1):
            if a == "<<<" and j + 1 < len(argv):
                out.append(argv[j + 1])
            elif a.startswith("<<<") and len(a) > 3:
                out.append(a[3:])
    if cmd == "eval":
        out.append(" ".join(argv[1:]))
    elif cmd == "xargs":
        k = 1
        while k < len(argv) and argv[k].startswith("-"):
            if argv[k] in ("-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a"):
                k += 1
            k += 1
        if k < len(argv):
            out.append(shlex.join(argv[k:]))
    elif cmd in ("docker", "podman", "nerdctl") and len(argv) > 1:
        rest = argv[1:]
        if rest and rest[0] == "container":
            rest = rest[1:]
        if rest and rest[0] in ("exec", "run"):
            k = 1
            while k < len(rest) and rest[k].startswith("-"):
                if rest[k] in _EXEC_VALUE_FLAGS:
                    k += 1
                k += 1
            if k + 1 < len(rest):
                out.append(shlex.join(rest[k + 1:]))
    elif cmd in ("ssh", "autossh"):
        k = 1
        while k < len(argv) and argv[k].startswith("-"):
            if argv[k] in _SSH_VALUE_FLAGS:
                k += 1
            k += 1
        if k + 1 < len(argv):
            out.append(" ".join(argv[k + 1:]))
    if "--" in argv[1:]:
        i = argv.index("--", 1)
        if i + 1 < len(argv):
            out.append(shlex.join(argv[i + 1:]))
    return out


def check_command(s: str, rules: Rules, depth: int = 0, count: Optional[list] = None, *, cwd: Optional[str] = None,
                  home: Optional[str] = None, budget: Optional[_Budget] = None) -> Optional[Withheld]:
    """Every simple command in a string, through wrappers, `sh -c`, eval,
    xargs, here-strings, remote/container exec and substitutions. Each
    word of each command also goes through the path rules after shell
    unquoting (so .e''nv or "$HOME"/.kube/config is seen as written)."""
    if count is None:
        count = [0]
    if budget is None:
        budget = _Budget()
    if depth > 4:
        return None
    prose = "\n" in s.strip() or len(s) > PROSE_CHARS
    if depth == 0:
        hit = env_code_rule(s)
        if hit and hit[1] not in rules.allow_rules:
            return Withheld(*hit)
        if ("base64" in s or "xxd" in s or "openssl" in s) and _DECODED_EXEC.search(s) \
                and "cmd.decoded-exec" not in rules.allow_rules:
            return Withheld(REASON_COMMAND, "cmd.decoded-exec", "$(… | base64 -d)")
    for script in scripts_in(s):
        for seg in _split_ops(script):
            count[0] += 1
            if count[0] > MAX_SEGMENTS:
                return None
            for rx in rules.deny_commands:
                if rx.search(seg[:4096]):
                    return Withheld(REASON_USER, "user.deny-command", rx.pattern[:120])
            words = _argv(seg)
            argv, assigns, env_dump = _strip_wrappers(words)
            hit = command_rule(argv, assigns, env_dump)
            if hit and hit[1] not in rules.allow_rules:
                return Withheld(*hit)
            # Unquoted words as paths. In a multi-line string (a script, or
            # prose / file content) only a command that reads files counts,
            # so "Copy the example to .env" in a README is not an access.
            if not prose or (argv and posixpath.basename(argv[0]).lower() in _FILE_READERS):
                for word in words[:MAX_SEGMENTS]:
                    if word and len(word) <= 4096 and "\n" not in word:
                        w = check_paths(word, cwd, home, rules, budget)
                        if w:
                            return w
            if not argv:
                continue
            for inner in _inner_commands(argv):
                if inner:
                    w = check_command(inner, rules, depth + 1, count, cwd=cwd, home=home, budget=budget)
                    if w:
                        return w
    return None


# ---------------------------------------------------------------------------
# URL and header rules
# ---------------------------------------------------------------------------

_CRED_PARAMS = ("token", "access_token", "id_token", "refresh_token", "api_key", "apikey", "api-key", "key", "sig",
                "signature", "password", "passwd", "secret", "client_secret", "code", "x-amz-signature",
                "x-amz-credential", "x-amz-security-token", "x-goog-signature", "x-goog-credential", "auth")
# The query, and the fragment (OAuth's #access_token=...), of every URL.
_URL_QUERY = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s?#\"'<>]*(?:\?([^\s#\"'<>]*))?(?:#([^\s\"'<>]*))?")
_HEADER_LINE = re.compile(
    r"(?im)(?:^|[\s\"'{,;])(authorization|proxy-authorization|cookie|set-cookie|x-api-key|api-key|"
    r"x-[a-z0-9-]*-(?:token|key|secret|auth))\s*:\s*[^\s\"',}]")
# In prose or file content (see PROSE_CHARS) only an explicit request header
# counts: `-H "Authorization: ..."` / `--header`, not code that builds one.
_HEADER_FLAG = re.compile(
    r"(?i)(?:-H|--header)[ \t=]*[\"']?(authorization|proxy-authorization|cookie|x-api-key|api-key|"
    r"x-[a-z0-9-]*-(?:token|key|secret|auth))\s*:\s*[^\s\"']")
_CRED_HEADER_NAMES = frozenset(("authorization", "proxy-authorization", "cookie", "x-api-key", "api-key",
                                "x-auth-token", "x-access-token"))


def check_url_header(s: str) -> Optional[Withheld]:
    from . import evidence

    prose = "\n" in s.strip() or len(s) > PROSE_CHARS

    if "://" in s and evidence._url_userinfo_spans(s):
        return Withheld(REASON_URL, "url.userinfo", "scheme://user:pass@")
    if ("?" in s or "#" in s) and "://" in s:
        for m in _URL_QUERY.finditer(s):
            for group in (m.group(1), m.group(2)):
                if not group or "=" not in group:
                    continue
                for part in group.split("&")[:256]:
                    name, eq, val = part.partition("=")
                    try:
                        name = unquote_plus(name)
                    except (ValueError, TypeError):
                        pass
                    if eq and val and (name.lower() in _CRED_PARAMS or secretish_name(name)):
                        return Withheld(REASON_URL, "url.credential-param", "?" + name.lower()[:40] + "=")
    if ":" in s and (_HEADER_FLAG.search(s) if prose else _HEADER_LINE.search(s)):
        return Withheld(REASON_HEADER, "header.credential", "Authorization / Cookie / API-key header")
    return None


def _header_name_credential(k: str) -> bool:
    from . import evidence

    kl = k.lower()
    if kl in _CRED_HEADER_NAMES:
        return True
    if kl.startswith("x-") and kl.endswith(("-token", "-key", "-secret", "-auth")):
        return True
    return evidence.is_credential_key(k)


def _has_value(v: Any) -> bool:
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, dict)):
        return bool(v)
    return v is not None


def check_headers_obj(k: str, v: Any) -> Optional[Withheld]:
    """A `headers` member (object, or [{name, value}] list) carrying a
    credential header with a value."""
    from . import evidence

    if not evidence.is_headers_key(k):
        return None
    if isinstance(v, dict):
        for hk, hv in list(v.items())[:256]:
            if isinstance(hk, str) and _header_name_credential(hk) and _has_value(hv):
                return Withheld(REASON_HEADER, "header.credential", "Authorization / Cookie / API-key header")
    elif isinstance(v, list):
        for e in v[:256]:
            if (isinstance(e, dict) and isinstance(e.get("name"), str) and _header_name_credential(e["name"])
                    and _has_value(e.get("value"))):
                return Withheld(REASON_HEADER, "header.credential", "Authorization / Cookie / API-key header")
    return None


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------

class _Overflow:
    """Set when the walk hit a bound (depth, leaf count) before seeing every
    string: the gate then cannot vouch for the input."""

    def __init__(self):
        self.hit = False


def _leaves(v: Any, overflow: Optional[_Overflow] = None) -> Iterator[tuple]:
    """(kind, key, value) for every key and string leaf, depth-first,
    bounded. kind: "key" (a dict member: key, value) or "str"."""
    stack = [(v, 0)]
    visited = 0
    while stack:
        cur, depth = stack.pop()
        visited += 1
        if visited > MAX_LEAVES:
            if overflow is not None:
                overflow.hit = True
            return
        if isinstance(cur, str):
            yield "str", None, cur
        elif isinstance(cur, dict):
            if depth >= MAX_DEPTH:
                if cur and overflow is not None:
                    overflow.hit = True
                continue
            items = list(cur.items())
            for k, val in items:
                if isinstance(k, str):
                    yield "key", k, val
            for k, val in reversed(items):
                if isinstance(k, str):
                    stack.append((k, depth + 1))
                stack.append((val, depth + 1))
        elif isinstance(cur, (list, tuple)):
            if depth >= MAX_DEPTH:
                if cur and overflow is not None:
                    overflow.hit = True
                continue
            for e in reversed(cur):
                stack.append((e, depth + 1))


_HEREDOC = re.compile(r"<<-?[ \t]*([\"']?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n")


def strip_heredocs(s: str) -> str:
    """A script without its here-document bodies (data, not commands): what
    runs around a large heredoc is still checked."""
    out, i = [], 0
    for _ in range(256):
        m = _HEREDOC.search(s, i)
        if not m:
            break
        out.append(s[i:m.end()])
        tag = m.group(2)
        end = re.compile(r"^[ \t]*" + re.escape(tag) + r"[ \t]*$", re.MULTILINE).search(s, m.end())
        if not end:
            i = len(s)
            break
        i = end.end()
    out.append(s[i:])
    return "".join(out)


BOUNDS_WITHHELD = Withheld(REASON_UNREADABLE, "gate.bounds", "input too large or deep to check")


def check(tool_name: Any, tool_input: Any, *, cwd: Optional[str] = None, home: Optional[str] = None,
          rules: Optional[Rules] = None) -> Optional[Withheld]:
    """None (capture the call) or the Withheld verdict for it. Looks at
    every key and string in tool_input; never at the tool's name except
    through the user's deny.tools rule. Never raises for any JSON input.
    Fails closed: an input the walk could not finish (too deep, too many
    leaves) is withheld, not captured unchecked."""
    rules = rules or Rules()
    name = tool_name if isinstance(tool_name, str) else ""
    for g in rules.deny_tools:
        if fnmatch.fnmatchcase(name, g):
            return Withheld(REASON_USER, "user.deny-tool", g)
    budget = _Budget()
    overflow = _Overflow()
    try:
        for kind, key, val in _leaves(tool_input, overflow):
            if kind == "key":
                w = check_headers_obj(key, val)
                if w and w.rule not in rules.allow_rules:
                    return w
                if (isinstance(val, str) and _header_name_credential(key) and key.lower() in _CRED_HEADER_NAMES
                        and val.strip() and "header.credential" not in rules.allow_rules):
                    return Withheld(REASON_HEADER, "header.credential", "Authorization / Cookie / API-key header")
                continue
            s = val
            if not s:
                continue
            if len(s) > MAX_STRING:
                # Content (a Write body), or a script with a large heredoc:
                # the commands around the heredoc bodies still run.
                if "<<" not in s:
                    continue
                s = strip_heredocs(s)
                if len(s) > MAX_STRING:
                    continue
            w = check_url_header(s)
            if w and w.rule not in rules.allow_rules:
                return w
            w = check_command(s, rules, cwd=cwd, home=home, budget=budget)
            if w:
                return w
            w = check_paths(s, cwd, home, rules, budget)
            if w:
                return w
    except RecursionError:
        return BOUNDS_WITHHELD
    if overflow.hit:
        return BOUNDS_WITHHELD
    return None


def describe(w: Withheld) -> str:
    """"sensitive path (.env*)": the context-line text for a verdict."""
    return f"{REASON_TEXT.get(w.reason, w.reason)} ({w.hint})"
