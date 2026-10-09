"""Generic evidence capture: ONE pipeline for every tool call.

Any tool call an agent makes (a built-in tool, any MCP server's tool, a tool
that does not exist yet) is recorded the same way, in the local spool
(cardinal_core.evidence, ~/.cardinal/evidence/<session>/ev_<12 hex>.json), so
a storyboard can later cite it. Nothing here touches the network: a result
leaves the machine only when the author promotes it
(cardinal_core.evidence_promote).

    capture_call(ToolCall)
      0  (adapter) budget guard: time_guard(), read_stdin_bounded()
      1  opt-out?  CARDINAL_EVIDENCE_CAPTURE=0 | <spool>/disabled  -> nothing
      2  dedupe:   source kind "cardinal" (Cardinal's own gateway already
                   minted a witnessed receipt)                      -> nothing
         control plane: any input string runs `cardinal-storyboard
                   investigation …` (the investigation's control log is
                   never evidence)                                  -> nothing
      3  sensitivity gate over every key and string of tool_input
         (cardinal_core.evidence_gate; tool-neutral)          -> withheld stub
      4  normalize: the first matching pluggable normalizer, else generic
         (cardinal_core.evidence_normalizers; shape only, never a gate)
      5  redact every field: the gateway's scrub (evidence.scrub) plus
         plain-text key=value rules, sensitive-path lines, base64 blobs and
         local paths (Claude's session temp dir -> [session tmp], spill
         root -> [local file], the dash-encoded cwd/$HOME -> [cwd]/[home],
         cwd -> ".", $HOME -> "~"); a summary is scrubbed before it is clipped
      6  cap: args <= 64 KiB, result <= 256 KiB, else conductor's
         {truncated, original_bytes, prefix} envelope
      7  write (atomic, 0600 in a 0700 dir) + gc (TTL, 256 MiB, 10k/session)

Steps 1-3 are the whole capture decision; none of them names a tool.

Entry schema: cardinal.evidence.v2 (see SCHEMA_V2 below and
docs/specs/generic-evidence-capture.md). v1 entries stay readable.
"""

from __future__ import annotations

import hashlib
import os
import re
import signal
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import evidence
from . import evidence_gate as gate
from .evidence_normalizers import MAX_SUMMARY_CHARS, Normalized, ToolCall, normalize_call

SCHEMA_V2 = "cardinal.evidence.v2"

# Budget: under the 2 s a Claude Code hook gets, leaving room for
# interpreter start-up and, when the pipeline runs out of time, the withheld
# stub capture_call_guarded writes instead (FALLBACK_BUDGET_S).
TIME_BUDGET_S = 1.4
MAX_STDIN_BYTES = 32 << 20
MAX_TOOL_NAME = 256
HINTED = ".hinted"
BASE64_MIN = 4096
LOCAL_FILE = evidence.LOCAL_FILE

CONTEXT_ENV = "CARDINAL_EVIDENCE_CONTEXT"

SOURCE_MCP = "mcp"
SOURCE_BUILTIN = "builtin"
SOURCE_TOOL = "tool"
SOURCE_CARDINAL = "cardinal"

# The characters maestro accepts in an identifier (evidence-upload.ts ident /
# gateway external.go checkIdent): ^[A-Za-z0-9_.:/@-]+(?: [A-Za-z0-9_.:/@-]+)*$
IDENT_RE = re.compile(r"^[A-Za-z0-9_.:/@-]+(?: [A-Za-z0-9_.:/@-]+)*$")
_IDENT_BAD = re.compile(r"[^A-Za-z0-9_.:/@ -]")
_RUNTIME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def mcp_source(server: str, runtime: str) -> dict:
    return {"kind": SOURCE_MCP, "server": server, "runtime": runtime}


def builtin_source(runtime: str) -> dict:
    return {"kind": SOURCE_BUILTIN, "runtime": runtime}


def classify_mcp_name(tool_name: Any, runtime: str, cardinal_servers=()) -> tuple:
    """(source, short tool) for a runtime that names MCP tools
    mcp__<server>__<tool> (Claude Code, Cursor, Codex): an MCP source for
    such a name (kind "cardinal" for Cardinal's own gateway), else a
    built-in source with the whole name as the tool."""
    name = tool_name if isinstance(tool_name, str) else ""
    parts = evidence.split_mcp_tool(name)
    if parts is not None:
        server, tool = parts
        if server in cardinal_servers:
            return {"kind": SOURCE_CARDINAL, "server": server, "runtime": runtime}, tool
        return mcp_source(server, runtime), tool
    return builtin_source(runtime), name


