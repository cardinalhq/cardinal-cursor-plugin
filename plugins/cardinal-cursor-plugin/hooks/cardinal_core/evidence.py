"""Local evidence spool: captured tool results, kept on this machine.

A storyboard cites evidence. Cardinal's own gateway mints a *witnessed*
receipt for every read-only call it serves; any other tool call (a built-in
tool such as a shell command or a file read, another MCP server, any tool at
all) is invisible to it. A client hook records such a result here instead,
through the generic pipeline in cardinal_core.evidence_capture: one JSON
file per call, under

    <root>/<session_id>/ev_<12 hex>.json      (root: ~/.cardinal/evidence)

Nothing here touches the network. Only a result the author later cites in a
storyboard is uploaded (as a *captured* receipt), by a separate, explicit
step (`cardinal-evidence promote`). Everything else ages out after
RETENTION_S.

What this module owns:
  - normalize(): Claude Code's PostToolUse tool_response for an MCP tool (a
    string, {content, structuredContent}, a list of content blocks, or the
    "Output has been saved to <file>" spill notice) -> the same
    {structured, text, other_blocks} body a gateway receipt stores as
    model_result.
  - scrub(): the credential scrub conductor's gateway applies to a receipt
    (packages/mcp-gateway/storyboard/receipts/receipts.go scrub and
    errors.go redactValueShapes), ported so a secret a tool echoes never
    reaches disk. Keep the two in step: tests/testdata/scrub_vectors.json
    holds vectors both must satisfy.
  - scrub_and_cap(): results over MAX_RESULT_BYTES become conductor's
    truncatedBody ({truncated, original_bytes, prefix}). A result too large
    to scrub whole in a hook's budget is scrubbed structurally only as far
    as the prefix it keeps (bounded_scrub), with the same rules.
  - write_entry(): atomic write, directories 0700, files 0600.
  - capture(): the whole per-call step a client hook runs (opt-out check,
    build, write, gc), shared by the Claude, Cursor and Gemini adapters;
    client_string() spells the entry's client ("cursor/1.7.29").
  - gc(): opportunistic, time-bounded removal of entries past retention.
  - store_token()/find_tokens(): a storyboard's evidence token, kept per
    session in token.json for `cardinal-evidence promote`.
  - record_promoted()/promoted_receipt(): the receipt each promoted entry
    got, kept per session in promoted.json, so a repeat promote reuses it.
  - list_entries()/session_dirs()/set_capture_disabled(): what the
    `cardinal-evidence list` / `off` / `on` / `status` commands read and write.
  - spill_path()/read_spill(): Claude Code's spill-file follower, shared
    with the storyboard preview hook.

No module-level path constants (spec §omnigent constraints): every function
that touches disk takes the spool root (default_root(home)) or an allowed
spill root as an argument.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = "cardinal.evidence.v1"
TIER = "captured"

SCHEMA_V2 = "cardinal.evidence.v2"
SCHEMAS = (SCHEMA, SCHEMA_V2)

RETENTION_S = 14 * 24 * 3600
# gc() runs at most once per GC_INTERVAL_S (stamp file) and for at most
# GC_BUDGET_S, so a hook that calls it on every tool call stays cheap.
GC_INTERVAL_S = 10 * 60
GC_BUDGET_S = 0.25
# Size bounds (every tool call is captured, so these are load-bearing): the
# whole spool stays under MAX_SPOOL_BYTES (CARDINAL_EVIDENCE_MAX_MB), the
# least recently active sessions' entries evicted first (oldest first within
# a session; per-session totals cached in GC_INDEX so this holds for a spool
# of any size), and one session keeps at most
# MAX_ENTRIES_PER_SESSION entries. Token files and markers are never evicted
# for size.
MAX_SPOOL_BYTES = 256 << 20
MAX_ENTRIES_PER_SESSION = 10000
MAX_MB_ENV = "CARDINAL_EVIDENCE_MAX_MB"
HINTED = ".hinted"
# A temp file this old belongs to a write that died.
STALE_TMP_S = 3600

# Mirrors conductor's receipts.MaxModelResultBytes.
MAX_RESULT_BYTES = 256 << 10
MAX_ARGS_BYTES = 64 << 10
# A result whose serialization is larger than this is not scrubbed whole
# (walking every leaf is too slow for a 2 s hook). It is still scrubbed
# structurally, with the same rules, but only as far as the prefix that is
# kept (bounded_scrub): decoding a JSON text runs at C speed, the walk stops
# once MAX_RESULT_BYTES of scrubbed output exist.
MAX_STRUCTURAL_SCRUB_BYTES = 2 << 20
MAX_SPILL_BYTES = 64 * 1024 * 1024
# How much of a spill file is read. A spill up to this size is read whole,
# so its JSON decodes and gets the structural scrub; a larger one is cut
# here, cannot decode, and falls back to redact_json_text.
MAX_SPILL_READ_BYTES = 16 << 20

DISABLED_FLAG = "disabled"
GC_STAMP = ".last-gc"

EVIDENCE_ID_RE = re.compile(r"^ev_[0-9a-f]{12}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
NO_SESSION = "no-session"


# ---------------------------------------------------------------------------
# Paths and opt-out
# ---------------------------------------------------------------------------

def default_root(home: Path) -> Path:
    """The spool root under a user's home directory: ~/.cardinal/evidence."""
    return Path(home) / ".cardinal" / "evidence"


def capture_disabled(root: Path, env: Optional[dict] = None) -> bool:
    """True when the user opted out: CARDINAL_EVIDENCE_CAPTURE=0 (or
    false/off/no), or the flag file <root>/disabled exists."""
    env = os.environ if env is None else env
    v = str(env.get("CARDINAL_EVIDENCE_CAPTURE", "")).strip().lower()
    if v in ("0", "false", "off", "no"):
        return True
    try:
        return (Path(root) / DISABLED_FLAG).exists()
    except OSError:
        return True


def session_dir_name(session_id: Any) -> str:
    if isinstance(session_id, str) and SESSION_ID_RE.match(session_id):
        return session_id
    return NO_SESSION


def split_mcp_tool(tool_name: Any) -> Optional[tuple]:
    """"mcp__<server>__<tool>" -> (server, tool); None for anything else.
    The server is the run up to the next "__" (a tool name may itself hold
    "__": mcp__plugin_cardinal_cardinal__storyboard__preview)."""
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return None
    rest = tool_name[len("mcp__"):]
    server, sep, tool = rest.partition("__")
    if not sep or not server or not tool:
        return None
    return server, tool


# ---------------------------------------------------------------------------
# Claude Code's spill notice
# ---------------------------------------------------------------------------

# Claude Code's notice for a result too large to keep inline, e.g.
# "Error: result (71,204 characters) exceeds maximum allowed tokens. Output
# has been saved to /Users/me/.claude/projects/<p>/<s>/tool-results/x.txt.\n..."
# The path runs to the end of its line (it may contain spaces, e.g. a HOME of
# "/Users/John Doe"), minus the sentence's closing period.
SPILL_RE = re.compile(r"Output has been saved to (.+?)\.?[ \t]*$", re.MULTILINE)


def spill_candidates(text: str) -> list:
    """Paths the spill notice may name: its whole line, then (for a notice that
    goes on after the path on the same line) each prefix ending before ". "."""
    if not isinstance(text, str):
        return []
    m = SPILL_RE.search(text)
    if not m:
        return []
    line = m.group(1).strip()
    out = [line]
    for i in range(len(line)):
        if line.startswith(". ", i) and line[:i] not in out:
            out.append(line[:i])
    return out[:8]


def spill_path(text: str, allowed_root: Path, max_bytes: int = MAX_SPILL_BYTES) -> Optional[Path]:
    """The file `text` (Claude Code's spill notice) names, resolved, when it
    is a regular file under `allowed_root` (for Claude Code,
    ~/.claude/projects) of at most max_bytes. A link that resolves outside
    the root, a relative or `..` path that escapes it, or a larger file is
    refused (None)."""
    for cand in spill_candidates(text):
        try:
            root = Path(allowed_root).resolve()
            path = Path(cand).expanduser()
            if not path.is_absolute():
                continue
            path = path.resolve()
            path.relative_to(root)
            if not path.is_file() or path.stat().st_size > max_bytes:
                continue
            return path
        except (OSError, ValueError, RuntimeError):
            continue
    return None


def read_spill(text: str, allowed_root: Path, max_bytes: int = MAX_SPILL_BYTES,
               read_limit: Optional[int] = None) -> Optional[str]:
    """The saved result, when `text` is Claude Code's spill notice and the file
    passes spill_path(). With read_limit, only the file's leading read_limit
    bytes are read (cut on a UTF-8 boundary)."""
    path = spill_path(text, allowed_root, max_bytes)
    if path is None:
        return None
    try:
        if read_limit is None:
            return path.read_text(encoding="utf-8")
        with open(path, "rb") as f:
            return f.read(read_limit).decode("utf-8", errors="ignore")
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Credential scrub — a port of conductor's receipt scrub.
#   packages/mcp-gateway/storyboard/receipts/receipts.go  (scrub, isCredentialKey, ...)
#   packages/mcp-gateway/storyboard/receipts/errors.go    (redactValueShapes, RedactErrorText, ...)
# Change both together; tests/testdata/scrub_vectors.json pins the shared
# behaviour.
# ---------------------------------------------------------------------------

