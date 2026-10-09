"""Automatic session bootstrap: every connected agent session gets an
Investigation and its live Storyboard, with no command, skill or prompt.

At session start (and lazily after a failure) the adapter calls ensure():

  - an existing binding of this session that bootstrapped ok (and belongs
    to the connected org, and is not overridden by an explicit join) is
    reused as it is: no network. Restart, resume and compaction answer
    "what's the storyboard link?" from the binding file.
  - otherwise maestro's ensure-session-investigation, idempotent per (org,
    caller principal, session id): the first call creates the Investigation
    (origin "session", no invented question) and its one Storyboard draft in
    one transaction; every later call (another hook, a retry, a resume on
    another machine with the same key) returns the same pair. With an
    explicit investigation id (CARDINAL_INVESTIGATION_ID) it joins that one
    and never creates an Investigation.
  - the answer is written into the session's binding
    (investigation_events: investigation_id, storyboard_id, view_url,
    investigation_url, org, capabilities, bootstrap {status: ok}); a binding
    to the same investigation keeps its cursor. `capabilities` is what the
    server advertises, validated to {name: {enabled: bool}}; {} from an
    older Cardinal, so every capability is off.

Failure never blocks the session (fail open). It is recorded, never
announced repeatedly: with a binding (an explicit join) in its
`bootstrap` field, without one in <sid>.bootstrap.json, both as
{status: "failed", last_error, retry_after, attempts}. A retry waits for
retry_after: 30 s doubling to 30 min after a network error or a 5xx, the
server's Retry-After (at least 10 min) after a 429, 6 h after a refusal that
retrying cannot fix (401, 403, an unknown investigation). A server without
the route (an older Maestro) records nothing: the session stays unbound, as
before, and the next session start asks again.

Nothing here uploads evidence: the only requests are
ensure-session-investigation and (CLI) set-investigation-question.

Standard library only. ensure() never raises.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from . import investigation_events as ie
from . import investigation_state as ist
from . import investigation_state_sync as sync

TIMEOUT = 3.0               # SessionStart's request (hooks.json gives the hook 6 s)
RETRY_FIRST = 30.0
RETRY_MAX = 1800.0
RETRY_QUOTA_MIN = 600.0
RETRY_PERMANENT = 6 * 3600.0
MAX_ERROR = 200
MAX_URL = 512

# What a URL put into the model's context may contain: an absolute http(s)
# URL of printable ASCII without quotes, angle brackets, backslashes or
# whitespace, so a server answer cannot carry a second instruction.
_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+")


def safe_url(v: Any, origin: Optional[str]) -> Optional[str]:
    """`v` as an absolute http(s) URL fit for model context, or None. An
    app-relative path (a self-hosted install without a public base URL) is
    put under the connection's origin."""
    if not isinstance(v, str) or not v:
        return None
    if v.startswith("/") and not v.startswith("//") and isinstance(origin, str):
        v = origin.rstrip("/") + v
    if len(v) > MAX_URL or not _URL_RE.fullmatch(v):
        return None
    return v


def pending_path(home: Path, sid: str) -> Path:
    return ie.sessions_dir(home) / f"{ie.binding_path(home, sid).stem}.bootstrap.json"


def refresh_path(home: Path, sid: str) -> Path:
    """Marker: this session started on a reused binding, so its capabilities
    are to be asked again (by the background poller, refresh_capabilities)."""
    return ie.sessions_dir(home) / f"{ie.binding_path(home, sid).stem}.refresh"


def wants_refresh(home: Path, sid: str) -> bool:
    try:
        return refresh_path(home, sid).exists()
    except (OSError, ValueError):
        return False


