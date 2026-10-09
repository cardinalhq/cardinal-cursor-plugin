"""InvestigationState on the Cardinal server: publish (put-state) and pull
(get-state), and the client attestation the server stores with a state.

The server checks a published state with the same rules as check() (its
port, in maestro) except one: it holds no session transcripts, so it cannot
verify an owner or agent quote. This client verifies them here, against the
transcripts, and ATTESTS what it verified:

  quotes          {key, role, session, utterance_sha256} for each message
                  quote that verify_quote() accepted. key = quote_key(role,
                  at, quote): what was said, by which role, when.
                  utterance_sha256 hashes the normalized utterance holding
                  it, so a verifier that holds the transcript can confirm it
                  later without trusting this client.
  plan_approvals  {key, session} for each plan approval
                  verify_plan_approval() accepted.

The server accepts an owner-authority claim only when the quote it rests on
is attested (otherwise it fails closed, like check() with no transcript),
and returns every such claim to every reader marked as client-attested,
never as verified. The attestation is only as good as this client: it is
recorded with the key and client that published it.

After a successful publish the server copy is canonical: the caller records
the version / etag it got back, and the next publish names that version
(base_version); a publish based on anything else is refused (409).

A state is addressed by its storyboard (storyboard_id, as in plugin 0.40.0)
or by its Investigation (investigation_id), a server object that exists
before any storyboard does: create_investigation, get_investigation,
attach_storyboard. The investigation's id travels in the request, never in
the state document.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from . import investigation_state as ist

CHECKER = "cardinal_core.investigation_state"
MAX_RESPONSE_BYTES = 4 << 20


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def quote_key(role: str, at: str, quote: str) -> str:
    """The attestation key of one quote (maestro investigation-state.ts quoteKey)."""
    return _sha(f"{role}\n{at}\n{ist._norm(quote)}")


def plan_approval_key(plan: dict, approval: dict) -> str:
    """The attestation key of one plan approval (planApprovalKey)."""
    return _sha(f"{ist.PLAN_APPROVAL}\n{quote_key('agent', plan['at'], plan['quote'])}\n"
                f"{quote_key('owner', approval['at'], approval['quote'])}")


def _messages(v: Any):
    if isinstance(v, dict):
        if v.get("kind") == "message":
            yield v
        for x in v.values():
            yield from _messages(x)
    elif isinstance(v, list):
        for x in v:
            yield from _messages(x)


def attest(state: Any, sessions: Optional[dict]) -> dict:
    """The attestation of every quote and plan approval in `state` that the
    transcripts in `sessions` verify. Unverified ones are left out: the
    server then refuses what rests on them, as check() does here. A state
    too malformed to walk attests nothing (check() reports it)."""
    out = {"checker": CHECKER, "schema": ist.SCHEMA, "quotes": [], "plan_approvals": []}
    if not sessions or not isinstance(state, dict):
        return out
    try:
        return _attest(state, sessions, out)
    except ist.MALFORMED:
        return {"checker": CHECKER, "schema": ist.SCHEMA, "quotes": [], "plan_approvals": []}


def _attest(state: dict, sessions: dict, out: dict) -> dict:
    roles = {a.get("id"): a.get("role") for a in state.get("actors") or [] if isinstance(a, dict)}
    seen = set()
    for ref in _messages(state):
        role = roles.get(ref.get("from"))
        if role not in ("owner", "agent") or not isinstance(ref.get("at"), str) or not isinstance(ref.get("quote"), str):
            continue
        key = quote_key(role, ref["at"], ref["quote"])
        if (role, key) in seen or ist.verify_quote(ref, role, sessions) is not None:
            continue
        found = ist._locate(ref, role, sessions)
        if found is None:
            continue
        sid, i = found
        seen.add((role, key))
        out["quotes"].append({"key": key, "role": role, "session": sid,
                              "utterance_sha256": _sha(ist._norm(sessions[sid][i][2]))})
    plans = set()
    for section in ("constraints", "decisions"):
        for item in state.get(section) or []:
            appr = item.get("approval") if isinstance(item, dict) else None
            if not isinstance(appr, dict) or appr.get("kind") != ist.PLAN_APPROVAL:
                continue
            if ist.verify_plan_approval(appr, roles.get, sessions) is not None:
                continue
            key = plan_approval_key(appr["plan_source"], appr["approval_source"])
            if key not in plans:
                plans.add(key)
                out["plan_approvals"].append({"key": key, "session": ist._locate(appr["plan_source"], "agent", sessions)[0]})
    return out


class ServerError(Exception):
    def __init__(self, status: int, body: dict, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.body = body
        self.retry_after = retry_after  # seconds, from a Retry-After header (429)


def _retry_after(headers: Any) -> Optional[float]:
    """Retry-After in seconds (the delta form; an HTTP date is ignored)."""
    try:
        v = headers.get("Retry-After") if headers is not None else None
        return max(0.0, float(int(str(v).strip()))) if v is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _post(conn: dict, tool: str, payload: dict, *, client: str, opener=None, timeout: float = 30.0) -> dict:
    """POST maestro's direct tool route with the MCP key; the JSON body, or
    ServerError carrying the server's status and body.

    A connection with a `token` (an Investigation access grant's token,
    investigation_grants.token_connection) authenticates with
    `Authorization: CardinalInvestigation <token>` instead, and never sends
    a key."""
    token = conn.get("token")
    if not (conn.get("origin") and conn.get("org") and (token or conn.get("key"))):
        raise ist.FetchError("not connected to Cardinal (run /cardinal:connect)")
    if opener is None:
        from .evidence_promote import no_redirect_opener
        opener = no_redirect_opener()
    url = conn["origin"] + "/api/orgs/" + urllib.parse.quote(conn["org"], safe="") + "/storyboards/mcp-tools/" + tool
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Cardinal-Client": client,
    }
    if token:
        headers["Authorization"] = "CardinalInvestigation " + token
    else:
        headers["X-CardinalHQ-API-Key"] = conn["key"]
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers=headers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as e:
        try:
            text = e.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
        finally:
            e.close()
        try:
            body = json.loads(text)
        except ValueError:
            body = {"error": text[:300]}
        raise ServerError(e.code, body if isinstance(body, dict) else {"error": str(body)[:300]},
                          f"{tool} answered {e.code}: {text[:300]}", retry_after=_retry_after(e.headers))
    except (urllib.error.URLError, OSError) as e:
        raise ist.FetchError(f"{tool} failed: {e}")
    try:
        body = json.loads(raw)
    except ValueError:
        raise ist.FetchError(f"{tool} answered with something that is not JSON")
    if not isinstance(body, dict):
        raise ist.FetchError(f"{tool} answered with something that is not an object")
    return body


UNSUPPORTED = ("this Cardinal server does not store InvestigationState yet (it needs a newer Maestro); "
               "the local state file is unchanged and still valid, so nothing needs fixing: skip publishing")

INVESTIGATIONS_UNSUPPORTED = (
    "this Cardinal server does not support investigations yet (it needs a newer Maestro); nothing was "
    "changed. A state can still be kept for a storyboard: publish an act, then `cardinal-storyboard state "
    "init <sb_id>`")

# Errors the state and investigation routes themselves answer; any other 404
# means the route is missing.
_ROUTE_404S = ("state_not_found", "storyboard_not_found", "investigation_not_found", "grant_not_found")

# What the investigation routes refuse, in words.
_PLAIN = {
    "investigation_not_found": "there is no such investigation in this org",
    "storyboard_not_found": "there is no such storyboard in this org",
    "state_not_found": "nothing has been published for it yet",
    "not_investigation_author": "only the investigation's author can do that (it was created by another user or key)",
    "storyboard_attached_elsewhere": "that storyboard already belongs to another investigation",
    "investigation_has_storyboard": "the investigation already has a storyboard (one per investigation)",
    "not_an_act_author": "you are not an author of any act of that storyboard",
    "no_principal": "this connection's key acts for no user or key Cardinal can name; reconnect with /cardinal:connect",
    "quota_exceeded": "this workspace reached its daily limit of new investigations; try again later",
    "token_scope_mismatch": "the access grant does not cover that (another investigation, route or event type)",
    "owner_input_not_readable": ("only the investigation's author and grantees holding owner_input:read can read its "
                                 "owner input"),
    "grant_limit_reached": "this investigation already has its limit of active access grants; revoke one first",
    "grant_not_found": "there is no such access grant for this investigation",
    "invalid_scopes": "the server refused those scopes (read, owner_input:read, advise; owner_input:read needs read)",
    "investigation_tokens_unavailable": "this Cardinal cannot issue access grants (no token signing secret configured)",
    "grant_revoked": "the investigation's author revoked this access grant; ask them for a new one",
    "Invalid token": ("CARDINAL_INVESTIGATION_TOKEN is not valid (expired, malformed or for another server); ask "
                      "the investigation's author for a new grant"),
}


def unsupported(err: "ServerError") -> bool:
    """True when the server predates the state routes: the plugin key's allowlist
    refuses them (403 insufficient_scope) or the route does not exist."""
    code = err.body.get("error")
    if err.status == 403 and code == "insufficient_scope":
        return True
    return err.status in (404, 405) and code not in _ROUTE_404S


# zod's message for a key a strict object does not know (v4, then v3).
_UNRECOGNIZED_RE = re.compile(r"""^Unrecognized key(?:s|\(s\) in object)?: .*["']investigation_id["']""")


def investigations_unsupported(err: "ServerError") -> bool:
    """True when the server predates investigations: the routes are missing
    or refused (as unsupported()), or put-state / get-state exist but their
    strict body refuses the investigation_id key itself (maestro v1.99.11:
    a top-level unrecognized-key issue). Any other 400 is a real refusal."""
    if unsupported(err):
        return True
    if err.status != 400 or err.body.get("error") != "invalid_body":
        return False
    issues = err.body.get("issues")
    return any(isinstance(i, dict) and i.get("path") in ("", None, [])
               and isinstance(i.get("message"), str) and _UNRECOGNIZED_RE.match(i["message"])
               for i in issues if isinstance(issues, list))


def _issues(body: dict) -> str:
    out = []
    for i in body.get("issues") if isinstance(body.get("issues"), list) else []:
        if isinstance(i, dict):
            path, msg = i.get("path"), i.get("message")
            out.append(f"{path}: {msg}" if path not in (None, "", []) else str(msg))
        else:
            out.append(str(i))
    return "; ".join(out)[:300]


def plain(err: "ServerError") -> str:
    """A server refusal in words, never a raw status line; a refused body
    names what the server objected to (its issues)."""
    code = err.body.get("error")
    said = _PLAIN.get(code) or err.body.get("message") or code or "no reason given"
    issues = _issues(err.body)
    return f"Cardinal refused ({said}{': ' + issues if issues else ''})"


def server_copy_ok(got: dict) -> bool:
    """A put-state / get-state answer carries the version and etag the .server
    sidecar records."""
    return isinstance(got.get("version"), int) and isinstance(got.get("etag"), str) and bool(got.get("etag"))


def put_state(conn: dict, state: dict, *, base_version: int, attestation: dict, allow_removed=(),
              client: str, opener=None, investigation_id: Optional[str] = None) -> dict:
    """Publish by investigation_id when given; otherwise by the storyboard the
    state reflects (as in plugin 0.40.0). Never both."""
    if investigation_id is not None:
        _check_investigation_id(investigation_id)
        payload = {"investigation_id": investigation_id}
    else:
        payload = {"storyboard_id": (state.get("source") or {}).get("storyboard_id")}
    payload.update(state=state, base_version=base_version, attestation=attestation)
    if allow_removed:
        payload["allow_removed"] = list(allow_removed)
    return _post(conn, "put-state", payload, client=client, opener=opener)


def get_state(conn: dict, storyboard_id: Optional[str] = None, *, investigation_id: Optional[str] = None,
              client: str, opener=None) -> dict:
    if (storyboard_id is None) == (investigation_id is None):
        raise ist.FetchError("get-state takes a storyboard id or an investigation id")
    if investigation_id is not None:
        _check_investigation_id(investigation_id)
        got = _post(conn, "get-state", {"investigation_id": investigation_id}, client=client, opener=opener)
        if got.get("investigation_id") != investigation_id or not isinstance(got.get("state"), dict):
            raise ist.FetchError("get-state answered for another investigation")
        return got
    if not ist.STORYBOARD_ID_RE.match(storyboard_id or ""):
        raise ist.FetchError(f"not a storyboard id: {storyboard_id!r}")
    got = _post(conn, "get-state", {"storyboard_id": storyboard_id}, client=client, opener=opener)
    if got.get("storyboard_id") != storyboard_id or not isinstance(got.get("state"), dict):
        raise ist.FetchError("get-state answered for another storyboard")
    return got


def _check_investigation_id(investigation_id: Any) -> None:
    if not isinstance(investigation_id, str) or not ist.INVESTIGATION_ID_RE.match(investigation_id):
        raise ist.FetchError(f"not an investigation id: {investigation_id!r}")


MAX_QUESTION = 2000


# The same shape every storyboard tool accepts for a session id (maestro).
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}\Z")