def ident(s: Any, max_len: int, empty: str) -> str:
    """s as a maestro identifier: characters outside [A-Za-z0-9_.:/@-] ->
    "_", runs of spaces -> one, trimmed, at most max_len characters."""
    t = _IDENT_BAD.sub("_", s if isinstance(s, str) else "")
    t = re.sub(r" +", " ", t).strip()[:max_len].strip()
    return t if t and IDENT_RE.match(t) else empty


def wire_source_server(source: Any, fallback: Any = None) -> str:
    """The upload's source_server for an entry's source: the MCP server
    (":" -> "_", so the builtin:/tool: namespaces stay the plugin's own),
    "builtin:<runtime>", or "tool:<runtime>"."""
    if isinstance(source, dict):
        kind = source.get("kind")
        rt = ident(source.get("runtime"), 60, "agent")
        if kind == SOURCE_BUILTIN:
            return "builtin:" + rt
        if kind == SOURCE_TOOL:
            return "tool:" + rt
        if kind == SOURCE_MCP:
            return ident(str(source.get("server") or "").replace(":", "_"), 128, "unknown_server")
    return ident(fallback, 128, "unknown_server")


# ---------------------------------------------------------------------------
# Ids
# ---------------------------------------------------------------------------

def evidence_id_v2(runtime: str, session_id: Any, tool_use_id: Any) -> Optional[str]:
    """ev_ + sha256("cardinal.evidence.v2|runtime|session|tool_use_id")[:12]
    when the runtime gives a tool-use id (deterministic: a re-fired hook
    rewrites the same file, and a JS adapter computes the same id;
    core/tests/testdata/evidence_id_vectors.json pins both). None
    otherwise."""
    if not isinstance(tool_use_id, str) or not tool_use_id or len(tool_use_id) > 256:
        return None
    s = "|".join((SCHEMA_V2, runtime or "", evidence.session_dir_name(session_id), tool_use_id))
    return "ev_" + hashlib.sha256(s.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]