REDACTED = "[redacted]"
MAX_NESTED_JSON = 4

_CREDENTIAL_WORD = {
    "secret", "password", "passwd", "passphrase", "pwd", "pass",
    "cookie", "authorization", "authentication", "auth", "credential",
    "apikey", "bearer", "dsn", "jwt",
    "apitoken", "authtoken", "accesstoken", "connstr", "databaseurl", "secretkeybase",
}
_DIGEST_WORD = {"hash", "hashes", "digest", "salt"}
_DIGEST_OF = {"password", "passwd", "pwd", "passphrase", "token", "secret"}
_KEY_QUALIFIER = ("api", "access", "private", "secret", "session", "signing", "encryption", "ssh", "routing",
                  "integration", "master", "client", "app", "subscription", "account", "storage", "shared", "hmac",
                  "license")
_TOKEN_QUALIFIER = {"api", "access", "auth", "bearer", "refresh", "id", "session", "service"}
_NON_SECRET_TOKEN = {
    "next", "page", "continuation", "continue", "cursor", "pagination",
    "sync", "resume", "prev", "previous",
    "max", "input", "output", "prompt", "completion", "total",
    "eos", "bos", "pad", "unk", "sep", "cls", "mask", "special",
}
_ENCODING_SUFFIX = {"pem", "b64", "base64", "hex", "raw", "value"}
_CREDENTIAL_SUFFIX = ("connection string", "connection str", "conn string", "conn str", "database url", "db url",
                      "secret key base")
_REFERENCE_WORD = {"name", "ref", "type", "kind", "path", "optional", "mode"}

_ACRONYM_RUN = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Go's \s (and so RE2's) is ASCII [\t\n\f\r ]; spelled out to match exactly.
_WS = r"\t\n\f\r "
# Go's RE2 is linear; Python's re backtracks. Two of conductor's patterns
# (errors.go urlUserinfo and keySep) can start a match anywhere inside a long
# unbroken run and are quadratic on one under re, so they are matched here by
# scanning from their fixed anchor ("://", or the separator) instead:
# _url_userinfo_spans and _key_sep_matches. Their results equal the Go
# patterns' (FindAll, leftmost-first); tests/test_evidence.py fuzzes both
# against the literal Go patterns (GO_URL_USERINFO, GO_KEY_SEP).
GO_URL_USERINFO = r"([A-Za-z][A-Za-z0-9+.\-]*://)[^\s/:@]+:[^\s/@]+@"
GO_KEY_SEP = r"""(?:\\[nrt]|\\*["'])?([A-Za-z_][A-Za-z0-9_.\-]*)\\*["']?[ \t]*(?:=>|[:=])[ \t]*"""
_USERINFO_TAIL = re.compile(r"[^" + _WS + r"/:@]+:[^" + _WS + r"/@]+@")
_SEP = re.compile(r"=>|[:=]")
_SCHEME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+.-")
_KEY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
_KEY_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic|token|apikey)([" + _WS + r"]+)([A-Za-z0-9._~+/=\-]{8,})", re.ASCII)
# Anchored by .match(s, pos) (a "^" would only match at the string's start).
_SCHEME_WORD = re.compile(r"(?i)(?:bearer|basic|token|apikey|digest|negotiate)[ \t]+")
_TOKEN_SHAPES = [re.compile(p, re.ASCII) for p in (
    r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]*",                                   # JWT
    r"\bgh[pousr]_[A-Za-z0-9]{20,}",                                                                   # GitHub
    r"\bxox[abprs]-[A-Za-z0-9\-]{10,}",                                                                # Slack
    r"\bAKIA[0-9A-Z]{16}\b",                                                                           # AWS access key id
    r"\bsk-[A-Za-z0-9_\-]{16,}",                                                                       # sk- API keys
    r"\$2[abxy]?\$\d{1,2}\$[./A-Za-z0-9]{20,}",                                                        # bcrypt
    r"\$argon2(?:id|i|d)\$(?:v=\d+\$)?m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/.=\-]+(?:\$[A-Za-z0-9+/.=\-]+)?",  # argon2
    r"\$(?:1|5|6|md5)\$(?:rounds=\d+\$)?[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{20,}",                      # md5/sha-crypt
    r"\$g?y\$[./A-Za-z0-9]+\$[./A-Za-z0-9]+\$[./A-Za-z0-9]{20,}",                                      # yescrypt
    r"\$7\$[./A-Za-z0-9]+\$[./A-Za-z0-9]{20,}",                                                        # crypt scrypt
    r"\$scrypt\$[A-Za-z0-9+/.=,$\-]{20,}",                                                             # PHC scrypt
    r"\bpbkdf2_sha(?:1|256|512)\$\d+\$[^" + _WS + r"$]+\$[A-Za-z0-9+/=]{20,}",                         # Django
)]
_SK_HOST_NAME = re.compile(r"^sk(?:-[a-z0-9]+){2,}$")
# A PEM private key block, header to footer; a block cut before its footer
# (a capped prefix) loses the base64 run after its header. Matched by
# _redact_pem_private_keys, not one regexp with a lazy "anything up to a
# footer" arm: that rescans to the end for every footerless header and is
# quadratic on a run of them (conductor errors.go redactPEMPrivateKeys).
# The single-pattern form, kept for the parity fuzz in tests/test_evidence.py.
GO_PEM_PRIVATE_KEY = (r"-----BEGIN[A-Z0-9 ]{0,40} PRIVATE KEY(?: BLOCK)?-----(?:(?s:.)*?"
                      r"-----END[A-Z0-9 ]{0,40} PRIVATE KEY(?: BLOCK)?-----|[A-Za-z0-9+/=\t\n\f\r \\:,.\-]*)")
_PEM_PRIVATE_BEGIN = re.compile(r"-----BEGIN[A-Z0-9 ]{0,40} PRIVATE KEY(?: BLOCK)?-----", re.ASCII)
_PEM_PRIVATE_END = re.compile(r"-----END[A-Z0-9 ]{0,40} PRIVATE KEY(?: BLOCK)?-----", re.ASCII)
_PEM_BODY_RUN = re.compile(r"[A-Za-z0-9+/=" + _WS + r"\\:,.\-]*", re.ASCII)
_VALUE_STOP = " \t\r\n\"'&,;)}]<>"
_VALUE_ESCAPE_STOP = "\"'nrt"


# key_words looks at a key's last words only; a pathological key is cut to
# its tail first (the acronym split backtracks on a long capital run).
_MAX_KEY_CHARS = 256


def key_words(k: str) -> list:
    k = k[-_MAX_KEY_CHARS:]
    k = _ACRONYM_RUN.sub(r"\1 \2", k)
    k = _CAMEL_BOUNDARY.sub(r"\1 \2", k)
    return _NON_ALNUM.sub(" ", k.lower()).split()


def is_headers_key(k: str) -> bool:
    w = key_words(k)
    return bool(w) and w[-1] == "headers"


def is_reference_key(k: str) -> bool:
    w = key_words(k)
    return bool(w) and w[-1] in _REFERENCE_WORD


def is_credential_key(k: str) -> bool:
    words = key_words(k)
    if len(words) >= 2 and words[-1] in _ENCODING_SUFFIX:
        words = words[:-1]
    if not words:
        return False
    raw = words[-1]
    prev = words[-2] if len(words) >= 2 else ""
    joined = " " + " ".join(words)
    for suf in _CREDENTIAL_SUFFIX:
        if joined.endswith(" " + suf):
            return True
    last = raw[:-1] if raw.endswith("s") else raw
    prev_single = prev[:-1] if prev.endswith("s") else prev
    if (raw in _DIGEST_WORD or last in _DIGEST_WORD) and prev_single in _DIGEST_OF:
        return True
    if last == "token":
        if raw == "token":
            return prev not in _NON_SECRET_TOKEN
        return prev in _TOKEN_QUALIFIER
    if last == "key":
        return any(prev == q or (len(prev) > len(q) and prev.endswith(q)) for q in _KEY_QUALIFIER)
    return raw in _CREDENTIAL_WORD or last in _CREDENTIAL_WORD


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _nul(s: str) -> str:
    """NULs and lone UTF-16 surrogates become U+FFFD. A tool can emit a
    lone surrogate as a JSON escape ("\\udcff"); Python keeps it in the str,
    where encoding it to UTF-8 raises. Go's decoder makes it U+FFFD, so the
    gateway stores the same thing."""
    s = s.replace("\x00", "\ufffd")
    return _LONE_SURROGATE.sub("\ufffd", s) if not s.isascii() else s


def well_formed(v: Any) -> Any:
    """A copy of a decoded JSON value with every string (keys included)
    passed through _nul, so it encodes to UTF-8. Values nested past the
    recursion limit are returned as they are."""
    try:
        return _well_formed(v)
    except RecursionError:
        return v


def _well_formed(v: Any) -> Any:
    if isinstance(v, str):
        return _nul(v)
    if isinstance(v, list):
        return [_well_formed(e) for e in v]
    if isinstance(v, dict):
        return {(_nul(k) if isinstance(k, str) else k): _well_formed(e) for k, e in v.items()}
    return v


