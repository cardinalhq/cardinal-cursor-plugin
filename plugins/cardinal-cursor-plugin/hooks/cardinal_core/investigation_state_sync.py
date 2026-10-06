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

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
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
    def __init__(self, status: int, body: dict, message: str):
        super().__init__(message)
        self.status = status
        self.body = body


def _post(conn: dict, tool: str, payload: dict, *, client: str, opener=None, timeout: float = 30.0) -> dict:
    """POST maestro's direct tool route with the MCP key; the JSON body, or
    ServerError carrying the server's status and body."""
    if not (conn.get("origin") and conn.get("org") and conn.get("key")):
        raise ist.FetchError("not connected to Cardinal (run /cardinal:connect)")
    if opener is None:
        from .evidence_promote import no_redirect_opener
        opener = no_redirect_opener()
    url = conn["origin"] + "/api/orgs/" + urllib.parse.quote(conn["org"], safe="") + "/storyboards/mcp-tools/" + tool
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST", headers={
        "X-CardinalHQ-API-Key": conn["key"],
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Cardinal-Client": client,
    })
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
                          f"{tool} answered {e.code}: {text[:300]}")
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

# Errors the state routes themselves answer; any other 404 means the route is missing.
_ROUTE_404S = ("state_not_found", "storyboard_not_found")


def unsupported(err: "ServerError") -> bool:
    """True when the server predates the state routes: the plugin key's allowlist
    refuses them (403 insufficient_scope) or the route does not exist."""
    code = err.body.get("error")
    if err.status == 403 and code == "insufficient_scope":
        return True
    return err.status in (404, 405) and code not in _ROUTE_404S


def server_copy_ok(got: dict) -> bool:
    """A put-state / get-state answer carries the version and etag the .server
    sidecar records."""
    return isinstance(got.get("version"), int) and isinstance(got.get("etag"), str) and bool(got.get("etag"))


def put_state(conn: dict, state: dict, *, base_version: int, attestation: dict, allow_removed=(),
              client: str, opener=None) -> dict:
    sid = (state.get("source") or {}).get("storyboard_id")
    payload = {"storyboard_id": sid, "state": state, "base_version": base_version, "attestation": attestation}
    if allow_removed:
        payload["allow_removed"] = list(allow_removed)
    return _post(conn, "put-state", payload, client=client, opener=opener)


def get_state(conn: dict, storyboard_id: str, *, client: str, opener=None) -> dict:
    if not ist.STORYBOARD_ID_RE.match(storyboard_id or ""):
        raise ist.FetchError(f"not a storyboard id: {storyboard_id!r}")
    got = _post(conn, "get-state", {"storyboard_id": storyboard_id}, client=client, opener=opener)
    if got.get("storyboard_id") != storyboard_id or not isinstance(got.get("state"), dict):
        raise ist.FetchError("get-state answered for another storyboard")
    return got