def read_pending(home: Path, sid: str) -> Optional[dict]:
    try:
        data = json.loads(pending_path(home, sid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _num(v: Any) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def _state(binding: Optional[dict], home: Path, sid: str) -> dict:
    st = binding.get("bootstrap") if binding else read_pending(home, sid)
    return st if isinstance(st, dict) else {}


def reusable(binding: Optional[dict], conn: dict, wanted: Optional[str]) -> bool:
    """A binding that needs no request: it bootstrapped ok, in this org, and
    no explicit join names another investigation."""
    if not binding:
        return False
    if wanted and binding["investigation_id"] != wanted:
        return False
    if binding.get("org") not in (None, conn.get("org")):
        return False
    st = binding.get("bootstrap")
    return isinstance(st, dict) and st.get("status") == "ok"


def due(home: Path, sid: str, now: Optional[float] = None) -> bool:
    """Whether a lazy retry may ask the server now (no failure recorded, or
    its retry_after has passed). False when the session bootstrapped ok."""
    now = time.time() if now is None else now
    b = ie.read_binding(home, sid)
    st = _state(b, home, sid)
    if st.get("status") == "ok":
        return False
    return now >= _num(st.get("retry_after"))


def needs_retry(home: Path, sid: str) -> bool:
    """A failed bootstrap is recorded for this session (the poller retries)."""
    b = ie.read_binding(home, sid)
    return _state(b, home, sid).get("status") == "failed"


def _delay(err: Exception, attempts: int) -> float:
    if isinstance(err, sync.ServerError):
        if err.status == 429:
            return max(RETRY_QUOTA_MIN, err.retry_after or 0.0)
        if err.status in (401, 403) or (err.status == 404 and err.body.get("error") == "investigation_not_found"):
            return RETRY_PERMANENT
    return min(RETRY_MAX, RETRY_FIRST * (2 ** max(0, attempts - 1)))


def reason(err: Exception) -> str:
    """A short, fixed phrase for model context (never server-supplied text)."""
    if isinstance(err, sync.ServerError):
        if err.status == 429:
            return "this workspace reached its limit of new investigations for now"
        if err.status in (401, 403):
            return "this connection's key may not create investigations"
        if err.status == 404 and err.body.get("error") == "investigation_not_found":
            return "there is no such investigation in this org"
        return f"Cardinal answered HTTP {int(err.status)}"
    return "Cardinal could not be reached"


def _describe(err: Exception) -> str:
    if isinstance(err, sync.ServerError):
        msg = sync.plain(err)
    else:
        msg = f"could not reach Cardinal ({err})"
    msg = " ".join(str(msg).split())
    return msg[:MAX_ERROR]


@contextlib.contextmanager
def _bootstrap_lock(home: Path, sid: str, wait: float):
    """Serializes concurrent bootstraps of one session (two hooks, a hook and
    the CLI) so the second reuses the first's answer instead of asking again.
    The server is idempotent either way."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover
        yield True
        return
    import os
    d = ie._ensure_dirs(home)
    fd = os.open(str(d / f"{ie.binding_path(home, sid).stem}.bootstrap.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    got = False
    try:
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield got
    finally:
        if got:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def ensure(home: Path, sid: str, conn: dict, client: str, *, wanted: Optional[str] = None,
           started_at: Optional[str] = None, opener=None, timeout: float = TIMEOUT, force: bool = False,
           now: Optional[float] = None) -> dict:
    """Make sure session `sid` is bound to its Investigation and live
    Storyboard. Returns {"status", "binding", "error"}:

      reused       the binding was already complete; no request
      ok           ensure-session-investigation answered; binding written
      backoff      an earlier failure's retry_after has not passed (force
                   ignores it: the user asked, through the CLI)
      failed       the request failed; recorded with its retry_after
      unsupported  the server has no such route (an older Maestro)
      busy         another process is bootstrapping this session
      invalid      no session id / no connection

    `binding` is the session's binding afterwards (None when unbound).
    Never raises."""
    try:
        return _ensure(home, sid, conn, client, wanted, started_at, opener, timeout, force,
                       time.time() if now is None else now)
    except Exception as e:  # fail open
        return {"status": "failed", "binding": None, "error": _describe(e)}


def _ensure(home, sid, conn, client, wanted, started_at, opener, timeout, force, now) -> dict:
    if not ie.valid_session(sid) or not (conn and conn.get("origin") and conn.get("org") and conn.get("key")):
        return {"status": "invalid", "binding": None, "error": None}
    if wanted is not None and not ie.valid_investigation(wanted):
        wanted = None
    with _bootstrap_lock(home, sid, wait=min(timeout, 2.0)) as got:
        b = ie.read_binding(home, sid)
        if not got:
            return {"status": "busy", "binding": b, "error": None}
        if reusable(b, conn, wanted):
            # No request on the session-start path: the background poller asks
            # again for what the server advertises (refresh_capabilities), so a
            # capability turned off or rolled back reaches the binding within
            # moments of this start, for the next start (resume, compaction).
            with contextlib.suppress(OSError, ValueError):
                ie._ensure_dirs(home)
                refresh_path(home, sid).touch()
            return {"status": "reused", "binding": b, "error": None}
        same_org = b is not None and b.get("org") in (None, conn.get("org"))
        join = wanted or (b["investigation_id"] if b and same_org else None)
        st = _state(b if (b and (not wanted or b["investigation_id"] == wanted)) else None, home, sid)
        if not force and st.get("status") == "failed" and now < _num(st.get("retry_after")):
            return {"status": "backoff", "binding": b, "error": st.get("last_error"), "reason": st.get("reason")}
        try:
            out = sync.ensure_session_investigation(conn, sid, investigation_id=join, started_at=started_at,
                                                    client=client, opener=opener, timeout=timeout)
        except Exception as e:
            if isinstance(e, sync.ServerError) and sync.unsupported(e):
                # An older Maestro: behave as before (an explicit join still binds
                # locally) and stop retrying in this session.
                with contextlib.suppress(OSError):
                    pending_path(home, sid).unlink()
                if wanted and (not b or b["investigation_id"] != wanted):
                    b, _ = ie.adopt(home, sid, wanted, "env", {"org": conn.get("org")}, now=now)
                elif b is not None and isinstance(b.get("bootstrap"), dict):
                    b = ie.update_fields(home, sid, {"bootstrap": {"status": "unsupported", "at": ie._now_iso(now)}}) or b
                return {"status": "unsupported", "binding": b, "error": None}
            attempts = int(_num(st.get("attempts"))) + 1
            failed = {"status": "failed", "last_error": _describe(e), "reason": reason(e),
                      "retry_after": now + _delay(e, attempts), "attempts": attempts, "at": ie._now_iso(now)}
            if join:
                b = _bind_join(home, sid, join, conn, failed, now, source="env" if wanted else None)
            else:
                ie._ensure_dirs(home)
                ie.write_json(pending_path(home, sid), failed)
            return {"status": "failed", "binding": b, "error": failed["last_error"], "reason": failed["reason"]}
        # Joining someone else's investigation is not authoring it: the server
        # says so (is_author on a join); an unknown answer to a join counts as
        # not the author. A session's own (created) investigation is its own.
        is_author = out.get("is_author") if isinstance(out.get("is_author"), bool) else join is None
        fields = {
            "is_author": is_author,
            "storyboard_id": out.get("storyboard_id"),
            "view_url": safe_url(out.get("view_url"), conn.get("origin")),
            "investigation_url": safe_url(out.get("investigation_url"), conn.get("origin")),
            "org": conn.get("org"),
            "bootstrap": {"status": "ok", "at": ie._now_iso(now)},
        }
        if out.get("question_status") in ("provisional", "stated"):
            fields["question_status"] = out["question_status"]
        # What this Cardinal advertises (`capabilities`, e.g. projection: it
        # keeps the storyboard up to date from the checkpoints itself), validated
        # to {name: {enabled: bool}}; an older Cardinal without the block gets {}
        # (every capability off). Written on every answer. A reused binding
        # (above) is not asked here: the poller refreshes it once per session
        # start (refresh_capabilities). Guidance only: the server enforces.
        fields["capabilities"] = capabilities(out)
        source = "env" if wanted else "auto"
        b, _ = ie.adopt(home, sid, out["investigation_id"], source, fields, now=now)
        for done in (pending_path(home, sid), refresh_path(home, sid)):   # this answer is current
            with contextlib.suppress(OSError):
                done.unlink()
        if _owner_input_gate(home, sid, b):
            with ie.locked(home, sid, wait=2.0) as got_lock:
                cur = ie.read_binding(home, sid)
                if got_lock and cur is not None and _owner_input_gate(home, sid, cur):
                    ie.write_binding(home, sid, cur)
                    b = cur
        return {"status": "ok", "binding": b, "error": None}


def _bind_join(home: Path, sid: str, inv: str, conn: dict, state: dict, now: float,
               source: Optional[str] = "env") -> Optional[dict]:
    """An explicit join (or a known binding) whose request failed: bind (or
    keep) it locally so advisory events still arrive, and record the failure
    on it."""
    try:
        old = ie.read_binding(home, sid)
        src = source or (old.get("source") if old else "auto") or "auto"
        b, _ = ie.adopt(home, sid, inv, src, {"org": conn.get("org"), "bootstrap": state}, now=now)
        return b
    except (OSError, ValueError):
        return ie.read_binding(home, sid)


# Capabilities a Cardinal advertises on ensure-session-investigation and
# get-investigation: {"capabilities": {"<name>": {"enabled": bool, ...}}}.
# The plugin does nothing for a capability that is not advertised as enabled.
PROJECTION = "projection"   # the server keeps the live storyboard up to date from the checkpoints
OWNER_INPUT = "owner_input"  # the server records the owner's prompts (owner_input.py)
CAPABILITY_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")   # fullmatch
MAX_CAPABILITIES = 32


def capabilities(answer: Any) -> dict:
    """`answer`'s capabilities block, validated to {name: {"enabled": bool}}:
    {} when it is absent (an older Cardinal) or not an object; an entry whose
    name is not a plain identifier or whose value is not an object is
    dropped; `enabled` is True only for an explicit true. Anything else the
    server says about a capability is not kept."""
    caps = answer.get("capabilities") if isinstance(answer, dict) else None
    if not isinstance(caps, dict):
        return {}
    out: dict = {}
    for name, v in caps.items():
        if len(out) >= MAX_CAPABILITIES:
            break
        if isinstance(name, str) and CAPABILITY_NAME_RE.fullmatch(name) and isinstance(v, dict):
            out[name] = {"enabled": v.get("enabled") is True}
    return out


def refresh_capabilities(home: Path, sid: str, conn: dict, client: str, *, opener=None,
                         timeout: float = 10.0) -> str:
    """Ask ensure-session-investigation again (joining the bound
    investigation: idempotent, never creates one) and store only what it
    now advertises in the binding's `capabilities`. Run by the background
    poller when wants_refresh(). Returns:
      ok     refreshed; the marker is gone
      off    the server refused for good (no route: an older Cardinal; any
             other 4xx but 408 / 429): capabilities {}; the marker is gone
      none   nothing to do (no marker, no binding, no connection)
    Raises on a transient failure (network, 5xx, 408, 429, a malformed
    answer): the marker stays and the caller retries with back-off."""
    if not wants_refresh(home, sid):
        return "none"
    b = ie.read_binding(home, sid)
    if b is None:
        with contextlib.suppress(OSError):
            refresh_path(home, sid).unlink()
        return "none"
    if not (conn and conn.get("origin") and conn.get("org") and conn.get("key")):
        return "none"
    inv = b["investigation_id"]
    refused = False
    try:
        out = sync.ensure_session_investigation(conn, sid, investigation_id=inv, client=client, opener=opener,
                                                timeout=timeout)
        caps = capabilities(out)
    except sync.ServerError as e:
        if not (sync.unsupported(e) or (400 <= e.status < 500 and e.status not in (408, 429))):
            raise
        caps, refused = {}, True
    with ie.locked(home, sid, wait=2.0) as got:
        if not got:
            raise OSError("the session's binding is locked by another process")
        cur = ie.read_binding(home, sid)
        if cur is not None and cur["investigation_id"] == inv:
            cur["capabilities"] = caps
            _owner_input_gate(home, sid, cur)
            ie.write_binding(home, sid, cur)
    with contextlib.suppress(OSError):
        refresh_path(home, sid).unlink()
    return "off" if refused else "ok"


def _owner_input_gate(home: Path, sid: str, binding: Optional[dict]) -> bool:
    """The capabilities just read no longer let this session record owner
    input (owner_input off or absent, or not the author): its owner input
    outbox goes, so nothing queued is sent later, and `binding` (mutated;
    the caller writes it) forgets that the user was told, so turning the
    capability on again discloses again. Whether `binding` changed."""
    try:
        from . import owner_input
        if owner_input.enabled(binding):
            return False
        owner_input.drop_outbox(home, sid)
        return isinstance(binding, dict) and binding.pop("owner_input_disclosed", None) is not None
    except Exception:
        return False


def capability_enabled(caps: Any, name: str) -> bool:
    """Whether `caps` (a capabilities block, raw or validated) says `name` is
    enabled. A missing block, a missing key or a malformed entry is off."""
    return capabilities({"capabilities": caps}).get(name, {}).get("enabled") is True


def started_now(now: Optional[float] = None) -> str:
    """An RFC 3339 timestamp for ensure's started_at."""
    now = time.time() if now is None else now
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def describe(binding: Optional[dict]) -> dict:
    """The binding's public facts: {investigation_id, storyboard_id,
    view_url, investigation_url, bootstrap_status, is_author, capabilities}
    (None values when unknown; capabilities validated, {} when unknown).
    Never a key or an org secret."""
    b = binding or {}
    st = b.get("bootstrap") if isinstance(b.get("bootstrap"), dict) else {}
    sb = b.get("storyboard_id")
    return {
        "investigation_id": b.get("investigation_id"),
        "storyboard_id": sb if isinstance(sb, str) and ist.STORYBOARD_ID_RE.match(sb) else None,
        "view_url": safe_url(b.get("view_url"), None),
        "investigation_url": safe_url(b.get("investigation_url"), None),
        "bootstrap_status": st.get("status"),
        "is_author": b.get("is_author") is not False,
        "capabilities": capabilities(b),
    }