def create_investigation(conn: dict, question: str, window: Optional[dict] = None, *, client: str,
                         session_id: Optional[str] = None, opener=None) -> dict:
    """A new Investigation authored by this key's principal: {investigation_id, author, created_at}.
    session_id: the agent session it is created in; before any storyboard,
    its transcript is the only oracle for the state's quotes."""
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION:
        raise ist.FetchError(f"the question is required, at most {MAX_QUESTION} characters")
    if window is not None and not isinstance(window, dict):
        raise ist.FetchError("the window is a JSON object (the shape a storyboard's window has)")
    payload: dict = {"question": question}
    if window is not None:
        payload["window"] = window
    if session_id is not None:
        if not SESSION_ID_RE.match(session_id):
            raise ist.FetchError(f"not a session id: {session_id!r}")
        payload["session_id"] = session_id
    out = _post(conn, "create-investigation", payload, client=client, opener=opener)
    if not ist.INVESTIGATION_ID_RE.match(str(out.get("investigation_id"))):
        raise ist.FetchError("create-investigation answered without an investigation id")
    return out


def get_investigation(conn: dict, investigation_id: str, *, client: str, opener=None) -> dict:
    """{investigation_id, question, window, author, origin, storyboard_id | null,
    session_id | null, created_at, state: {version, etag, current} | null}."""
    _check_investigation_id(investigation_id)
    out = _post(conn, "get-investigation", {"investigation_id": investigation_id}, client=client, opener=opener)
    sb = out.get("storyboard_id")
    if out.get("investigation_id") != investigation_id or (sb is not None and not ist.STORYBOARD_ID_RE.match(str(sb))):
        raise ist.FetchError("get-investigation answered for another investigation")
    return out