def scrub(v: Any) -> Any:
    """A copy of a decoded JSON value that is safe to persist (conductor
    receipts.go scrub): credential-keyed strings, header values and the
    value of a {name, value} credential pair become REDACTED; every other
    string leaf is scrubbed by value (scrub_string); NULs become U+FFFD."""
    return _scrub_at(v, 0)


def _scrub_at(v: Any, depth: int) -> Any:
    if isinstance(v, str):
        return scrub_string(v, depth)
    if isinstance(v, list):
        return [_scrub_at(e, depth) for e in v]
    if isinstance(v, dict):
        name = v.get("name")
        secret_pair = isinstance(name, str) and "value" in v and is_credential_key(name)
        out = {}
        for k, val in v.items():
            key = _nul(k) if isinstance(k, str) else k
            ks = k if isinstance(k, str) else str(k)
            if is_headers_key(ks):
                out[key] = _redact_all(val)
            elif is_credential_key(ks) or (secret_pair and ks == "value"):
                out[key] = _redact_secret(val, depth)
            else:
                out[key] = _scrub_at(val, depth)
        return out
    return v


def _reject_constant(name: str):
    raise ValueError("not JSON: " + name)


def _decode_json(t: str) -> Any:
    # Go's json.Valid rejects NaN/Infinity; so must this.
    return json.loads(t, parse_constant=_reject_constant)


