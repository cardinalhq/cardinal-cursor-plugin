"""cardinal-evidence: find, inspect and promote locally captured evidence.

Shared by every adapter. The capture hooks keep each tool call's result on
this machine (cardinal_core.evidence_capture). Nothing is uploaded until the
author promotes it: `promote` sends the entries it names to a draft
storyboard, and Cardinal stores each as a *captured* receipt (rcpt_...) that
scenes bind like any other receipt.

Commands (main()):
  promote [--storyboard sb_...] [--force] ev_... [ev_...]
      POST <maestro>/api/orgs/<org>/storyboards/<id>/evidence
      {items: [{provenance: "captured", source_server, client,
      client_called_at, tool, args, result}]}. source_server is the MCP
      server, "builtin:<runtime>" for the agent's own tools or
      "tool:<runtime>" where the runtime cannot tell. A withheld entry is
      refused locally and never sent. An entry already promoted into the
      same storyboard (the session's promoted.json ledger) is not sent
      again: its existing receipt is printed ("already promoted"), unless
      --force.
  list [--session ID | --all] [--limit N] [--tool GLOB] [--grep TEXT] [--withheld]
  find TEXT                  list --grep TEXT
  show ev_...                the scrubbed entry exactly as promote would cite it
  off | on | status

Each adapter supplies a PromoteAdapter: its client string, how to find the
Cardinal connection (URL + key), which environment variables name the
current session, and how to tell the user to reconnect.

Credentials, in order: the storyboard's evidence token (stored per session by
the Claude adapter's storyboard-token hook; 24 h; this storyboard's evidence
upload only) as `Authorization: CardinalEvidence <token>`, then the Cardinal
MCP key as X-CardinalHQ-API-Key, only to the connection's origin, never
across a redirect.

Exit codes: 0 every entry promoted (or the command succeeded); 1 some entries
failed (each is listed); 2 nothing was uploaded.
"""

from __future__ import annotations

import argparse
import fnmatch
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import evidence
from . import evidence_capture as capture
from . import evidence_gate as gate

EXIT_OK = 0
EXIT_ITEM_ERRORS = 1
EXIT_FAILED = 2

# conductor storyboard/evidence-ingest.ts MAX_EVIDENCE_ITEMS.
MAX_ITEMS_PER_REQUEST = 50
# maestro parses an upload up to STORYBOARD_EVIDENCE_BODY_LIMIT ("8mb");
# larger batches are split.
MAX_REQUEST_BYTES = 7 << 20
MAX_429_RETRIES = 3
REQUEST_TIMEOUT_S = 60
MAX_MESSAGE_CHARS = 300
MAX_OTHER_BLOCKS = 1000
MAX_SHOW_BYTES = 24 << 10
# maestro evidence-upload.ts ident lengths.
MAX_SOURCE_SERVER = 128
MAX_TOOL = 200
MAX_CLIENT = 64

DEFAULT_MCP_URL = "https://app.cardinalhq.io/mcp"
MCP_URL_PATH_RE = re.compile(r"^/api/orgs/([^/?#]+)/mcp(?:/|$)")
RECEIPT_ID_RE = evidence.RECEIPT_ID_RE
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


class PromoteError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.code = code


class WithheldEntry(ValueError):
    """The entry is a withheld stub: nothing of the call was kept."""


def _no_settings(home: Path) -> dict:  # noqa: ARG001
    return {}


@dataclass
class PromoteAdapter:
    runtime: str                                   # "claude-code"
    client: str                                    # "claude-code/0.36.0"
    connection: Callable[[Path, dict], dict]       # -> {origin, org, key} or {}
    session_env: tuple = ()                        # env vars naming the current session
    settings_env: Callable[[Path], dict] = field(default=_no_settings)
    settings_label: str = "the agent settings"
    connect_hint: str = "cardinal-connect"
    reconnect_hint: str = "cardinal-connect --rotate"
    prog: str = "cardinal-evidence"


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def origin_of(url: str) -> Optional[str]:
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    default = 443 if parts.scheme == "https" else 80
    host = parts.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


def connection_for(url: Any, key: Any) -> dict:
    """{origin, org, key} for an MCP URL and key; {} when the URL is not a
    usable http(s) URL."""
    if not (isinstance(url, str) and url):
        return {}
    origin = origin_of(url)
    if origin is None:
        return {}
    m = MCP_URL_PATH_RE.match(urllib.parse.urlsplit(url).path)
    return {"origin": origin, "org": urllib.parse.unquote(m.group(1)) if m else None,
            "key": key if isinstance(key, str) and key else None}