def attach_storyboard(conn: dict, investigation_id: str, storyboard_id: str, *, client: str, opener=None) -> dict:
    _check_investigation_id(investigation_id)
    if not ist.STORYBOARD_ID_RE.match(storyboard_id or ""):
        raise ist.FetchError(f"not a storyboard id: {storyboard_id!r}")
    return _post(conn, "attach-storyboard", {"investigation_id": investigation_id, "storyboard_id": storyboard_id},
                 client=client, opener=opener)


def ensure_session_investigation(conn: dict, session_id: str, *, investigation_id: Optional[str] = None,
                                 started_at: Optional[str] = None, client: str, opener=None,
                                 timeout: float = 30.0) -> dict:
    """maestro ensure-session-investigation: this session's Investigation and
    its live Storyboard, created on the first call and returned on every
    later one (idempotent per org, caller principal and session id). With
    investigation_id: join that one instead (never creates an Investigation).

    {investigation_id, storyboard_id | null, view_url, investigation_url,
     created, question, question_status, author}, validated."""
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        raise ist.FetchError(f"not a session id: {session_id!r}")
    body: dict = {"session_id": session_id}
    if investigation_id is not None:
        _check_investigation_id(investigation_id)
        body["investigation_id"] = investigation_id
    if started_at is not None:
        body["started_at"] = started_at
    out = _post(conn, "ensure-session-investigation", body, client=client, opener=opener, timeout=timeout)
    inv, sb = out.get("investigation_id"), out.get("storyboard_id")
    if not isinstance(inv, str) or not ist.INVESTIGATION_ID_RE.match(inv) \
            or (investigation_id is not None and inv != investigation_id):
        raise ist.FetchError("ensure-session-investigation answered without this session's investigation")
    if sb is not None and not (isinstance(sb, str) and ist.STORYBOARD_ID_RE.match(sb)):
        raise ist.FetchError("ensure-session-investigation answered with something that is not a storyboard id")
    return out