def encode_json(v: Any) -> str:
    """Go's json.Encoder output with SetEscapeHTML(false): compact, map keys
    sorted, U+2028/U+2029 escaped. (Number literals may differ: Go keeps
    1.50 as written, Python writes 1.5.)"""
    s = json.dumps(v, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
    return s.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def scrub_string(s: str, depth: int = 0) -> str:
    """One string leaf (conductor scrubString): a JSON document is scrubbed
    structurally and re-serialized only when that changed something; any
    other string has URL userinfo, Authorization-style credentials and
    well-known token shapes replaced."""
    s = _nul(s)
    t = s.strip()
    if depth < MAX_NESTED_JSON and t and t[0] in "{[":
        try:
            v = _decode_json(t)
        except (ValueError, RecursionError):
            v = None
        else:
            sv = _scrub_at(v, depth + 1)
            if sv != v:
                try:
                    return encode_json(sv)
                except (ValueError, TypeError, RecursionError):
                    return REDACTED
            return s
    return redact_value_shapes(s)


scrub_text = scrub_string


def _redact_secret(v: Any, depth: int) -> Any:
    if isinstance(v, str):
        return REDACTED
    if isinstance(v, list):
        return [_redact_secret(e, depth) for e in v]
    if isinstance(v, dict):
        out = {}
        for k, val in v.items():
            ks = k if isinstance(k, str) else str(k)
            key = _nul(k) if isinstance(k, str) else k
            out[key] = _scrub_at(val, depth) if is_reference_key(ks) else _redact_secret(val, depth)
        return out
    return v


def _redact_all(v: Any) -> Any:
    if isinstance(v, dict):
        out = {}
        for k, val in v.items():
            key = _nul(k) if isinstance(k, str) else k
            if k == "name" and isinstance(val, str):
                out[key] = _nul(val)
                continue
            out[key] = _redact_all(val)
        return out
    if isinstance(v, list):
        return [_redact_all(e) for e in v]
    if isinstance(v, str):
        return REDACTED
    return v


def _auth_credential(run: str) -> bool:
    return (len(run) >= 16 or any(c in run for c in "0123456789+/=_~")
            or any("A" <= c <= "Z" for c in run[1:]))


def _leaf_auth_credential(run: str) -> bool:
    return any(c in run for c in "0123456789+/") or run.endswith("=")


def _url_userinfo_spans(s: str) -> list:
    """[(start, end)) of the userinfo "user:pass@" of every GO_URL_USERINFO
    match, in order. The scheme is the id run before "://"; it needs a
    letter to start a match (the leftmost letter in that run starts it)."""
    spans = []
    i = s.find("://")
    while i >= 0:
        j = i
        letter = False
        while j > 0 and s[j - 1] in _SCHEME_CHARS:
            j -= 1
            if s[j].isalpha():
                letter = True
        m = _USERINFO_TAIL.match(s, i + 3) if letter else None
        if m:
            spans.append((i + 3, m.end()))
            i = s.find("://", m.end())
        else:
            i = s.find("://", i + 1)
    return spans


def _redact_url_and_scheme(s: str, credential) -> str:
    if "://" in s:
        spans = _url_userinfo_spans(s)
        if spans:
            out, last = [], 0
            for a, b in spans:
                out.append(s[last:a])
                out.append(REDACTED + "@")
                last = b
            out.append(s[last:])
            s = "".join(out)

    def repl(m):
        if not credential(m.group(3)):
            return m.group(0)
        return m.group(1) + m.group(2) + REDACTED

    return _AUTH_SCHEME.sub(repl, s)


def _redact_pem_private_keys(s: str) -> str:
    """Each PEM private key block -> REDACTED: a header through the first
    footer after it or, with no footer after it, the body run after the
    header. Linear in len(s)."""
    if "-----BEGIN" not in s:
        return s
    out = []
    last = 0
    # The first footer at or after the last search start: None = not
    # searched yet, -1 = none left in s.
    end_start, end_stop = None, 0
    while last < len(s):
        m = _PEM_PRIVATE_BEGIN.search(s, last)
        if m is None:
            break
        hs, he = m.start(), m.end()
        if end_start is None or 0 <= end_start < he:
            e = _PEM_PRIVATE_END.search(s, he)
            if e is None:
                end_start = -1
            else:
                end_start, end_stop = e.start(), e.end()
        stop = end_stop if end_start >= 0 else _PEM_BODY_RUN.match(s, he).end()
        out.append(s[last:hs])
        out.append(REDACTED)
        last = stop
    if not out:
        return s
    out.append(s[last:])
    return "".join(out)


def _redact_token_shapes(s: str) -> str:
    # PEM first: a shape matched inside a key's base64 would end a
    # footerless block's body run early and leave the rest of it.
    s = _redact_pem_private_keys(s)
    for rx in _TOKEN_SHAPES:
        s = rx.sub(lambda m: m.group(0) if _SK_HOST_NAME.match(m.group(0)) else REDACTED, s)
    return s


def redact_value_shapes(s: str) -> str:
    """conductor redactValueShapes: URL userinfo, an Authorization-style
    scheme + credential (the leaf rule: the run needs a digit, + or /, or
    trailing =), and self-identifying token / password-hash shapes."""
    return _redact_token_shapes(_redact_url_and_scheme(s, _leaf_auth_credential))


def _value_span(s: str, i: int) -> tuple:
    n = 0
    while i + n < len(s) and s[i + n] == "\\":
        n += 1
    if n > 0 and i + n < len(s) and s[i + n] in "\"'":
        q = s[i + n]
        vs = i + n + 1
        ve = vs
        while ve < len(s):
            c = s[ve]
            if c in "\n\r":
                return vs, ve
            if c == q:
                r = 0
                while ve - r - 1 >= vs and s[ve - r - 1] == "\\":
                    r += 1
                if r <= n:
                    return vs, ve - r
            ve += 1
        return vs, ve
    if i < len(s) and s[i] in "\"'":
        q = s[i]
        vs = i + 1
        ve = vs
        while ve < len(s):
            c = s[ve]
            if c == "\\" and ve + 1 < len(s) and s[ve + 1] != "\n":
                ve += 2
                continue
            if c == q or c in "\n\r":
                return vs, ve
            ve += 1
        return vs, min(ve, len(s))
    vs = ve = i
    while ve < len(s) and s[ve] not in _VALUE_STOP:
        if s[ve] == "\\" and ve + 1 < len(s) and s[ve + 1] in _VALUE_ESCAPE_STOP:
            break
        ve += 1
    return vs, ve


def _key_sep_matches(s: str):
    """(start, key, end) of every GO_KEY_SEP match (FindAll: leftmost-first,
    non-overlapping), found from each separator backwards: [ \\t]*, an
    optional quote and backslashes, then the key: the id run ending there,
    from its first letter or underscore. A key that starts its run may take
    the optional prefix: backslashes and a quote, or a backslash escape
    (\\n, \\r, \\t) whose letter is the run's first byte."""
    prev_end = 0
    for m in _SEP.finditer(s):
        j = m.start()
        k = j
        while k > 0 and s[k - 1] in " \t":
            k -= 1
        if k > 0 and s[k - 1] in "\"'":
            k -= 1
        while k > 0 and s[k - 1] == "\\":
            k -= 1
        run_end = k
        run_start = run_end
        while run_start > 0 and s[run_start - 1] in _KEY_CHARS:
            run_start -= 1
        key_start = run_start
        while key_start < run_end and s[key_start] not in _KEY_START:
            key_start += 1
        if key_start >= run_end:
            continue
        start = key_start
        if key_start == run_start and run_start > 0:
            c = s[run_start - 1]
            if c in "\"'":
                start = run_start - 1
                while start > 0 and s[start - 1] == "\\":
                    start -= 1
            elif (c == "\\" and s[run_start] in "nrt" and run_start + 1 < run_end
                  and s[run_start + 1] in _KEY_START):
                start = run_start - 1
                key_start = run_start + 1
        if start < prev_end:
            continue
        end = m.end()
        while end < len(s) and s[end] in " \t":
            end += 1
        prev_end = end
        yield start, s[key_start:run_end], end


_GO_TEST_STATUS_LEAD = "--- "


def _is_go_test_status(s: str, key_start: int, key_end: int, value_start: int) -> bool:
    """conductor isGoTestStatus: whether the pair whose key is
    s[key_start:key_end] and whose value starts at value_start is a `go test
    -v` status line ("--- PASS: TestX (0.01s)") rather than a credential.
    PASS is a credential key, so without this every passing test's name is
    redacted. One exact shape, no tool and no list; all of these must hold:
    the key is exactly PASS (upper case, the whole key run); the four
    characters before it are "--- " (so it is unquoted); the separator is a
    ':' directly after the key followed by a space or tab; the value does
    not open with a quote or a backslash (an escaped quote). Column-0
    "PASS: <file>" (TAP/automake), "Pass:", "--- PASS=x" and DB_PASS stay
    redacted."""
    if s[key_start:key_end] != "PASS":
        return False
    lead = len(_GO_TEST_STATUS_LEAD)
    if key_start < lead or s[key_start - lead:key_start] != _GO_TEST_STATUS_LEAD:
        return False
    if key_end + 1 >= len(s) or s[key_end] != ":" or s[key_end + 1] not in " \t":
        return False
    return not (value_start < len(s) and s[value_start] in "\"'\\")


def _redact_key_values(s: str, pred=None) -> str:
    """The value of every key=value / key: value pair whose key is a
    credential key (pred: a wider key test, used by the plugin's own
    stricter pass; the gateway parity path always uses is_credential_key).
    A `go test -v` status line keeps its test name (_is_go_test_status),
    whichever key test is in use."""
    pred = pred or is_credential_key
    out = []
    last = 0
    for start, key, end in _key_sep_matches(s):
        if start < last or not pred(key):
            continue
        # start is the key's first character unless a quote or escape
        # prefix precedes it; then s[start:start+4] is not "PASS" and the
        # shape test fails, as the Go one does for a quoted key.
        if _is_go_test_status(s, start, start + len(key), end):
            continue
        vs, ve = _value_span(s, end)
        if vs == end:
            sw = _SCHEME_WORD.match(s, vs)
            if sw:
                vs, ve = _value_span(s, sw.end())
        if ve == vs or s.startswith(REDACTED, vs):
            continue
        out.append(s[last:vs])
        out.append(REDACTED)
        last = ve
    if last == 0:
        return s
    out.append(s[last:])
    return "".join(out)


def redact_plain_text(text: str) -> str:
    """conductor redactPlainText (error text): the value-shape rules with the
    looser scheme test, plus the value of every key=value / key: value /
    "key": "value" pair whose key is a credential key. Used on a prefix that
    could not be scrubbed structurally."""
    s = _nul(text)
    s = _redact_url_and_scheme(s, _auth_credential)
    s = _redact_key_values(s)
    return _redact_token_shapes(s)


def redact_error_text(text: str, depth: int = 0) -> str:
    """conductor RedactErrorText: an error result's text. A JSON document is
    scrubbed structurally and every string leaf redacted as error text; any
    other text gets redact_plain_text (which, unlike a result leaf, also
    redacts credential key=value pairs). Text over the structural scrub
    budget is left to scrub_and_cap, which cuts it and applies the same
    plain-text rules to the kept prefix."""
    if len(text) > MAX_STRUCTURAL_SCRUB_BYTES:
        return text
    t = text.strip()
    if depth < MAX_NESTED_JSON and t and t[0] in "{[":
        try:
            v = _decode_json(t)
        except (ValueError, RecursionError):
            v = None
        else:
            try:
                sv = _redact_error_leaves(scrub(v), depth + 1)
                if sv == v:
                    return text
                return encode_json(sv)
            except (ValueError, TypeError, RecursionError):
                return REDACTED
    return redact_plain_text(text)


def _redact_error_leaves(v: Any, depth: int) -> Any:
    if isinstance(v, str):
        return v if v == REDACTED else redact_error_text(v, depth)
    if isinstance(v, list):
        return [_redact_error_leaves(e, depth) for e in v]
    if isinstance(v, dict):
        return {k: _redact_error_leaves(e, depth) for k, e in v.items()}
    return v


# JSON text that could not be decoded (a document cut short: a spill larger
# than MAX_SPILL_READ_BYTES, or one leaf cut at the prefix end) still holds
# JSON-shaped secrets redact_plain_text does not see: the value of a
# {"name": "<credential key>", "value": ...} pair (a Kubernetes env var) and
# every value under a headers object. redact_json_text applies those rules
# textually, then the plain-text rules.
_HEADERS_OPEN = re.compile(r'"([^"\\\n]{0,256})"[' + _WS + r']*:[' + _WS + r']*\{')
_FLAT_OBJECT = re.compile(r"\{[^{}]*\}")
_NAME_VALUE = re.compile(r'"name"[' + _WS + r']*:[' + _WS + r']*"([^"\\\n]{1,256})"[' + _WS + r']*,['
                         + _WS + r']*"value"[' + _WS + r']*:[' + _WS + r']*')


def _string_end(s: str, i: int) -> int:
    """Index of the quote closing the JSON string whose opening quote is at
    i - 1 (len(s) when it is unterminated)."""
    n = len(s)
    while i < n and s[i] != '"':
        i += 2 if s[i] == "\\" else 1
    return min(i, n)


def _redact_headers_text(s: str) -> str:
    """Every string value (not key, not a "name") inside a JSON object whose
    key is a headers key becomes REDACTED, to the object's end or the text's."""
    out, last, pos, n = [], 0, 0, len(s)
    while True:
        m = _HEADERS_OPEN.search(s, pos)
        if not m:
            break
        if not is_headers_key(m.group(1)):
            pos = m.end()
            continue
        j, depth, prev_key = m.end() - 1, 0, None
        while j < n:
            c = s[j]
            if c == '"':
                end = _string_end(s, j + 1)
                q = end + 1
                while q < n and s[q] in " \t\n\r\f":
                    q += 1
                if q < n and s[q] == ":":
                    prev_key = s[j + 1:end]
                else:
                    if prev_key != "name" and end > j + 1:
                        out.append(s[last:j + 1])
                        out.append(REDACTED)
                        last = end
                    prev_key = None
                j = end + 1
                continue
            if c in "{[":
                depth += 1
                prev_key = None
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            elif c == ",":
                prev_key = None
            j += 1
        pos = j
    if not out:
        return s
    out.append(s[last:])
    return "".join(out)


def _scrub_flat_objects(s: str) -> str:
    """Each innermost {...} that decodes as a JSON object is scrubbed
    structurally (so a {name, value} credential pair loses its value) and
    re-serialized when that changed it."""
    def repl(m):
        seg = m.group(0)
        if '"' not in seg:
            return seg
        try:
            v = _decode_json(seg)
        except (ValueError, RecursionError):
            return seg
        sv = _scrub_at(v, MAX_NESTED_JSON)
        return seg if sv == v else encode_json(sv)
    return _FLAT_OBJECT.sub(repl, s)


def _redact_name_value_text(s: str) -> str:
    """"name": "<credential key>", "value": <v> (an object cut before its
    close, or one _FLAT_OBJECT could not decode): v becomes REDACTED."""
    out, last = [], 0
    for m in _NAME_VALUE.finditer(s):
        if m.start() < last or not is_credential_key(m.group(1)):
            continue
        vs, ve = _value_span(s, m.end())
        if ve == vs or s.startswith(REDACTED, vs):
            continue
        out.append(s[last:vs])
        out.append(REDACTED)
        last = ve
    if not out:
        return s
    out.append(s[last:])
    return "".join(out)


def redact_json_text(text: str) -> str:
    """Text that looks like JSON but does not decode (cut short): the JSON
    rules applied textually (headers objects, {name, value} credential
    pairs, credential-keyed members of any decodable innermost object), then
    redact_plain_text."""
    s = _nul(text)
    s = _redact_headers_text(s)
    s = _scrub_flat_objects(s)
    s = _redact_name_value_text(s)
    return redact_plain_text(s)


# ---------------------------------------------------------------------------
# tool_response -> model_result body
# ---------------------------------------------------------------------------

def _same_json(text: str, want: Any) -> bool:
    t = text.strip()
    if not t or t[0] not in "{[":
        return False
    try:
        return _decode_json(t) == want
    except (ValueError, RecursionError):
        return False


def normalize(tool_response: Any, spill_root: Optional[Path] = None, structure_json: bool = True) -> dict:
    """Claude Code's PostToolUse tool_response for an MCP tool -> an
    UNSCRUBBED {structured?, text?, other_blocks?, spilled?, spilled_bytes?}
    body (spilled_bytes: the spill files' real size; the rest is the shape
    of conductor's model_result). Shapes: a string (the result text, or the
    spill notice, followed when the file resolves under spill_root);
    {content: str | [blocks], structuredContent?}; a list of content blocks;
    any other JSON value (kept as structured).

    Two client renderings are undone:
      - Claude Code hands a hook a tool's structuredContent serialized as
        the result's only text (MCP also asks a tool to mirror it as one
        JSON text block). A result that is exactly one text, with no other
        block, holding a JSON object or array is kept as structured, so a
        storyboard can bind into it like a witnessed receipt (not with
        structure_json=False: an error result's text stays its message).
      - A non-text block (an image, a blob resource) reaches the hook as a
        text placeholder naming the file Claude Code saved it to under
        spill_root ("[Image source: /Users/me/.claude/projects/...png]").
        That path is this machine's, not the tool's result: it becomes
        LOCAL_FILE."""
    body: dict = {}
    texts: list = []
    other = 0

    def add_text(t: str) -> None:
        if spill_root is not None and "Output has been saved to" in t:
            # Only a prefix of a large result is kept anyway (scrub_and_cap),
            # so a 64 MiB spill is not read whole.
            path = spill_path(t, spill_root)
            if path is not None:
                try:
                    size = path.stat().st_size
                    with open(path, "rb") as f:
                        spilled = f.read(MAX_SPILL_READ_BYTES).decode("utf-8", errors="ignore")
                except (OSError, ValueError):
                    spilled = None
                if spilled is not None:
                    body["spilled"] = True
                    body["spilled_bytes"] = body.get("spilled_bytes", 0) + size
                    texts.append(spilled)
                    return
        texts.append(t)

    def add_blocks(blocks: list) -> None:
        nonlocal other
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                add_text(b["text"])
            elif isinstance(b, str):
                add_text(b)
            else:
                other += 1

    if isinstance(tool_response, str):
        add_text(tool_response)
    elif isinstance(tool_response, list):
        add_blocks(tool_response)
    elif isinstance(tool_response, dict) and ("content" in tool_response or "structuredContent" in tool_response):
        sc = tool_response.get("structuredContent")
        if sc is not None:
            body["structured"] = sc
        content = tool_response.get("content")
        if isinstance(content, str):
            add_text(content)
        elif isinstance(content, list):
            add_blocks(content)
    elif tool_response is not None:
        body["structured"] = tool_response
    if "structured" in body:
        texts = [t for t in texts if not _same_json(t, body["structured"])]
    elif structure_json and len(texts) == 1 and not other:
        decoded = _json_container(texts[0])
        if decoded is not None:
            body["structured"] = decoded
            texts = []
    if spill_root is not None and texts:
        texts = [_redact_local_paths(t, spill_root) for t in texts]
    if texts:
        body["text"] = texts
    if other:
        body["other_blocks"] = other
    return body


LOCAL_FILE = "[local file]"


def _json_container(text: str) -> Any:
    """The JSON object or array text holds, whole; None otherwise."""
    t = text.strip()
    if not t or t[0] not in "{[":
        return None
    try:
        v = _decode_json(t)
    except (ValueError, RecursionError):
        return None
    return v if isinstance(v, (dict, list)) else None


def _redact_local_paths(text: str, spill_root: Path) -> str:
    """Paths under spill_root (Claude Code's own ~/.claude/projects files)
    -> LOCAL_FILE. A path runs to whitespace or a closing bracket."""
    roots = {str(spill_root)}
    try:
        roots.add(str(Path(spill_root).resolve()))
    except (OSError, RuntimeError):
        pass
    for root in sorted(roots, key=len, reverse=True):
        if root in text:
            text = re.sub(re.escape(root) + r"(?:/[^\s\]]*)?", LOCAL_FILE, text)
    return text


def _utf8_prefix(s: str, max_bytes: int) -> str:
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max(0, max_bytes)].decode("utf-8", errors="ignore")