def _random_id(server: str, tool: str, called_at: str) -> str:
    s = "|".join((SCHEMA_V2, server, tool, called_at, os.urandom(8).hex()))
    return "ev_" + hashlib.sha256(s.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Step 5: redaction beyond the gateway's leaf rule
# ---------------------------------------------------------------------------

_B64_RUN = re.compile(r"[A-Za-z0-9+/=_\-\r\n]+")
_DATA_URL = re.compile(r"data:[^,;\s]{0,100}(?:;[^,;\s]{0,100}){0,4};base64,", re.IGNORECASE)


# Claude Code's per-session temp dir: <TMPDIR>/claude-<uid>/<cwd with every
# character outside [A-Za-z0-9] as "-">/... (e.g.
# /private/tmp/claude-501/-Users-alice-git-app/<session uuid>/scratchpad).
# The encoded segment names the user; a shape rule, not a list of TMPDIRs.
_SESSION_TMP = re.compile(r"(?<=/)(claude-\d+/)-[A-Za-z0-9._-]+(?![A-Za-z0-9._-])")
SESSION_TMP = "[session tmp]"
# An encoded path (see _SESSION_TMP) shorter than this is too generic to
# rewrite ("-root" would hit "--root-dir").
MIN_ENCODED = 6
_ENCODED_LEFT = r"(?:^|(?<=[/\s\"'=`]))"
_ENCODED_RIGHT = r"(?=[-/\u2026]|$|[^\w])"


def encode_path(p: str) -> str:
    """A path as Claude Code encodes it into a directory name: every
    character outside [A-Za-z0-9] becomes "-" (/Users/alice -> -Users-alice)."""
    return re.sub(r"[^A-Za-z0-9]", "-", p.rstrip("/"))


class _Local:
    """Local path rewrites, in this order: Claude's session temp dir ->
    claude-<uid>/[session tmp]; spill root -> [local file] (whole path, so
    the encoded project dir, session id and file name under it all go);
    the dash-encoded cwd/home (-Users-alice-app) -> [cwd]/[home], longest
    first; cwd -> ".", home -> "~". The spill rule must precede the encoded
    rules: a spill path holds the encoded cwd, and rewriting that first
    would end the spill match at the "]" of "[cwd]"."""

    def __init__(self, spill_root: Optional[str], cwd: Optional[str], home: Optional[str]):
        self.rules = [("claude-", _SESSION_TMP, lambda m: m.group(1) + SESSION_TMP)]
        roots = set()
        if spill_root:
            roots.add(str(spill_root))
            try:
                roots.add(str(Path(spill_root).resolve()))
            except (OSError, RuntimeError):
                pass
        for r in sorted(roots, key=len, reverse=True):
            if len(r) > 1:
                self.rules.append((r, re.compile(re.escape(r) + r"(?:/[^\s\]\"'`)]*)?"), LOCAL_FILE))
        encoded = {}
        for p, repl in ((cwd, "[cwd]"), (home, "[home]")):
            if not isinstance(p, str) or not p.startswith("/"):
                continue
            paths = {p}
            try:
                paths.add(os.path.realpath(p))
            except (OSError, ValueError):
                pass
            for q in paths:
                enc = encode_path(q)
                if len(enc) >= MIN_ENCODED and enc not in encoded:
                    encoded[enc] = repl
        for enc in sorted(encoded, key=len, reverse=True):
            rx = re.compile(_ENCODED_LEFT + re.escape(enc) + _ENCODED_RIGHT)
            self.rules.append((enc, rx, encoded[enc]))
        for p, repl in ((cwd, "."), (home, "~")):
            if isinstance(p, str) and len(p.rstrip("/")) > 1 and p.startswith("/"):
                q = p.rstrip("/")
                self.rules.append((q, re.compile(re.escape(q) + r"(?=/|$|[\s\"'`:;,)\]}])"), repl))

    def apply(self, s: str) -> str:
        for needle, rx, repl in self.rules:
            if needle in s:
                s = rx.sub(repl, s)
        return s


_LINE_HEAD = re.compile(r"^([^\s:]{1,1024})")
_MAX_HEAD_CUTS = 16


def _sensitive(p: str, home: Optional[str], cwd: Optional[str]) -> bool:
    q = gate._norm(p, cwd, home)
    return bool(q and gate.full_path_rule(q, home))


def _path_lines(s: str, home: Optional[str], cwd: Optional[str]) -> str:
    """grep/rg output naming a sensitive file keeps only `<path>:[withheld]`
    for that file's lines: `path:line:text` and `path:text` match lines,
    `path-line-text` / `path-text` context lines, and rg's --heading form
    (a line that is only the path, then its lines up to a blank line)."""
    if ":" not in s and "-" not in s and "/" not in s and "." not in s:
        return s
    out = []
    in_block = False
    seen: dict = {}

    def sensitive(p: str) -> bool:
        v = seen.get(p)
        if v is None:
            if len(seen) > 4096:
                seen.clear()
            v = seen[p] = _sensitive(p, home, cwd)
        return v

    for line in s.split("\n"):
        if in_block:
            if line.strip() == "":
                in_block = False
                out.append(line)
            else:
                out.append("[withheld]")
            continue
        m = _LINE_HEAD.match(line)
        if m and (":" in line or "-" in m.group(1)):
            head = m.group(1)
            cut = None
            if len(head) < len(line) and line[len(head)] == ":" and not line.startswith("//", len(head) + 1) \
                    and sensitive(head):
                cut = len(head)
            else:
                n = 0
                for i, c in enumerate(head):
                    if c == "-" and i > 0:
                        n += 1
                        if n > _MAX_HEAD_CUTS:
                            break
                        if sensitive(head[:i]):
                            cut = i
                            break
            if cut is not None:
                out.append(line[:cut] + line[cut] + "[withheld]")
                continue
        stripped = line.strip()
        if stripped and " " not in stripped and sensitive(stripped):
            # rg --heading: the path alone, then its lines. The heading
            # itself is kept only when it has no separator in it.
            out.append(line if ":" not in stripped else "[withheld]")
            in_block = True
            continue
        out.append(line)
    return "\n".join(out)


def _is_base64_blob(s: str) -> bool:
    """A string of at least BASE64_MIN characters that is a base64 (or
    base64url) run, or a data: URL of one. Encoded bytes use the whole
    alphabet (upper and lower case, digits and "+/" or "-_"), which a hex
    listing or a run of one letter does not."""
    if len(s) < BASE64_MIN:
        return False
    m = _DATA_URL.match(s)
    body = s[m.end():] if m else s
    if len(body) < BASE64_MIN or _B64_RUN.fullmatch(body) is None:
        return False
    if m:
        return True
    sample = body[:BASE64_MIN]
    return (any(c.isupper() for c in sample) and any(c.islower() for c in sample)
            and any(c.isdigit() for c in sample) and any(c in "+/-_" for c in sample))


_YAML_NAME_VALUE = re.compile(
    r"(?m)^([ \t]*-?[ \t]*)name:[ \t]*[\"']?([A-Za-z_][A-Za-z0-9_.\-]{0,255})[\"']?[ \t]*\r?\n"
    r"([ \t]*)value:[ \t]*(?!\[redacted\])(\S[^\n]*)$")


def redact_text_wide(s: str) -> str:
    """The plugin's stricter text pass on top of the gateway's rules: a
    key=value / key: value pair whose key is secret-ish in the wider sense
    (OPENAI_KEY=..., tls.key: ...; gate.secretish_name) and a YAML
    `- name: DB_PASSWORD` / `value: ...` pair (kubectl -o yaml env)."""
    s = evidence._redact_key_values(s, gate.secretish_name)
    if "value:" in s and "name:" in s:
        s = _YAML_NAME_VALUE.sub(
            lambda m: (m.group(0)[:m.start(4) - m.start(0)] + evidence.REDACTED)
            if gate.secretish_name(m.group(2)) else m.group(0), s)
    return s


def scrub_prompt(s: str) -> str:
    """A prompt the owner typed (an Investigation's owner input), with the
    same credential rules a captured plain-text result gets: the gateway's
    plain-text pass (token shapes, URL userinfo, auth schemes, credential
    key=value pairs) and redact_text_wide. Nothing else changes: no path
    rewrites and no withheld lines, so the rest stays as typed."""
    return redact_text_wide(evidence.redact_plain_text(s))


def _looks_json(s: str) -> bool:
    t = s.lstrip()
    return bool(t) and t[0] in "{["


class _Hardener:
    """Walks a value in the order encode_json serializes it (sorted keys) and
    applies the stricter text rules to every string leaf. Past `budget`
    characters of output the rest of the value cannot fit in the capped
    prefix, so later strings are dropped (and the caller marks the value
    truncated)."""

    def __init__(self, budget: int, local: _Local, home: Optional[str], cwd: Optional[str]):
        self.left = budget
        self.local = local
        self.home = home
        self.cwd = cwd
        self.cut = False

    def string(self, s: str, depth: int = 0) -> str:
        if self.left <= 0:
            self.cut = True
            return ""
        s = evidence._nul(s)
        decoded = None
        if (not _is_base64_blob(s) and depth < evidence.MAX_NESTED_JSON and _looks_json(s)
                and len(s) <= evidence.MAX_STRUCTURAL_SCRUB_BYTES):
            decoded = evidence._json_container(s)
        if _is_base64_blob(s):
            out = f"[binary omitted: {len(s)} bytes]"
        elif decoded is not None:
            # A JSON document in a string: its own leaves get the same rules;
            # evidence.scrub_string scrubs it structurally afterwards.
            # The text is kept as written unless a rule changed a leaf.
            try:
                hardened = self.walk(decoded, depth + 1)
                out = self.local.apply(s) if hardened == decoded else evidence.encode_json(hardened)
            except (ValueError, TypeError):
                out = evidence.redact_json_text(s)
            self.left -= 2
            return out
        else:
            if len(s) > self.left + 1024:
                s = evidence._drop_trailing_run(s[: self.left + 1024])
                self.cut = True
            if _looks_json(s):
                out = evidence.redact_json_text(s)
            else:
                out = redact_text_wide(evidence.redact_plain_text(_path_lines(s, self.home, self.cwd)))
            out = self.local.apply(out)
        self.left -= len(out) + 2
        return out

    def walk(self, v: Any, depth: int = 0) -> Any:
        if isinstance(v, str):
            return self.string(v, depth)
        # Past the budget nothing more can reach the stored prefix: the rest
        # of a container is dropped (cut), not walked, so the scrub after
        # this pass only sees what can be kept. (A 4 MiB pod list used to
        # be walked and scrubbed whole: over a second per call.)
        if isinstance(v, list):
            out_l = []
            for e in v:
                if self.left <= 0:
                    self.cut = True
                    break
                out_l.append(self.walk(e, depth))
            return out_l
        if isinstance(v, dict):
            try:
                items = sorted(v.items(), key=lambda kv: kv[0])
            except TypeError:
                items = sorted(v.items(), key=lambda kv: str(kv[0]))
            out = {}
            # {"name": "OPENAI_KEY", "value": "..."} (k8s env, ECS, docker)
            nv_secret = isinstance(v.get("name"), str) and gate.secretish_name(v["name"]) and "value" in v
            for k, e in items:
                if self.left <= 0:
                    self.cut = True
                    break
                if isinstance(k, str):
                    self.left -= len(k) + 3
                if ((isinstance(k, str) and gate.secretish_name(k)) or (nv_secret and k == "value")) \
                        and isinstance(e, (str, int, float)) and not isinstance(e, bool) and e != "":
                    out[k] = evidence.REDACTED
                    self.left -= len(evidence.REDACTED)
                    continue
                out[k] = self.walk(e, depth)
            return out
        if v is not None and not isinstance(v, (bool, int, float)):
            return str(v)
        self.left -= 8
        return v


def redact_and_cap(value: Any, max_bytes: int, local: _Local, home: Optional[str], cwd: Optional[str]) -> tuple:
    """-> (stored value, truncated). The hardening pass, then the gateway's
    scrub and cap (evidence.scrub_and_cap). A value whose tail had to be
    dropped to stay in budget is always stored as the truncation envelope,
    with the original size."""
    try:
        raw_len = len(evidence.encode_json(value).encode("utf-8", errors="surrogatepass"))
    except (ValueError, TypeError, RecursionError):
        raw_len = 0
    h = _Hardener(max_bytes + (max_bytes >> 2), local, home, cwd)
    try:
        hardened = h.walk(value)
    except RecursionError:
        return {"truncated": True, "original_bytes": raw_len, "prefix": ""}, True
    stored, truncated = evidence.scrub_and_cap(hardened, max_bytes)
    if not h.cut:
        return stored, truncated
    if truncated and isinstance(stored, dict):
        stored["original_bytes"] = max(raw_len, int(stored.get("original_bytes") or 0))
        return stored, True
    try:
        text = evidence.encode_json(stored)
    except (ValueError, TypeError, RecursionError):
        text = ""
    return evidence._truncated(raw_len, text, max_bytes), True


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class Captured:
    entry: dict
    line: Optional[str]    # the context line for the agent, or None
    withheld: bool = False


# The run of characters a path segment or word can end in (see _clip_scrubbed).
_PARTIAL_TAIL = re.compile(r"[^\s/\\:;,=\"'`()\[\]{}<>|&]+$")


def _clip_scrubbed(s: Any, n: int, local: _Local) -> Optional[str]:
    """s scrubbed, THEN clipped to one line of at most n characters. This is
    the only clip: a cut made before the scrub can land inside a path
    (/Users/mgr…) that the local rules no longer recognise. The scrub sees
    at most n*4 characters; when s is longer (or was already clipped
    upstream, ending in "…"), its partial last segment is dropped first for
    the same reason, and the result ends in "…"."""
    if not isinstance(s, str) or not s:
        return None
    raw = evidence._nul(s[: n * 4])
    cut = len(s) > n * 4 or (len(s) == n * 4 and raw.endswith("…"))
    if cut:
        # A single run with no separator at all is kept (it holds no path).
        raw = _PARTIAL_TAIL.sub("", raw.rstrip("…")).rstrip() or raw
    t = local.apply(redact_text_wide(evidence.redact_plain_text(raw)))
    t = " ".join(t.split())
    if len(t) > n:
        return t[: n - 1] + "…"
    if cut:
        return (t[: n - 1] if len(t) >= n else t) + "…"
    return t or None


def _base_record(call: ToolCall, local: _Local, called_at: str) -> dict:
    source = dict(call.source) if isinstance(call.source, dict) else builtin_source(call.runtime)
    source["runtime"] = call.runtime
    if source.get("kind") == SOURCE_MCP:
        source["server"] = _clip_scrubbed(source.get("server"), 128, local) or "unknown_server"
    else:
        source.pop("server", None)
    tool_name = _clip_scrubbed(call.tool_name, MAX_TOOL_NAME, local) or ""
    tool = _clip_scrubbed(call.tool, MAX_TOOL_NAME, local) or tool_name or "unknown_tool"
    rec = {
        "schema": SCHEMA_V2,
        "evidence_id": "",
        "tier": evidence.TIER,
        "session_id": call.session_id if isinstance(call.session_id, str)
        and evidence.SESSION_ID_RE.match(call.session_id) else None,
        "source": source,
        "server": wire_source_server(source),
        "tool": tool,
        "tool_name": tool_name,
        "client": _clip_scrubbed(call.client, 64, local) or call.runtime,
        "called_at": called_at,
    }
    if isinstance(call.tool_use_id, str) and 0 < len(call.tool_use_id) <= 256:
        rec["tool_use_id"] = call.tool_use_id
    return rec


def build_record(call: ToolCall, *, home: Optional[str] = None, withheld: Optional[gate.Withheld] = None) -> dict:
    """The v2 spool entry for one call (scrubbed, capped), or its withheld
    stub (no args, no result, no summary)."""
    local = _Local(call.spill_root, call.cwd, home)
    called_at = call.called_at or evidence.now_iso()
    rec = _base_record(call, local, called_at)
    failed = call.error is not None
    if withheld is not None:
        rec.update({"status": "error" if failed else "ok", "args": None, "result": None, "truncated": False,
                    "withheld": withheld.as_dict()})
        if failed:
            rec["is_error"] = True
    else:
        norm, nid = normalize_call(call)
        if not isinstance(norm, Normalized):  # pragma: no cover - normalize_call guarantees it
            norm = Normalized(text=[""])
        args_in = call.tool_input if call.tool_input is not None else {}
        args, args_truncated = redact_and_cap(evidence.well_formed(args_in), evidence.MAX_ARGS_BYTES, local, home,
                                              call.cwd)
        body: dict = {}
        if norm.has_structured or norm.structured is not None:
            body["structured"] = norm.structured
        if norm.text:
            body["text"] = [t if isinstance(t, str) else str(t) for t in norm.text]
        if norm.other_blocks:
            body["other_blocks"] = int(norm.other_blocks)
        result, truncated = redact_and_cap(evidence.well_formed(body), evidence.MAX_RESULT_BYTES, local, home,
                                           call.cwd)
        is_error = failed or bool(norm.is_error)
        rec.update({
            "status": "error" if is_error else "ok",
            "normalizer": nid,
            "args": args,
            "result": result,
            "truncated": truncated,
        })
        if norm.spilled_bytes is not None:
            rec["spilled"] = True
            rec["spilled_bytes"] = int(norm.spilled_bytes)
        if args_truncated:
            rec["args_truncated"] = True
        if is_error:
            rec["is_error"] = True
        if isinstance(norm.exit_code, int) and not isinstance(norm.exit_code, bool):
            rec["exit_code"] = norm.exit_code
        summary = _clip_scrubbed(norm.summary, MAX_SUMMARY_CHARS, local)
        if summary:
            rec["summary"] = summary
    ev_id = evidence_id_v2(call.runtime, call.session_id, call.tool_use_id)
    rec["evidence_id"] = ev_id or _random_id(rec["server"], rec["tool"], called_at)
    return rec


def head_field(head: bytes, name: str, rx: str = r"[^\"\\]{1,256}") -> Optional[str]:
    """A top-level string field read from a payload's first 64 KiB without
    parsing it (a payload too large or too deep to parse)."""
    text = head[: 64 << 10].decode("utf-8", errors="ignore")
    m = re.search(r'"' + re.escape(name) + r'"\s*:\s*"(' + rx + r')"', text)
    return m.group(1) if m else None


def unreadable_record(runtime: str, client: str, head: bytes, spill_root: Optional[str] = None, *,
                      rule: str = "size", hint: Optional[str] = None) -> Optional[dict]:
    """A payload that cannot be read (too large: rule "size"; nested deeper
    than the parser goes: rule "depth"): a withheld stub (reason
    "unreadable"), with the session and tool pulled from the payload's head
    when they are there. None when not even the tool name is readable."""
    tool_name = head_field(head, "tool_name")
    if not tool_name:
        return None
    source, tool = classify_mcp_name(tool_name, runtime)
    call = ToolCall(runtime=runtime, tool_name=tool_name, source=source, tool=tool,
                    session_id=head_field(head, "session_id", r"[A-Za-z0-9_-]{1,128}"),
                    tool_use_id=head_field(head, "tool_use_id", r"[A-Za-z0-9_.:-]{1,256}"), client=client,
                    spill_root=spill_root)
    return build_record(call, withheld=gate.Withheld(gate.REASON_UNREADABLE, rule,
                                                     hint or f"> {MAX_STDIN_BYTES >> 20} MiB"))


# ---------------------------------------------------------------------------
# Context lines
# ---------------------------------------------------------------------------

def context_enabled(env: Optional[dict] = None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(CONTEXT_ENV, "")).strip().lower() not in ("0", "false", "off", "no")


def _first_in_session(root: Path, session_id: Any) -> bool:
    """True once per session: creates <session>/.hinted."""
    try:
        sdir = Path(root) / evidence.session_dir_name(session_id)
        fd = os.open(str(sdir / HINTED), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return False


def context_line(entry: dict, first: bool, promote_cmd: str = "cardinal-evidence") -> str:
    ev_id = entry["evidence_id"]
    w = entry.get("withheld")
    if isinstance(w, dict):
        text = gate.describe(gate.Withheld(str(w.get("reason")), str(w.get("rule")), str(w.get("hint"))))
        line = f"[evidence:{ev_id} withheld: {text}]"
        if first:
            line += (" Nothing from this call was kept, so it cannot be cited; say so plainly instead of "
                     "paraphrasing its result.")
        return line
    if not first:
        return f"[evidence:{ev_id}]"
    return (f"[evidence:{ev_id}] Cardinal kept this result on this machine. Every tool result in this session gets "
            f"an id like this and can be cited in a storyboard: `{promote_cmd} promote ev_...` uploads only what "
            f"you promote (only what a scene cites; a repeat prints the receipt it already has); "
            f"`{promote_cmd} find <text>` looks an id up.")


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

# A command line that runs the investigation control-log CLI (any path to
# it, any shell separator before it).
_CONTROL_PLANE_RE = re.compile(r"(?:^|[\s/;&|(`'\"])cardinal-storyboard[\s'\"]+investigation(?:[\s'\"]|$)")


def control_plane(tool_input: Any, _depth: int = 0) -> bool:
    """Whether any string in a tool call's input runs `cardinal-storyboard
    investigation …`: the investigation's control log (events, acks, the
    question, links) is never evidence, so such a call is never captured.
    Tool-neutral, like the sensitivity gate."""
    if _depth > 20:
        return False
    if isinstance(tool_input, str):
        return "cardinal-storyboard" in tool_input and bool(_CONTROL_PLANE_RE.search(tool_input))
    if isinstance(tool_input, dict):
        return any(control_plane(v, _depth + 1) for v in tool_input.values())
    if isinstance(tool_input, list):
        return any(control_plane(v, _depth + 1) for v in tool_input)
    return False


def capture_call(call: ToolCall, home: Path, *, env: Optional[dict] = None, rules: Optional[gate.Rules] = None,
                 promote_cmd: str = "cardinal-evidence", write: bool = True) -> Optional[Captured]:
    """Record one tool call. None when capture is off or the call is
    Cardinal's own (already witnessed). Otherwise the written entry (or its
    withheld stub) and the context line for the agent (None when
    CARDINAL_EVIDENCE_CONTEXT=0). A failed write raises: hooks call this
    inside their fail-open guard."""
    env = os.environ if env is None else env
    if not isinstance(call, ToolCall) or not _RUNTIME_RE.match(call.runtime or ""):
        return None
    root = evidence.default_root(home)
    if evidence.capture_disabled(root, env):
        return None
    if isinstance(call.source, dict) and call.source.get("kind") == SOURCE_CARDINAL:
        return None
    if control_plane(call.tool_input):
        return None
    home_s = str(home) if home else None
    if rules is None:
        rules = gate.load_rules(Path(home) if home else None, call.cwd)
    verdict = gate.check(call.tool_name, call.tool_input, cwd=call.cwd, home=home_s, rules=rules)
    entry = build_record(call, home=home_s, withheld=verdict)
    if not write:
        return Captured(entry, None, verdict is not None)
    evidence.write_entry(root, entry)
    # The entry is on disk: from here nothing (not even the budget timer,
    # see capture_call_guarded) may turn it into a failed capture.
    try:
        evidence.gc(root)
    except BaseException:
        pass
    line = None
    try:
        if context_enabled(env):
            line = context_line(entry, _first_in_session(root, entry.get("session_id")), promote_cmd)
    except BaseException:
        line = f"[evidence:{entry['evidence_id']}]"
    return Captured(entry, line, verdict is not None)


# What a call that could not be processed in time is recorded as.
FALLBACK_BUDGET_S = 0.3


def capture_call_guarded(call: ToolCall, home: Path, *, budget_s: float = TIME_BUDGET_S, env: Optional[dict] = None,
                         rules: Optional[gate.Rules] = None, promote_cmd: str = "cardinal-evidence",
                         ) -> Optional[Captured]:
    """capture_call under a wall-clock budget, for hooks. Never raises.

    Whatever the tool and whatever its input or result, the call is not
    lost: when the pipeline runs out of budget (a pathological input the
    gate or the scrub cannot finish in time) or fails, the call is recorded
    as a withheld stub (reason "unreadable", rule "budget" or "error"; no
    arguments, no result), so the agent still gets an id and a reason
    instead of silence. Fail closed: nothing unchecked is ever kept."""
    rule = hint = None
    try:
        with time_guard(budget_s):
            return capture_call(call, home, env=env, rules=rules, promote_cmd=promote_cmd)
    except BudgetExceeded:
        rule, hint = "budget", "too large to check in time"
    except Exception:
        rule, hint = "error", "could not be processed"
    except BaseException:
        return None
    try:
        with time_guard(FALLBACK_BUDGET_S):
            return write_stub(call, home, gate.Withheld(gate.REASON_UNREADABLE, rule, hint), env, promote_cmd)
    except BaseException:
        return None


def write_stub(call: ToolCall, home: Path, withheld: "gate.Withheld", env: Optional[dict] = None,
               promote_cmd: str = "cardinal-evidence") -> Optional[Captured]:
    """Record a call as a withheld stub only (no arguments, no result): for
    a call whose content cannot be checked (too large to send or read, or
    the pipeline could not finish). Same opt-outs and skips as
    capture_call. A deterministic id already on disk is left alone."""
    env = os.environ if env is None else env
    if not isinstance(call, ToolCall) or not _RUNTIME_RE.match(call.runtime or ""):
        return None
    root = evidence.default_root(home)
    if evidence.capture_disabled(root, env):
        return None
    if isinstance(call.source, dict) and call.source.get("kind") == SOURCE_CARDINAL:
        return None
    entry = build_record(call, home=str(home) if home else None, withheld=withheld)
    path = evidence.entry_path(root, entry.get("session_id"), entry["evidence_id"])
    if os.path.lexists(str(path)):
        # A deterministic id already on disk is the full entry, written just
        # before the budget ran out: keep it.
        return Captured(entry, f"[evidence:{entry['evidence_id']}]" if context_enabled(env) else None, False)
    evidence.write_entry(root, entry)
    line = None
    if context_enabled(env):
        line = context_line(entry, False, promote_cmd)
    return Captured(entry, line, True)


# ---------------------------------------------------------------------------
# Hook budget helpers
# ---------------------------------------------------------------------------

class BudgetExceeded(BaseException):
    """Raised by time_guard's timer; a hook's fail-open guard swallows it."""


@contextmanager
def time_guard(seconds: float = TIME_BUDGET_S):
    """Abort the block after `seconds` of wall time (SIGALRM/setitimer, main
    thread, POSIX). A write it interrupts leaves only a temp file, which gc
    reaps. A no-op where timers are unavailable."""
    armed = False
    old = None
    try:
        if hasattr(signal, "setitimer") and seconds > 0:
            def _fire(signum, frame):  # noqa: ARG001
                raise BudgetExceeded()
            old = signal.signal(signal.SIGALRM, _fire)
            signal.setitimer(signal.ITIMER_REAL, seconds)
            armed = True
    except (ValueError, OSError):
        armed = False
    try:
        yield
    finally:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            try:
                signal.signal(signal.SIGALRM, old if old is not None else signal.SIG_DFL)
            except (ValueError, OSError):
                pass


def read_stdin_bounded(limit: int = MAX_STDIN_BYTES) -> tuple:
    """-> (bytes, complete). Reads at most limit + 1 bytes of stdin."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        data = sys.stdin.read(limit + 1).encode("utf-8", errors="ignore")
    else:
        data = stream.read(limit + 1)
    if len(data) > limit:
        return data[:limit], False
    return data, True