def agent_paths_connection(paths) -> Callable[[Path, dict], dict]:
    """A connection lookup over an adapter's managed files (AgentPaths:
    cardinal.json mcp_url + cardinal-secrets.json mcp_api_key), with
    CARDINAL_MCP_URL / CARDINAL_MCP_API_KEY in the environment as a
    fallback."""
    def lookup(home: Path, environ: dict) -> dict:  # noqa: ARG001
        state, secrets = paths.read_state(), paths.read_secrets()
        url, key = state.get("mcp_url"), secrets.get("mcp_api_key")
        if not (isinstance(url, str) and url):
            url, key = environ.get("CARDINAL_MCP_URL"), environ.get("CARDINAL_MCP_API_KEY")
        return connection_for(url, key)
    return lookup


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib re-sends custom headers on a redirect: a credential must never
    follow one."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def no_redirect_opener():
    return urllib.request.build_opener(_NoRedirect())


def _error_body(err: urllib.error.HTTPError) -> dict:
    try:
        body = json.loads(err.read(64 * 1024) or b"{}")
        return body if isinstance(body, dict) else {}
    except (ValueError, OSError):
        return {}


def home_dir() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home()))


def clip(text, n: int = MAX_MESSAGE_CHARS) -> str:
    """Server- or entry-supplied text, one line, bounded."""
    s = _CONTROL_RE.sub(" ", str(text or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------------------
# Wire mapping
# ---------------------------------------------------------------------------

def wire_result(result) -> dict:
    """A spool entry's result ({structured?, text?, other_blocks?}, or the
    256 KiB truncation envelope) -> the upload's result ({content?,
    structured_content?}). A truncated result is uploaded as its envelope
    ({truncated, original_bytes, prefix}) so the receipt says it was cut."""
    if not isinstance(result, dict):
        raise ValueError("the entry has no result")
    if result.get("truncated") is True and "prefix" in result:
        return {"structured_content": result}
    out: dict = {}
    content = []
    texts = result.get("text")
    if isinstance(texts, list):
        content.extend({"type": "text", "text": t} for t in texts if isinstance(t, str))
    other = result.get("other_blocks")
    if isinstance(other, int) and not isinstance(other, bool) and other > 0:
        # Non-text blocks are counted, never stored (gateway external.go):
        # one untyped placeholder per block the client saw keeps the count.
        content.extend({"type": "other"} for _ in range(min(other, MAX_OTHER_BLOCKS)))
    if content:
        out["content"] = content
    if result.get("structured") is not None:
        out["structured_content"] = result["structured"]
    if not out:
        # An empty result is still what the tool returned.
        out["text"] = ""
    return out


def is_withheld(entry: dict) -> bool:
    return isinstance(entry.get("withheld"), dict)


def withheld_reason(entry: dict) -> str:
    w = entry.get("withheld") or {}
    return f"withheld: {clip(w.get('reason'), 40)} ({clip(w.get('rule'), 60)})"


def wire_client(entry: dict, fallback: str) -> str:
    c = entry.get("client")
    if isinstance(c, str) and len(c) <= MAX_CLIENT and capture.IDENT_RE.match(c):
        return c
    return fallback


def wire_item(entry: dict, client: str) -> dict:
    """One spool entry (v1 or v2) -> one upload item (conductor
    EvidenceItemSchema). A failed call is uploaded with result.is_error. A
    withheld stub raises WithheldEntry: nothing of it was kept, and it is
    never sent."""
    if is_withheld(entry):
        raise WithheldEntry(withheld_reason(entry))
    server, tool = entry.get("server"), entry.get("tool")
    if not isinstance(server, str) or not server or not isinstance(tool, str) or not tool:
        raise ValueError("the entry names no server or tool")
    result = wire_result(entry.get("result"))
    if entry.get("is_error") is True or entry.get("status") == "error":
        result["is_error"] = True
    item = {
        "provenance": "captured",
        "source_server": capture.wire_source_server(entry.get("source"), server),
        "client": wire_client(entry, client),
        "tool": capture.ident(tool, MAX_TOOL, "unknown_tool"),
        "result": result,
    }
    if evidence.parse_time(entry.get("called_at")) is not None:
        item["client_called_at"] = entry["called_at"]
    args = entry.get("args")
    if isinstance(args, dict):
        item["args"] = args
    elif args is not None:
        item["args"] = {"input": args}
    return item


def batches(items: list) -> list:
    """[(ev_id, item, encoded)] -> lists of at most MAX_ITEMS_PER_REQUEST
    items whose request body stays under MAX_REQUEST_BYTES."""
    out, cur, size = [], [], 16
    for row in items:
        n = len(row[2]) + 1
        if cur and (len(cur) >= MAX_ITEMS_PER_REQUEST or size + n > MAX_REQUEST_BYTES):
            out.append(cur)
            cur, size = [], 16
        cur.append(row)
        size += n
    if cur:
        out.append(cur)
    return out


CONTROL_LOG_NOT_EVIDENCE = "control_log_not_evidence"
CONTROL_LOG_MESSAGE = ("an investigation's control log (its events, and `cardinal-storyboard investigation` calls) "
                       "is never evidence and never public: it cannot be cited in a storyboard")


def explain_status(status: int, body: dict, org: str, storyboard_id: str, auth: str,
                   adapter: Optional[PromoteAdapter] = None) -> str:
    connect = adapter.connect_hint if adapter else "cardinal-connect"
    reconnect = adapter.reconnect_hint if adapter else "cardinal-connect --rotate"
    code = str(body.get("error") or "")
    msg = clip(body.get("message"))
    if code == CONTROL_LOG_NOT_EVIDENCE:
        return CONTROL_LOG_MESSAGE
    if status == 400:
        return f"maestro rejected the upload ({code or 'bad request'}): {msg or 'upgrade the plugin'}"
    if status == 401:
        if auth == "token":
            return ("the storyboard's evidence token was rejected (expired or revoked): call storyboard__preview "
                    f"for a fresh one, or connect with {connect}")
        return f"the Cardinal MCP key was rejected: reconnect with {reconnect}"
    if status == 403:
        if code == "token_scope_mismatch":
            return "the evidence token is for a different storyboard or org"
        if code == "org_scope_mismatch":
            return (f"storyboard {storyboard_id} is in org {org}, which this key cannot reach: "
                    f"reconnect to that org ({reconnect})")
        if code == "insufficient_scope":
            return ("this Cardinal does not let the plugin key upload evidence: upgrade Cardinal, or promote "
                    "right after storyboard__create so its evidence token is used")
        return "forbidden: adding evidence needs the Member role in this org"
    if status == 404:
        if code == "storyboard_not_found":
            return f"storyboard {storyboard_id} not found in org {org} (deleted, or in another org)"
        return ("this Cardinal has no evidence upload route (it predates captured evidence): upgrade Cardinal, "
                "or report the result with storyboard__record_evidence")
    if status == 409:
        return ("storyboard " + storyboard_id + " is published, and a published storyboard is immutable: "
                "evidence can be added only to a draft")
    if status == 413:
        return "the upload is too large for this Cardinal: promote fewer entries at a time"
    if status == 429:
        return "Cardinal is rate-limiting evidence uploads for this org: wait a minute and promote again"
    if status in (501, 503):
        return "this Cardinal cannot store evidence uploads (" + (msg or code or f"HTTP {status}") + ")"
    if status == 502:
        return "Cardinal could not store the evidence: promote again"
    return f"maestro answered HTTP {status}" + (f" ({code})" if code else "") + (f": {msg}" if msg else "")


def post_upload(conn: dict, org: str, storyboard_id: str, body: bytes, auth: tuple,
                opener=None, sleep=time.sleep, adapter: Optional[PromoteAdapter] = None) -> dict:
    """POST one upload; the parsed 200 body. Retries 429 (Retry-After, at
    most MAX_429_RETRIES times); never follows a redirect."""
    kind, headers = auth
    path = f"/api/orgs/{urllib.parse.quote(org, safe='')}/storyboards/{storyboard_id}/evidence"
    url = conn["origin"] + path
    opener = opener or no_redirect_opener()
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=body, method="POST", headers={
            **headers,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            with opener.open(req, timeout=REQUEST_TIMEOUT_S) as resp:
                raw = resp.read(8 << 20)
            try:
                out = json.loads(raw or b"{}")
            except ValueError:
                raise PromoteError("maestro answered with something other than JSON: upgrade Cardinal")
            if not isinstance(out, dict) or not isinstance(out.get("results"), list):
                raise PromoteError("maestro's answer has no per-item results: upgrade Cardinal")
            return out
        except urllib.error.HTTPError as err:
            err_body = _error_body(err)
            err.close()
            if err.code == 429 and attempt < MAX_429_RETRIES:
                attempt += 1
                try:
                    wait = int(err.headers.get("Retry-After") or 5)
                except (TypeError, ValueError):
                    wait = 5
                sleep(max(1, min(wait, 30)))
                continue
            if 300 <= err.code < 400:
                raise PromoteError(f"maestro redirected the upload (HTTP {err.code}); not following it with your "
                                   "credentials", status=err.code)
            raise PromoteError(explain_status(err.code, err_body, org, storyboard_id, kind, adapter),
                               status=err.code, code=str(err_body.get("error") or "") or None)
        except (urllib.error.URLError, OSError) as err:
            raise PromoteError(f"could not reach {conn['origin']}: {clip(getattr(err, 'reason', err))}")
        except http.client.HTTPException as err:
            raise PromoteError(f"could not read maestro's answer from {conn['origin']}: {type(err).__name__}")


def pick_storyboard(root: Path, entries: dict) -> tuple:
    """(storyboard_id, None) for the one storyboard with a stored token in
    the entries' sessions, else (None, why)."""
    sessions = sorted({e.get("session_id") or evidence.NO_SESSION for e in entries.values()})
    found = sorted({sb for s in sessions for (_, sb, _) in evidence.find_tokens(root, session_id=s)})
    if len(found) == 1:
        return found[0], None
    if not found:
        return None, ("no storyboard to promote into: pass --storyboard sb_... (the id storyboard__create "
                      "returned)")
    return None, "more than one storyboard in this session: pass --storyboard (one of " + ", ".join(found) + ")"


def cmd_promote(args, adapter: PromoteAdapter, out=sys.stdout, err=sys.stderr, opener=None, sleep=time.sleep,
                now=None) -> int:
    home = home_dir()
    root = evidence.default_root(home)
    prog = adapter.prog

    ids = []
    for raw in args.evidence_ids:
        if not evidence.EVIDENCE_ID_RE.match(raw):
            err.write(f"{prog}: not an evidence id: {clip(raw, 80)!r} (expected ev_ + 12 hex)\n")
            return EXIT_FAILED
        if raw not in ids:
            ids.append(raw)
    sb = args.storyboard
    if sb is not None and not evidence.STORYBOARD_ID_RE.match(sb):
        err.write(f"{prog}: not a storyboard id: {clip(sb, 80)!r} (expected sb_ + 24 hex)\n")
        return EXIT_FAILED

    results: dict = {}
    entries: dict = {}
    for ev_id in ids:
        entry = evidence.read_entry(root, ev_id)
        if entry is None or entry.get("evidence_id") != ev_id:
            results[ev_id] = ("error", "not_in_spool", "no such entry on this machine (captured elsewhere, "
                              "removed after 14 days, or capture was off)")
        elif is_withheld(entry):
            w = entry.get("withheld") or {}
            results[ev_id] = ("error", "withheld",
                              f"{clip(w.get('reason'), 40)} ({clip(w.get('rule'), 60)}): nothing from this call was "
                              "kept on this machine, so it cannot be cited; say so in the storyboard instead")
        else:
            entries[ev_id] = entry

    if not entries:
        return report(ids, results, out, err)
    if sb is None:
        sb, why = pick_storyboard(root, entries)
        if sb is None:
            err.write(f"{prog}: " + why + "\n")
            return EXIT_FAILED

    # Already promoted into this storyboard: print the receipt it got, send nothing.
    reused = 0
    if not getattr(args, "force", False):
        for ev_id in list(entries):
            prior = evidence.promoted_receipt(root, entries[ev_id].get("session_id"), sb, ev_id, now)
            if prior:
                results[ev_id] = ("ok", prior, f"{summary_of(entries.pop(ev_id))}, captured, already promoted")
                reused += 1
    if not entries:
        return report(ids, results, out, err, sb, reused)

    conn = adapter.connection(home, dict(os.environ))
    if not conn:
        err.write(f"{prog}: not connected to Cardinal (no usable Cardinal MCP URL): run "
                  f"{adapter.connect_hint}\n")
        return EXIT_FAILED

    token = None
    for _, _, rec in evidence.find_tokens(root, sb):
        if evidence.token_live(rec, now):
            token = rec
            break
    org = token["org"] if token else conn.get("org")
    if token and conn.get("org") and conn["org"] != token["org"]:
        err.write(f"{prog}: storyboard {sb} was created in org {token['org']}, but this agent is "
                  f"connected to org {conn['org']}: reconnect to that org ({adapter.reconnect_hint})\n")
        return EXIT_FAILED
    if not org:
        err.write(f"{prog}: no org for storyboard {sb}: the Cardinal MCP URL names none and no evidence "
                  "token was stored for it (call storyboard__preview to get a fresh one)\n")
        return EXIT_FAILED
    auths = []
    if token:
        auths.append(("token", {"Authorization": "CardinalEvidence " + token["evidence_token"]}))
    if conn.get("key"):
        auths.append(("key", {"X-CardinalHQ-API-Key": conn["key"]}))
    if not auths:
        err.write(f"{prog}: no credential for storyboard {sb}: no live evidence token was stored "
                  f"for it (call storyboard__preview for a fresh one) and there is no Cardinal MCP key "
                  f"(run {adapter.connect_hint})\n")
        return EXIT_FAILED

    rows = []
    for ev_id in ids:
        if ev_id not in entries:
            continue
        try:
            item = wire_item(entries[ev_id], adapter.client)
            encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except WithheldEntry as e:
            results[ev_id] = ("error", "withheld", str(e))
            continue
        except (ValueError, TypeError) as e:
            results[ev_id] = ("error", "invalid_entry", str(e))
            continue
        if len(encoded) + 16 > MAX_REQUEST_BYTES:
            results[ev_id] = ("error", "entry_too_large", "the entry is larger than one upload")
            continue
        rows.append((ev_id, item, encoded))

    planned = batches(rows)
    ledger: dict = {}
    for bi, batch in enumerate(planned):
        body = b'{"items":[' + b",".join(r[2] for r in batch) + b"]}"
        answer = None
        failure = None
        while auths:
            try:
                answer = post_upload(conn, org, sb, body, auths[0], opener=opener, sleep=sleep, adapter=adapter)
                break
            except PromoteError as e:
                failure = e
                # A rejected token falls back to the key, for this and every later batch.
                if auths[0][0] == "token" and e.status in (401, 403) and len(auths) > 1:
                    auths.pop(0)
                    continue
                break
        if answer is None:
            # A whole-request refusal (auth, org, a published storyboard) would
            # refuse every later batch the same way: stop here.
            err.write(f"{prog}: upload to {sb} failed: {failure}\n")
            for rest in planned[bi:]:
                for ev_id, _, _ in rest:
                    results[ev_id] = ("error", "not_uploaded", str(failure))
            break
        by_index = {}
        for r in answer["results"]:
            if isinstance(r, dict) and isinstance(r.get("index"), int):
                by_index[r["index"]] = r
        for i, (ev_id, item, _) in enumerate(batch):
            r = by_index.get(i)
            if r is None:
                results[ev_id] = ("error", "no_result", "maestro returned no result for this entry")
            elif isinstance(r.get("receipt_id"), str) and RECEIPT_ID_RE.match(r["receipt_id"]):
                results[ev_id] = ("ok", r["receipt_id"], f"{item['source_server']}/{item['tool']}, captured")
                ledger.setdefault(entries[ev_id].get("session_id"), {})[ev_id] = r["receipt_id"]
            else:
                e = r.get("error") if isinstance(r.get("error"), dict) else {}
                if e.get("code") == CONTROL_LOG_NOT_EVIDENCE:
                    results[ev_id] = ("error", CONTROL_LOG_NOT_EVIDENCE, CONTROL_LOG_MESSAGE)
                    continue
                results[ev_id] = ("error", clip(e.get("code") or "rejected", 60), clip(e.get("message")))
    for session, receipts in ledger.items():
        try:
            evidence.record_promoted(root, session, sb, receipts, now)
        except (OSError, ValueError) as e:
            # The receipts are minted and printed; only the reuse is lost.
            err.write(f"{prog}: could not record the promotion locally ({type(e).__name__}); "
                      "promoting these entries again would upload them again\n")
    return report(ids, results, out, err, sb, reused)


def summary_of(entry: dict) -> str:
    return f"{clip(entry.get('server'), 80)}/{clip(entry.get('tool'), 120)}"


def report(ids: list, results: dict, out, err, sb: Optional[str] = None, reused: int = 0) -> int:
    ok = 0
    for ev_id in ids:
        kind, a, b = results.get(ev_id, ("error", "not_uploaded", ""))
        if kind == "ok":
            ok += 1
            out.write(f"{ev_id} -> {a}  ({clip(b, 200)})\n")
        else:
            out.write(f"{ev_id}: error {a}: {b}\n")
    failed = len(ids) - ok
    if ok:
        out.write(f"{ok} promoted" + (f" to {sb}" if sb else "") +
                  (f" ({reused} already, receipt reused)" if reused else "") +
                  (f", {failed} failed" if failed else "") +
                  ". Bind each rcpt_ id like any receipt; the storyboard labels it captured.\n")
    if ok == 0:
        return EXIT_FAILED
    return EXIT_ITEM_ERRORS if failed else EXIT_OK


# ---------------------------------------------------------------------------
# list / find / show / off / on / status
# ---------------------------------------------------------------------------

def current_session(root: Path, environ: dict, adapter: PromoteAdapter):
    for name in adapter.session_env:
        value = environ.get(name)
        if isinstance(value, str) and evidence.SESSION_ID_RE.match(value) and (root / value).is_dir():
            return value
    dirs = evidence.session_dirs(root)
    return dirs[0][0] if dirs else None


def _size(entry: dict) -> str:
    try:
        n = len(json.dumps(entry.get("result"), ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return "?"
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KiB"


def _matches(entry: dict, tool_glob: Optional[str], grep: Optional[str], only_withheld: bool) -> bool:
    if only_withheld and not is_withheld(entry):
        return False
    if tool_glob:
        names = [n for n in (entry.get("tool"), entry.get("tool_name")) if isinstance(n, str)]
        if not any(fnmatch.fnmatchcase(n, tool_glob) or fnmatch.fnmatchcase(n.lower(), tool_glob.lower())
                   for n in names):
            return False
    if grep:
        try:
            blob = json.dumps({k: entry.get(k) for k in ("tool", "tool_name", "summary", "args", "result")},
                              ensure_ascii=False)
        except (TypeError, ValueError):
            return False
        if grep.lower() not in blob.lower():
            return False
    return True


def entry_line(e: dict) -> str:
    line = (f"  {e['evidence_id']}  {clip(e.get('called_at'), 40)}  "
            f"{clip(e.get('server'), 80)}/{clip(e.get('tool'), 120)}")
    if is_withheld(e):
        w = e.get("withheld") or {}
        return line + f"  WITHHELD {clip(w.get('reason'), 40)} ({clip(w.get('hint'), 60)})"
    line += f"  {_size(e)}"
    if e.get("status") == "error" or e.get("is_error") is True:
        line += "  error"
    if isinstance(e.get("exit_code"), int):
        line += f"  exit {e['exit_code']}"
    if e.get("truncated"):
        line += "  truncated"
    if isinstance(e.get("summary"), str) and e["summary"]:
        line += "  " + clip(e["summary"], 100)
    return line


def cmd_list(args, adapter: PromoteAdapter, out=sys.stdout, err=sys.stderr) -> int:
    root = evidence.default_root(home_dir())
    if getattr(args, "all", False):
        sessions = [name for name, _ in evidence.session_dirs(root)]
    elif getattr(args, "session", None):
        if not evidence.SESSION_ID_RE.match(args.session):
            err.write(f"{adapter.prog}: not a session id\n")
            return EXIT_FAILED
        sessions = [args.session]
    else:
        s = current_session(root, os.environ, adapter)
        sessions = [s] if s else []
    if not sessions:
        out.write(f"No captured evidence in {root}.\n")
        return EXIT_OK
    tool_glob, grep = getattr(args, "tool", None), getattr(args, "grep", None)
    only_withheld = bool(getattr(args, "withheld", False))
    filtered = bool(tool_glob or grep or only_withheld)
    for s in sessions:
        entries = evidence.list_entries(root, s)
        matching = [e for e in entries if _matches(e, tool_glob, grep, only_withheld)]
        limit = args.limit
        shown = matching[-limit:] if limit > 0 else matching
        head = f"session {s}: {len(entries)} captured"
        if filtered:
            head += f", {len(matching)} matching"
        if len(shown) < len(matching):
            head += f" (newest {len(shown)} shown)"
        out.write(head + "\n")
        for e in shown:
            out.write(entry_line(e) + "\n")
        for sb, rec in sorted(evidence.read_tokens(root, s).items()):
            state = "live" if evidence.token_live(rec) else "expired"
            out.write(f"  evidence token for {sb} (org {rec['org']}, {state}"
                      + (f", expires {rec['expires_at']}" if rec.get("expires_at") else "") + ")\n")
    return EXIT_OK


def cmd_find(args, adapter: PromoteAdapter, out=sys.stdout, err=sys.stderr) -> int:
    ns = argparse.Namespace(all=args.all, session=args.session, limit=args.limit, tool=args.tool,
                            grep=args.text, withheld=False)
    return cmd_list(ns, adapter, out, err)


def _outline(v: Any, depth: int = 0) -> list:
    """Top-level shape of a large value: key -> size, for `show`."""
    lines = []
    if isinstance(v, dict) and depth < 2:
        for k in sorted(v, key=str)[:64]:
            try:
                n = len(json.dumps(v[k], ensure_ascii=False).encode("utf-8"))
            except (TypeError, ValueError):
                n = 0
            lines.append("  " * depth + f"  /{k}: {type(v[k]).__name__}, {n} bytes")
            lines.extend(_outline(v[k], depth + 1))
    return lines


def cmd_show(args, adapter: PromoteAdapter, out=sys.stdout, err=sys.stderr) -> int:
    root = evidence.default_root(home_dir())
    if not evidence.EVIDENCE_ID_RE.match(args.evidence_id):
        err.write(f"{adapter.prog}: not an evidence id (expected ev_ + 12 hex)\n")
        return EXIT_FAILED
    entry = evidence.read_entry(root, args.evidence_id)
    if entry is None:
        err.write(f"{adapter.prog}: {args.evidence_id} is not in the spool on this machine\n")
        return EXIT_FAILED
    if is_withheld(entry):
        out.write(entry_line(entry).strip() + "\n")
        out.write("Nothing from this call was kept (no arguments, no result): it cannot be cited. "
                  "Say so plainly in the storyboard.\n")
        return EXIT_OK
    text = json.dumps(entry, ensure_ascii=False, indent=1)
    data = text.encode("utf-8")
    if len(data) <= MAX_SHOW_BYTES:
        out.write(text + "\n")
        return EXIT_OK
    out.write(data[:MAX_SHOW_BYTES].decode("utf-8", errors="ignore") + "\n")
    out.write(f"... ({len(data)} bytes; first {MAX_SHOW_BYTES} shown). Outline of result:\n")
    for line in _outline(entry.get("result")):
        out.write(line + "\n")
    return EXIT_OK


def disabled_reason(root: Path, home: Path, environ: dict, adapter: PromoteAdapter):
    for label, env in (("the environment", environ), (adapter.settings_label, adapter.settings_env(home))):
        v = str(env.get("CARDINAL_EVIDENCE_CAPTURE", "")).strip().lower()
        if v in ("0", "false", "off", "no"):
            return f"CARDINAL_EVIDENCE_CAPTURE={v} in {label}"
    if evidence.capture_disabled(root, {}):
        return f"the flag file {root / evidence.DISABLED_FLAG}"
    return None


def cmd_off(args, adapter: PromoteAdapter, out=sys.stdout, err=sys.stderr) -> int:
    root = evidence.default_root(home_dir())
    evidence.set_capture_disabled(root, True)
    out.write("Evidence capture is off: tool results are no longer kept. Entries already captured stay in "
              f"{root} until they age out (14 days). `{adapter.prog} on` resumes.\n")
    return EXIT_OK


def cmd_on(args, adapter: PromoteAdapter, out=sys.stdout, err=sys.stderr) -> int:
    home = home_dir()
    root = evidence.default_root(home)
    evidence.set_capture_disabled(root, False)
    why = disabled_reason(root, home, dict(os.environ), adapter)
    if why:
        out.write(f"Removed the flag file, but capture is still off: {why}. Remove that setting to resume.\n")
        return EXIT_OK
    out.write("Evidence capture is on: every tool result is kept locally (sensitive calls as withheld stubs) "
              "until you promote it.\n")
    return EXIT_OK


def cmd_status(args, adapter: PromoteAdapter, out=sys.stdout, err=sys.stderr) -> int:
    home = home_dir()
    root = evidence.default_root(home)
    why = disabled_reason(root, home, dict(os.environ), adapter)
    out.write(f"capture: {'off (' + why + ')' if why else 'on'}\n")
    out.write("captures: every tool call; a call that touches something sensitive is kept as a withheld stub\n")
    n, size, sessions = evidence.spool_usage(root)
    cap = evidence.spool_cap_bytes()
    out.write(f"spool: {root} ({n} entries in {sessions} sessions, {size / (1 << 20):.1f} of {cap >> 20} MiB; "
              f"entries are removed after 14 days, oldest first past the cap)\n")
    rules = gate.load_rules(home, os.getcwd())
    for w in rules.warnings:
        out.write(f"evidence rules: ignored {w}\n")
    tokens = [(s, sb, rec) for s, sb, rec in evidence.find_tokens(root) if evidence.token_live(rec)]
    out.write(f"evidence tokens: {len(tokens)} live" + (" (" + ", ".join(sorted({sb for _, sb, _ in tokens})) + ")"
                                                         if tokens else "") + "\n")
    conn = adapter.connection(home, dict(os.environ))
    if conn:
        out.write(f"uploads go to: {conn['origin']}" + (f" (org {conn['org']})" if conn.get("org") else "")
                  + (", Cardinal MCP key present" if conn.get("key") else ", no Cardinal MCP key") + "\n")
    else:
        out.write(f"uploads go to: nowhere yet (not connected: run {adapter.connect_hint})\n")
    return EXIT_OK


def build_parser(prog: str = "cardinal-evidence") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=prog,
        description="Cite any tool result from your agent sessions in a Cardinal storyboard: find, inspect and "
                    "promote locally captured evidence to captured receipts.",
    )
    sub = p.add_subparsers(dest="command", metavar="{promote,list,find,show,off,on,status}")
    pr = sub.add_parser("promote", help="upload spool entries to a draft storyboard as captured receipts")
    pr.add_argument("--storyboard", metavar="SB_ID",
                    help="the draft storyboard (sb_...); default: the one storyboard created in this session")
    pr.add_argument("--force", action="store_true",
                    help="upload again even when an entry was already promoted into this storyboard")
    pr.add_argument("evidence_ids", nargs="+", metavar="ev_ID")

    def scope(sp, limit_default=50):
        grp = sp.add_mutually_exclusive_group()
        grp.add_argument("--session", metavar="ID", help="a session id (default: this or the newest)")
        grp.add_argument("--all", action="store_true", help="every session in the spool")
        sp.add_argument("--limit", "--last", dest="limit", type=int, default=limit_default,
                        help=f"newest N matching entries per session (0: all; default {limit_default})")
        sp.add_argument("--tool", metavar="GLOB", help="only tools whose name matches (e.g. Bash, 'mcp__*')")

    ls = sub.add_parser("list", help="this session's captured evidence")
    scope(ls)
    ls.add_argument("--grep", metavar="TEXT", help="only entries whose args, result or summary contain TEXT")
    ls.add_argument("--withheld", action="store_true", help="only withheld (sensitive, uncitable) calls")
    fd = sub.add_parser("find", help="find captured evidence containing TEXT")
    fd.add_argument("text", metavar="TEXT")
    scope(fd, 20)
    sh = sub.add_parser("show", help="print one entry exactly as promote would cite it")
    sh.add_argument("evidence_id", metavar="ev_ID")
    sub.add_parser("off", help="stop capturing tool results")
    sub.add_parser("on", help="resume capturing tool results")
    sub.add_parser("status", help="capture state, spool size and upload target")
    return p


HANDLERS = {"promote": cmd_promote, "list": cmd_list, "find": cmd_find, "show": cmd_show, "off": cmd_off,
            "on": cmd_on, "status": cmd_status}


def main(argv: Optional[list], adapter: PromoteAdapter) -> int:
    parser = build_parser(adapter.prog)
    args = parser.parse_args(argv)
    handler = HANDLERS.get(args.command)
    if handler is None:
        parser.print_help(sys.stderr)
        return EXIT_FAILED
    try:
        return handler(args, adapter)
    except OSError as e:
        sys.stderr.write(f"{adapter.prog}: {type(e).__name__}: {clip(e)}\n")
        return EXIT_FAILED