# A token-ish run cut at the end of a prefix is dropped (up to
# _MAX_TRAILING_RUN chars): a half token no longer matches its shape and
# would otherwise survive the scrub.
_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~+/=$-")
_MAX_TRAILING_RUN = 4096


def _drop_trailing_run(s: str) -> str:
    i = len(s)
    stop = max(0, i - _MAX_TRAILING_RUN)
    while i > stop and s[i - 1] in _TOKEN_CHARS:
        i -= 1
    return s[:i]


def _truncated(original_bytes: int, prefix: str, max_bytes: int) -> dict:
    """conductor truncatedBody, shrunk until its serialization fits."""
    budget = max_bytes - 128
    while True:
        p = _utf8_prefix(prefix, budget)
        env = {"truncated": True, "original_bytes": original_bytes, "prefix": p}
        n = len(encode_json(env).encode("utf-8"))
        if n <= max_bytes or budget <= 0:
            return env
        budget -= (n - max_bytes) + 16


class _Full(Exception):
    """bounded_scrub has written as much as it keeps."""


class _BoundedWriter:
    """The scrubbed serialization of a value, as encode_json(scrub(value))
    would write it, stopping (raising _Full) once `limit` characters exist.
    A leaf cut short also stops it: nothing after a cut leaf is written."""

    def __init__(self, limit: int):
        self.parts: list = []
        self.n = 0
        self.limit = limit
        # A string this much longer than the room left is not scrubbed whole.
        self.slack = max(1024, limit // 4)

    def text(self) -> str:
        return "".join(self.parts)

    def put(self, s: str) -> None:
        self.parts.append(s)
        self.n += len(s)
        if self.n >= self.limit:
            raise _Full

    def room(self) -> int:
        return max(1, self.limit - self.n)

    def _members(self, v: dict) -> list:
        try:
            return sorted(v.items(), key=lambda kv: kv[0])
        except TypeError:
            return sorted(v.items(), key=lambda kv: str(kv[0]))

    def _key(self, k: Any) -> str:
        return encode_json(_nul(k) if isinstance(k, str) else str(k)) + ":"

    # mode: "scrub" (_scrub_at), "secret" (_redact_secret), "all" (_redact_all)
    def emit(self, v: Any, depth: int, mode: str = "scrub") -> None:
        if isinstance(v, str):
            if mode == "scrub":
                self._string(v, depth)
            else:
                self.put(encode_json(REDACTED))
            return
        if isinstance(v, list):
            self.put("[")
            for i, e in enumerate(v):
                if i:
                    self.put(",")
                self.emit(e, depth, mode)
            self.put("]")
            return
        if isinstance(v, dict):
            name = v.get("name")
            secret_pair = mode == "scrub" and isinstance(name, str) and "value" in v and is_credential_key(name)
            self.put("{")
            for i, (k, val) in enumerate(self._members(v)):
                if i:
                    self.put(",")
                self.put(self._key(k))
                ks = k if isinstance(k, str) else str(k)
                if mode == "scrub":
                    if is_headers_key(ks):
                        self.emit(val, depth, "all")
                    elif is_credential_key(ks) or (secret_pair and ks == "value"):
                        self.emit(val, depth, "secret")
                    else:
                        self.emit(val, depth, "scrub")
                elif mode == "secret":
                    self.emit(val, depth, "scrub" if is_reference_key(ks) else "secret")
                elif k == "name" and isinstance(val, str):
                    self.put(encode_json(_nul(val)))
                else:
                    self.emit(val, depth, "all")
            self.put("}")
            return
        self.put(encode_json(v))

    def _string(self, s: str, depth: int) -> None:
        room = self.room()
        if len(s) <= room + self.slack:
            self.put(encode_json(scrub_string(s, depth)))
            return
        s = _nul(s)
        t = s.strip()
        if depth < MAX_NESTED_JSON and t and t[0] in "{[":
            try:
                v = _decode_json(t)
            except (ValueError, RecursionError):
                # JSON cut short (e.g. a spill read to MAX_SPILL_READ_BYTES).
                head = _drop_trailing_run(s[:room])
                self.put(encode_json(redact_json_text(head)))
                raise _Full
            inner = _BoundedWriter(room)
            try:
                inner.emit(v, depth + 1)
            except _Full:
                self.put(encode_json(inner.text()))
                raise
            self.put(encode_json(inner.text()))
            return
        head = _drop_trailing_run(s[:room])
        self.put(encode_json(redact_plain_text(head)))
        raise _Full


def bounded_scrub(value: Any, limit: int) -> tuple:
    """-> (text, complete): the leading `limit`-odd characters of
    encode_json(scrub(value)), computed without walking the rest of the
    value. Same rules as scrub(); a string leaf too long to scrub whole is
    decoded when it is JSON (and walked the same way) or cut and scrubbed
    as text (redact_json_text / redact_plain_text, both stricter than the
    leaf rule) when it is not."""
    w = _BoundedWriter(limit)
    try:
        w.emit(value, 0)
    except _Full:
        return w.text(), False
    return w.text(), True


def scrub_and_cap(value: Any, max_bytes: int = MAX_RESULT_BYTES,
                  structural_limit: int = MAX_STRUCTURAL_SCRUB_BYTES) -> tuple:
    """-> (stored value, truncated). Scrubs first, then caps, like a gateway
    receipt: a value that serializes to more than max_bytes becomes
    {truncated: true, original_bytes, prefix}. A value whose serialization
    is over structural_limit is not scrubbed whole: bounded_scrub scrubs it
    structurally, with the same rules, only as far as the kept prefix."""
    try:
        raw = encode_json(value)
    except (ValueError, TypeError, RecursionError):
        return {"truncated": True, "original_bytes": 0, "prefix": ""}, True
    raw_len = len(raw.encode("utf-8"))
    if raw_len > structural_limit:
        del raw
        try:
            text, complete = bounded_scrub(value, max_bytes)
        except RecursionError:
            return {"truncated": True, "original_bytes": raw_len, "prefix": ""}, True
        if complete and len(text.encode("utf-8")) <= max_bytes:
            try:
                return _decode_json(text), False
            except (ValueError, RecursionError):
                pass
        return _truncated(raw_len, text, max_bytes), True
    scrubbed = scrub(value)
    out = encode_json(scrubbed)
    if len(out.encode("utf-8")) <= max_bytes:
        return scrubbed, False
    return _truncated(len(out.encode("utf-8")), out, max_bytes), True


# ---------------------------------------------------------------------------
# Spool entries
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def evidence_id(server: str, tool: str, args: Any, called_at: str) -> str:
    """ev_ + the first 12 hex of sha256(server|tool|args|called_at), args as
    compact sorted-key JSON (the stored, scrubbed args, so the id can be
    recomputed from the entry)."""
    try:
        a = encode_json(args)
    except (ValueError, TypeError, RecursionError):
        a = "null"
    h = hashlib.sha256("|".join((server, tool, a, called_at)).encode("utf-8")).hexdigest()
    return "ev_" + h[:12]


def build_entry(*, server: str, tool: str, tool_name: str, tool_input: Any, tool_response: Any,
                session_id: Any, spill_root: Optional[Path] = None, called_at: Optional[str] = None,
                tool_use_id: Any = None, agent: str = "", is_error: bool = False) -> dict:
    """A spool entry (scrubbed, capped) for one MCP call. is_error: the call
    returned an error result (MCP isError, or the client's error text for
    it); its text is redacted with the stricter error-text rules, as the
    gateway does for an error receipt (RedactErrorText)."""
    called_at = called_at or now_iso()
    args, args_truncated = scrub_and_cap(well_formed(tool_input if tool_input is not None else {}), MAX_ARGS_BYTES)
    body = well_formed(normalize(tool_response, spill_root, structure_json=not is_error))
    spilled = bool(body.pop("spilled", False))
    spilled_bytes = body.pop("spilled_bytes", None)
    if is_error and isinstance(body.get("text"), list):
        body["text"] = [redact_error_text(t) if isinstance(t, str) else t for t in body["text"]]
    result, truncated = scrub_and_cap(body, MAX_RESULT_BYTES)
    entry = {
        "schema": SCHEMA,
        "evidence_id": evidence_id(server, tool, args, called_at),
        "tier": TIER,
        "session_id": session_id if isinstance(session_id, str) and SESSION_ID_RE.match(session_id) else None,
        "server": server,
        "tool": tool,
        "tool_name": tool_name,
        "called_at": called_at,
        "args": args,
        "result": result,
        "truncated": truncated,
    }
    if is_error:
        entry["is_error"] = True
    if args_truncated:
        entry["args_truncated"] = True
    if spilled:
        entry["spilled"] = True
        if isinstance(spilled_bytes, int):
            entry["spilled_bytes"] = spilled_bytes
    if isinstance(tool_use_id, str) and len(tool_use_id) <= 256:
        entry["tool_use_id"] = tool_use_id
    if agent:
        entry["agent"] = agent
    return entry


def _ensure_private_dir(path: Path) -> None:
    """mkdir 0700 (or tighten an existing one we own); refuse a symlink.
    Concurrent hooks race to create the same directory; losing that race is
    fine (the lstat after it still refuses a planted link)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise OSError(f"not a private directory: {path}")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)


def ensure_root(root: Path) -> None:
    """Create the spool root (and a missing parent, e.g. ~/.cardinal) 0700."""
    root = Path(root)
    if not root.parent.exists():
        os.makedirs(root.parent, mode=0o700, exist_ok=True)
    _ensure_private_dir(root)


def entry_path(root: Path, session_id: Any, ev_id: str) -> Path:
    if not EVIDENCE_ID_RE.match(ev_id):
        raise ValueError("bad evidence id")
    return Path(root) / session_dir_name(session_id) / f"{ev_id}.json"


def write_entry(root: Path, entry: dict) -> Path:
    """Atomically write entry to <root>/<session>/<evidence_id>.json:
    directories 0700, the file 0600 (mkstemp creates it 0600; os.replace
    swaps it in whole)."""
    root = Path(root)
    path = entry_path(root, entry.get("session_id"), entry["evidence_id"])
    for attempt in (0, 1):
        ensure_root(root)
        _ensure_private_dir(path.parent)
        try:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ev_", suffix=".tmp")
            break
        except FileNotFoundError:
            # gc() removed the (still empty) session directory in between.
            if attempt:
                raise
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def read_entry(root: Path, ev_id: str) -> Optional[dict]:
    """Find an entry by id in any session directory."""
    if not isinstance(ev_id, str) or not EVIDENCE_ID_RE.match(ev_id):
        return None
    try:
        for sdir in Path(root).iterdir():
            p = sdir / f"{ev_id}.json"
            try:
                if not stat.S_ISDIR(os.lstat(sdir).st_mode) or not stat.S_ISREG(os.lstat(p).st_mode):
                    continue
            except OSError:
                continue
            fd = os.open(str(p), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                data = json.loads(f.read())
            return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
    return None


def _read_private_json(path: Path) -> Any:
    """A regular file's JSON (a symlink is refused), or None."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            return json.loads(f.read(1 << 20))
    except (OSError, ValueError):
        return None


def session_dirs(root: Path) -> list:
    """The spool's session directories (real directories, never a link),
    most recently modified first: [(name, path)]."""
    out = []
    try:
        with os.scandir(root) as it:
            for d in it:
                if d.is_dir(follow_symlinks=False) and (SESSION_ID_RE.match(d.name) or d.name == NO_SESSION):
                    try:
                        out.append((d.stat(follow_symlinks=False).st_mtime, d.name, Path(d.path)))
                    except OSError:
                        continue
    except OSError:
        return []
    out.sort(key=lambda t: (-t[0], t[1]))
    return [(name, path) for _, name, path in out]


def list_entries(root: Path, session_id: Any) -> list:
    """Every readable entry in one session directory, oldest call first."""
    sdir = Path(root) / session_dir_name(session_id)
    try:
        if not stat.S_ISDIR(os.lstat(sdir).st_mode):
            return []
        names = sorted(n for n in os.listdir(sdir) if n.startswith("ev_") and n.endswith(".json"))
    except OSError:
        return []
    out = []
    for n in names:
        if not EVIDENCE_ID_RE.match(n[:-len(".json")]):
            continue
        data = _read_private_json(sdir / n)
        if isinstance(data, dict) and data.get("evidence_id") == n[:-len(".json")]:
            out.append(data)
    out.sort(key=lambda e: (str(e.get("called_at") or ""), e["evidence_id"]))
    return out


# ---------------------------------------------------------------------------
# Storyboard evidence tokens
#
# storyboard__create (and, while a draft, storyboard__preview) returns an
# evidence_token: a 24 h JWT that can upload evidence for that one
# storyboard and nothing else (conductor storyboard/scoped-tokens.ts). The
# Claude adapter's hooks/storyboard-token.py keeps it in
#     <root>/<session_id>/token.json     (0600, in the 0700 session dir)
# keyed by storyboard id, so `cardinal-evidence promote` can upload without
# the org API key.
# ---------------------------------------------------------------------------

TOKEN_FILE = "token.json"
TOKEN_SCHEMA = "cardinal.evidence-token.v1"
# A token file outlives its tokens (24 h) by a day, then gc() removes it.
TOKEN_RETENTION_S = 2 * 24 * 3600
MAX_TOKENS_PER_SESSION = 20
STORYBOARD_ID_RE = re.compile(r"^sb_[0-9a-f]{24}$")
EVIDENCE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,2048}\.[A-Za-z0-9_-]{1,4096}\.[A-Za-z0-9_-]{0,1024}$")
ORG_ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")
_RFC3339_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$")