MAX_INVESTIGATION_QUESTION = 1000  # set-investigation-question: the storyboard's own bound


def set_investigation_question(conn: dict, investigation_id: str, question: str, *, client: str,
                               session_id: Optional[str] = None, opener=None) -> dict:
    """maestro set-investigation-question: the investigation's question (and
    its live storyboard's draft question while no act is published). The
    server records who set it; the client claims no authority (session_id
    is recorded as the setter's CLAIMED session)."""
    _check_investigation_id(investigation_id)
    if not isinstance(question, str) or not question.strip():
        raise ist.FetchError("the question is required")
    if len(question) > MAX_INVESTIGATION_QUESTION:
        raise ist.FetchError(f"the question is at most {MAX_INVESTIGATION_QUESTION} characters "
                             f"(this one has {len(question)})")
    if any(ord(c) < 32 and c not in "\n\t" for c in question):
        raise ist.FetchError("the question may not contain control characters other than newline and tab")
    body: dict = {"investigation_id": investigation_id, "question": question}
    if session_id is not None and SESSION_ID_RE.match(session_id):
        body["session_id"] = session_id
    out = _post(conn, "set-investigation-question", body, client=client, opener=opener)
    if out.get("investigation_id") not in (None, investigation_id):
        raise ist.FetchError("set-investigation-question answered for another investigation")
    return out