def parse_time(s: Any) -> Optional[float]:
    """An RFC 3339 timestamp -> epoch seconds, or None."""
    if not isinstance(s, str) or not _RFC3339_RE.match(s):
        return None
    t = s.replace("Z", "+00:00")
    # Python 3.9's fromisoformat takes 3 or 6 fractional digits only.
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?(.*)$", t)
    if m:
        frac = (m.group(2) or "")[:6]
        t = m.group(1) + ("." + frac.ljust(6, "0") if frac else "") + m.group(3)
    try:
        return datetime.fromisoformat(t).timestamp()
    except ValueError:
        return None


def token_live(rec: Any, now: Optional[float] = None, slack_s: float = 60) -> bool:
    """True when a stored token record has not expired (with slack_s to
    spare). A record with no parseable expiry counts as live; the server
    decides."""
    if not isinstance(rec, dict):
        return False
    exp = parse_time(rec.get("expires_at"))
    now = time.time() if now is None else now
    return exp is None or exp - slack_s > now


def _valid_token_record(sb: Any, rec: Any) -> bool:
    return (isinstance(sb, str) and STORYBOARD_ID_RE.match(sb) is not None and isinstance(rec, dict)
            and isinstance(rec.get("org"), str) and ORG_ID_RE.match(rec["org"]) is not None
            and isinstance(rec.get("evidence_token"), str)
            and EVIDENCE_TOKEN_RE.match(rec["evidence_token"]) is not None)


def read_tokens(root: Path, session_id: Any) -> dict:
    """{storyboard_id: {org, evidence_token, expires_at?, stored_at}} from a
    session's token file; malformed records are dropped."""
    data = _read_private_json(Path(root) / session_dir_name(session_id) / TOKEN_FILE)
    sbs = data.get("storyboards") if isinstance(data, dict) else None
    if not isinstance(sbs, dict):
        return {}
    return {sb: rec for sb, rec in sbs.items() if _valid_token_record(sb, rec)}


def store_token(root: Path, session_id: Any, *, storyboard_id: str, org: str, token: str,
                expires_at: Any = None, now: Optional[float] = None) -> Path:
    """Record one storyboard's evidence token in the session's token file
    (atomic, 0600; directories 0700). Expired records are dropped and at
    most MAX_TOKENS_PER_SESSION are kept, newest first."""
    now = time.time() if now is None else now
    rec = {"org": org, "evidence_token": token, "stored_at": datetime.fromtimestamp(now, timezone.utc)
           .strftime("%Y-%m-%dT%H:%M:%SZ")}
    if parse_time(expires_at) is not None:
        rec["expires_at"] = expires_at
    if not _valid_token_record(storyboard_id, rec):
        raise ValueError("bad storyboard token record")
    root = Path(root)
    tokens = {sb: r for sb, r in read_tokens(root, session_id).items() if sb != storyboard_id and token_live(r, now, 0)}
    tokens[storyboard_id] = rec
    keep = sorted(tokens.items(), key=lambda kv: str(kv[1].get("stored_at") or ""), reverse=True)
    body = {"schema": TOKEN_SCHEMA, "storyboards": dict(keep[:MAX_TOKENS_PER_SESSION])}
    return _write_session_json(root, session_id, TOKEN_FILE, ".token_", body)


def _write_session_json(root: Path, session_id: Any, name: str, tmp_prefix: str, body: dict) -> Path:
    """Atomically write <root>/<session>/<name> (0600; directories 0700)."""
    sdir = Path(root) / session_dir_name(session_id)
    ensure_root(Path(root))
    _ensure_private_dir(sdir)
    fd, tmp = tempfile.mkstemp(dir=str(sdir), prefix=tmp_prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(body, separators=(",", ":")))
        os.chmod(tmp, 0o600)
        os.replace(tmp, sdir / name)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return sdir / name


def find_tokens(root: Path, storyboard_id: Optional[str] = None, session_id: Any = None) -> list:
    """[(session, storyboard_id, record)] across the spool (or one session),
    newest session first, optionally for one storyboard."""
    if session_id is not None:
        sessions = [session_dir_name(session_id)]
    else:
        sessions = [name for name, _ in session_dirs(root)]
    out = []
    for s in sessions:
        for sb, rec in read_tokens(root, s).items():
            if storyboard_id is None or sb == storyboard_id:
                out.append((s, sb, rec))
    return out


# ---------------------------------------------------------------------------
# Promotion ledger
#
# `cardinal-evidence promote` records each receipt it gets back in
#     <root>/<session_id>/promoted.json    (0600, in the 0700 session dir)
# keyed by storyboard id, then evidence id, so promoting an entry again into
# the same storyboard prints the receipt it already has instead of uploading
# a duplicate. A record is reused for PROMOTED_REUSE_S only: maestro sweeps an
# unreferenced receipt after 14 days (conductor receipts.ts
# RECEIPT_RETENTION_DAYS), so an older one is uploaded again.
# ---------------------------------------------------------------------------

PROMOTED_FILE = "promoted.json"
PROMOTED_SCHEMA = "cardinal.evidence-promoted.v1"
PROMOTED_REUSE_S = 7 * 24 * 3600
MAX_PROMOTED_STORYBOARDS = MAX_TOKENS_PER_SESSION
RECEIPT_ID_RE = re.compile(r"^rcpt_[0-9a-f]{24}$")


def _valid_promotion(ev_id: Any, rec: Any) -> bool:
    return (isinstance(ev_id, str) and EVIDENCE_ID_RE.match(ev_id) is not None and isinstance(rec, dict)
            and isinstance(rec.get("receipt_id"), str) and RECEIPT_ID_RE.match(rec["receipt_id"]) is not None
            and parse_time(rec.get("promoted_at")) is not None)


def read_promoted(root: Path, session_id: Any) -> dict:
    """{storyboard_id: {evidence_id: {receipt_id, promoted_at}}} from a
    session's ledger; malformed records are dropped."""
    data = _read_private_json(Path(root) / session_dir_name(session_id) / PROMOTED_FILE)
    sbs = data.get("storyboards") if isinstance(data, dict) else None
    if not isinstance(sbs, dict):
        return {}
    out = {}
    for sb, recs in sbs.items():
        if isinstance(sb, str) and STORYBOARD_ID_RE.match(sb) and isinstance(recs, dict):
            good = {ev_id: rec for ev_id, rec in recs.items() if _valid_promotion(ev_id, rec)}
            if good:
                out[sb] = good
    return out


def promoted_receipt(root: Path, session_id: Any, storyboard_id: str, ev_id: str,
                     now: Optional[float] = None) -> Optional[str]:
    """The receipt an entry was promoted to in this storyboard, if that was
    recent enough to reuse (PROMOTED_REUSE_S); else None."""
    rec = read_promoted(root, session_id).get(storyboard_id, {}).get(ev_id)
    if rec is None:
        return None
    now = time.time() if now is None else now
    at = parse_time(rec["promoted_at"])
    return rec["receipt_id"] if at is not None and now - at < PROMOTED_REUSE_S else None


def record_promoted(root: Path, session_id: Any, storyboard_id: str, receipts: dict,
                    now: Optional[float] = None) -> Optional[Path]:
    """Add {evidence_id: receipt_id} for one storyboard to the session's
    ledger (atomic, 0600). At most MAX_PROMOTED_STORYBOARDS storyboards are
    kept, most recently promoted first."""
    if not STORYBOARD_ID_RE.match(storyboard_id or ""):
        raise ValueError("bad storyboard id")
    now = time.time() if now is None else now
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = {ev_id: {"receipt_id": r, "promoted_at": stamp} for ev_id, r in receipts.items()}
    new = {ev_id: rec for ev_id, rec in new.items() if _valid_promotion(ev_id, rec)}
    if not new:
        return None
    ledger = read_promoted(root, session_id)
    ledger[storyboard_id] = {**ledger.get(storyboard_id, {}), **new}

    def newest(kv):
        return max(str(r.get("promoted_at") or "") for r in kv[1].values())

    keep = sorted(ledger.items(), key=newest, reverse=True)[:MAX_PROMOTED_STORYBOARDS]
    body = {"schema": PROMOTED_SCHEMA, "storyboards": dict(keep)}
    return _write_session_json(root, session_id, PROMOTED_FILE, ".promoted_", body)


def set_capture_disabled(root: Path, disabled: bool) -> None:
    """Write (disabled) or remove the opt-out flag file <root>/disabled."""
    root = Path(root)
    flag = root / DISABLED_FLAG
    if disabled:
        ensure_root(root)
        fd = os.open(str(flag), os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        return
    try:
        os.unlink(flag)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Client capture: one MCP call, as a client hook records it
# ---------------------------------------------------------------------------

# A version token a client reports about itself ("1.7.29", "0.50.0-nightly.1").
_CLIENT_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}")
_CLIENT_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")


def client_string(name: str, version: Any = None) -> str:
    """The client an entry names: "<name>/<version>" (e.g. "cursor/1.7.29",
    the shape of a receipt's `client`), or the bare name when the version is
    missing or is not a plain version token (it comes from a hook payload or
    a file on disk, so it is never trusted into the spool as free text)."""
    if not isinstance(name, str) or not _CLIENT_NAME_RE.fullmatch(name):
        raise ValueError("bad client name")
    if isinstance(version, str) and _CLIENT_VERSION_RE.fullmatch(version):
        return f"{name}/{version}"
    return name


def captured_line(ev_id: str, server: str, tool: str) -> str:
    """The context line a hook hands the model beside a captured result."""
    return f"[evidence:{ev_id}] captured locally from {server}/{tool}"


def capture(home: Path, *, server: str, tool: str, tool_name: str, tool_input: Any, tool_response: Any,
            session_id: Any, tool_use_id: Any = None, agent: str = "", spill_root: Optional[Path] = None,
            env: Optional[dict] = None, is_error: bool = False) -> Optional[dict]:
    """Record one MCP call in the spool under home: nothing when the user
    opted out (capture_disabled), else build_entry -> write_entry, then an
    opportunistic gc(). Returns the written entry, or None when disabled.
    A failed write raises; hooks call this inside their fail-open guard."""
    root = default_root(home)
    if capture_disabled(root, env):
        return None
    entry = build_entry(
        server=server,
        tool=tool,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_response=tool_response,
        session_id=session_id,
        spill_root=spill_root,
        tool_use_id=tool_use_id,
        agent=agent,
        is_error=is_error,
    )
    write_entry(root, entry)
    try:
        gc(root)
    except Exception:
        pass
    return entry


# ---------------------------------------------------------------------------
# GC
# ---------------------------------------------------------------------------

def spool_cap_bytes(env: Optional[dict] = None) -> int:
    """MAX_SPOOL_BYTES, or CARDINAL_EVIDENCE_MAX_MB (1..65536) MiB."""
    env = os.environ if env is None else env
    v = str(env.get(MAX_MB_ENV, "")).strip()
    if v.isdigit() and 1 <= int(v) <= 65536:
        return int(v) << 20
    return MAX_SPOOL_BYTES


def gc(root: Path, now: Optional[float] = None, retention_s: float = RETENTION_S,
       budget_s: float = GC_BUDGET_S, interval_s: float = GC_INTERVAL_S, force: bool = False,
       max_bytes: Optional[int] = None, max_per_session: int = MAX_ENTRIES_PER_SESSION) -> int:
    """Remove spool entries older than retention_s (by mtime), token files
    older than TOKEN_RETENTION_S, temp files of dead writes, then (size
    bounds) the oldest entries beyond max_per_session in a session and,
    while the spool is over max_bytes (default spool_cap_bytes()), entries
    of the least recently active sessions, oldest first; then emptied
    session directories. Opportunistic: at most once per interval_s (a stamp
    file under root) unless force, and stops after budget_s. Never raises.
    Returns the number of files removed.

    The size bound holds for a spool of any size: each session's totals are
    kept in GC_INDEX, keyed by the session directory's mtime (any entry
    written, replaced or removed changes it), so a pass only lists the
    sessions that changed or have something due to expire. A pass that runs
    out of budget keeps what it learned and asks for another pass in
    GC_RETRY_S instead of waiting out the interval."""
    now = time.time() if now is None else now
    deadline = time.monotonic() + budget_s
    root = Path(root)
    cap = spool_cap_bytes() if max_bytes is None else max_bytes
    removed = 0
    try:
        st = os.lstat(root)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return 0
        stamp = root / GC_STAMP
        if not force:
            try:
                if now - stamp.stat().st_mtime < interval_s:
                    return 0
            except OSError:
                pass
        try:
            fd = os.open(str(stamp), os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
            os.utime(str(stamp), (now, now))
        except OSError:
            pass
        old_index = _gc_index_load(root)
        index = {}       # session name -> [dir mtime_ns, bytes, entries, due, newest]
        stale = []       # (name, path, dir mtime_ns) to list this pass
        with os.scandir(root) as sessions:
            for sdir in sessions:
                if not sdir.is_dir(follow_symlinks=False):
                    continue
                try:
                    dm = sdir.stat(follow_symlinks=False).st_mtime_ns
                except OSError:
                    continue
                c = old_index.get(sdir.name)
                if c is not None and c[0] == dm and now < c[3] and c[2] <= max_per_session:
                    index[sdir.name] = c
                else:
                    stale.append((sdir.name, sdir.path, dm))
        complete = True
        empty = []
        for name, path, dm in stale:
            if time.monotonic() > deadline:
                complete = False
                break
            got = _gc_session(path, now, retention_s, deadline, max_per_session)
            if got is None:
                complete = False
                break
            n_removed, n_left, entries, due = got
            removed += n_removed
            if n_left <= 0:
                empty.append(path)
                continue
            # The mtime read BEFORE listing: a write during the listing
            # leaves the stored mtime stale, so the next pass lists again.
            index[name] = [dm if n_removed == 0 else -1, sum(e[1] for e in entries), len(entries), due,
                           max((e[0] for e in entries), default=0.0)]
        if complete:
            total = sum(c[1] for c in index.values())
            if total > cap:
                # Least recently active sessions first; the oldest entries
                # of each first.
                for name in sorted(index, key=lambda k: (index[k][4], k)):
                    if total <= cap:
                        break
                    if time.monotonic() > deadline:
                        complete = False
                        break
                    path = os.path.join(str(root), name)
                    entries = _gc_entries(path, deadline)
                    if entries is None:
                        complete = False
                        break
                    entries.sort()
                    for _, size, p in entries:
                        if total <= cap or time.monotonic() > deadline:
                            break
                        try:
                            os.unlink(p)
                            removed += 1
                            total -= size
                        except OSError:
                            pass
                    index[name] = [-1, 0, 0, 0.0, 0.0]   # list it again next pass
                    if total > cap and time.monotonic() > deadline:
                        complete = False
                        break
        for path in empty:
            try:
                os.rmdir(path)
            except OSError:
                pass
        _gc_index_save(root, index)
        if not complete:
            try:
                t = now - interval_s + GC_RETRY_S
                os.utime(str(stamp), (t, t))
            except OSError:
                pass
    except OSError:
        pass
    return removed


GC_INDEX = ".gc-index.json"
# A pass that ran out of budget runs again this soon, not a whole interval
# later, so a large spool is brought under its bounds in a few passes.
GC_RETRY_S = 30
MAX_GC_INDEX_BYTES = 8 << 20


def _gc_index_load(root: Path) -> dict:
    """{session: [dir mtime_ns, bytes, entries, due, newest]} from GC_INDEX;
    anything malformed is dropped (that session is listed again)."""
    data = None
    try:
        fd = os.open(str(root / GC_INDEX), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            if stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                data = json.loads(f.read(MAX_GC_INDEX_BYTES))
    except (OSError, ValueError):
        data = None
    out = {}
    if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
        for k, v in data["sessions"].items():
            if (isinstance(k, str) and SESSION_ID_RE.match(k) and isinstance(v, list) and len(v) == 5
                    and isinstance(v[0], int) and isinstance(v[1], int) and isinstance(v[2], int)
                    and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)):
                out[k] = v
    return out


def _gc_index_save(root: Path, index: dict) -> None:
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(root), prefix=".gc-index_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"version": 1, "sessions": index}, separators=(",", ":")))
        os.replace(tmp, str(root / GC_INDEX))
        tmp = None
    except (OSError, ValueError, TypeError):
        pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _gc_entries(path: str, deadline: float) -> Optional[list]:
    """[(mtime, size, path)] of a session's entries, or None past deadline."""
    out = []
    try:
        with os.scandir(path) as files:
            for f in files:
                if time.monotonic() > deadline:
                    return None
                if not (f.name.startswith("ev_") and f.name.endswith(".json")):
                    continue
                try:
                    fst = f.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(fst.st_mode):
                    out.append((fst.st_mtime, fst.st_size, f.path))
    except OSError:
        return []
    return out


def _gc_session(path: str, now: float, retention_s: float, deadline: float,
                max_per_session: int) -> Optional[tuple]:
    """List one session directory: remove what expired (entries, token,
    promotion ledger, marker, dead temp files) and the oldest entries beyond max_per_session.
    -> (removed, files left, [(mtime, size, path)] of entries kept, the
    time the next file here expires), or None past deadline."""
    removed = 0
    n_left = 0
    entries = []
    due = float("inf")
    try:
        with os.scandir(path) as files:
            for f in files:
                if time.monotonic() > deadline:
                    return None
                try:
                    fst = f.stat(follow_symlinks=False)
                except OSError:
                    continue
                name = f.name
                is_entry = name.startswith("ev_") and name.endswith(".json")
                if is_entry:
                    ttl = retention_s
                elif name == TOKEN_FILE:
                    ttl = min(retention_s, TOKEN_RETENTION_S)
                elif name == HINTED:
                    ttl = retention_s
                elif name == PROMOTED_FILE:
                    ttl = retention_s
                elif name.startswith((".ev_", ".token_", ".promoted_")) and name.endswith(".tmp"):
                    ttl = STALE_TMP_S
                else:
                    ttl = None
                if ttl is not None and not stat.S_ISDIR(fst.st_mode):
                    if now - fst.st_mtime > ttl:
                        try:
                            os.unlink(f.path)
                            removed += 1
                            continue
                        except OSError:
                            pass
                    else:
                        due = min(due, fst.st_mtime + ttl)
                n_left += 1
                if is_entry and stat.S_ISREG(fst.st_mode):
                    entries.append((fst.st_mtime, fst.st_size, f.path))
    except OSError:
        return removed, 1, [], now
    if len(entries) > max_per_session:
        entries.sort()
        drop, entries = entries[:len(entries) - max_per_session], entries[len(entries) - max_per_session:]
        for _, _, p in drop:
            try:
                os.unlink(p)
                removed += 1
                n_left -= 1
            except OSError:
                pass
    if due == float("inf"):
        due = now + retention_s
    return removed, n_left, entries, due


def spool_usage(root: Path) -> tuple:
    """(entries, bytes, sessions) across the spool."""
    n = size = sessions = 0
    for _, sdir in session_dirs(root):
        sessions += 1
        try:
            with os.scandir(sdir) as it:
                for f in it:
                    if f.name.startswith("ev_") and f.name.endswith(".json"):
                        try:
                            size += f.stat(follow_symlinks=False).st_size
                            n += 1
                        except OSError:
                            continue
        except OSError:
            continue
    return n, size, sessions
