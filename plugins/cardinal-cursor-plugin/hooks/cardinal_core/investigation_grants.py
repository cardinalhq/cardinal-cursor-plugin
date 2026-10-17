"""Investigation access grants: scoped, revocable access to ONE
Investigation for someone other than its author (another agent acting as a
supervisor, a reviewer), without handing over this machine's key.

maestro routes (same mcp-tools base; the plugin's API key; the
investigation's author only):

  grant-investigation-access {investigation_id, scopes, ttl_seconds?, label?}
      -> {grant_id, token, expires_at, scopes, label, principal:
          "grantee:<grant_id>", investigation_id, org}   (the token, once)
  list-investigation-grants {investigation_id} -> {grants: [...]} (never tokens)
  revoke-investigation-grant {grant_id}               (idempotent)

Scopes: `read` (the control and semantic events, get-investigation),
`owner_input:read` (adds the owner input class; needs read), `advise`
(posts cue / question / challenge) and `supervise` (delegated supervision:
implies read and advise, and adds issue-ruling: one binding ruling on each
of its own challenges once the worker took a position; a newer Maestro). TTL 5 min to 24 h (default 4 h); at
most 20 live grants per investigation.

The grantee uses the token alone: CARDINAL_INVESTIGATION_TOKEN (and
CARDINAL_INVESTIGATION_ORIGIN, else this machine's configured origin; https,
or plain http only to localhost / 127.0.0.1 / [::1]).
token_connection() reads them; investigation_state_sync._post then sends
`Authorization: CardinalInvestigation <token>` and never a key. The org and
the investigation come from the token's own claims (`org`, `inv`), read
without verifying the signature: the server verifies it on every request.
Standard library only.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
from typing import Any, Optional

from . import investigation_events as ie
from . import investigation_state as ist
from . import investigation_state_sync as sync

READ = "read"
OWNER_INPUT_READ = "owner_input:read"
ADVISE = "advise"
EVIDENCE_READ = "evidence:read"
SUPERVISE = "supervise"   # delegated supervision: read + advise + issue-ruling on its own challenges
SCOPES = (READ, OWNER_INPUT_READ, ADVISE, EVIDENCE_READ, SUPERVISE)
DEFAULT_SCOPES = (READ, ADVISE)
TTL_DEFAULT = 14400
TTL_MIN = 300
TTL_MAX = 86400
MAX_LABEL = 200
TOKEN_ENV = "CARDINAL_INVESTIGATION_TOKEN"
ORIGIN_ENV = "CARDINAL_INVESTIGATION_ORIGIN"
TOKEN_TYPE = "CardinalInvestigation"   # the JWT header's typ, and the Authorization scheme
PRINCIPAL_PREFIX = "grantee:"

GRANT_ID_RE = re.compile(r"grt_[0-9a-f]{24}")                                     # fullmatch
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{2,4096}\.[A-Za-z0-9_-]{2,4096}\.[A-Za-z0-9_-]{2,4096}")  # fullmatch
_ORIGIN_RE = re.compile(r"(https?)://([A-Za-z0-9.-]+|\[::1\])(?::[0-9]{1,5})?")  # fullmatch
LOOPBACK = ("localhost", "127.0.0.1", "[::1]")
_ORG_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")                                   # fullmatch


def valid_grant_id(v: Any) -> bool:
    return isinstance(v, str) and bool(GRANT_ID_RE.fullmatch(v))


def parse_ttl(s: Any) -> int:
    """"4h", "90m", "1d", "3600" or "3600s" -> seconds. ValueError otherwise."""
    m = re.fullmatch(r"\s*([0-9]{1,6})\s*([smhd]?)\s*", str(s))
    if not m:
        raise ValueError(f"not a duration: {s!r} (e.g. 4h, 90m, 3600)")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def parse_scopes(raw: Optional[str], owner_input: bool = False) -> list:
    """`--scope read,advise` (+ --owner-input) -> the scopes, in SCOPES
    order. ValueError on an unknown scope, none, or owner input without
    read."""
    names = [x.strip() for x in (raw if raw is not None else ",".join(DEFAULT_SCOPES)).split(",") if x.strip()]
    bad = [x for x in names if x not in SCOPES]
    if bad:
        raise ValueError(f"unknown scope {', '.join(repr(x) for x in bad)}: one or more of {', '.join(SCOPES)}")
    got = set(names)
    if SUPERVISE in got:
        got.update((READ, ADVISE))   # supervise implies them; listed explicitly for an unambiguous grant
    if owner_input:
        got.add(OWNER_INPUT_READ)
    if not got:
        raise ValueError("at least one scope")
    if OWNER_INPUT_READ in got and READ not in got:
        raise ValueError("owner input needs the read scope too")
    if EVIDENCE_READ in got and READ not in got:
        raise ValueError("evidence:read requires read")
    return [x for x in SCOPES if x in got]


def valid_label(v: Any) -> bool:
    return isinstance(v, str) and 1 <= len(v) <= MAX_LABEL and v.strip() != "" and v.isprintable()


# ---------------------------------------------------------------------------
# Author side (the plugin's key)
# ---------------------------------------------------------------------------

def grant(conn: dict, investigation_id: str, scopes: list, *, ttl_seconds: Optional[int] = None,
          label: Optional[str] = None, client: str, opener=None,
          supervisor: Optional[dict] = None) -> dict:
    """grant-investigation-access: the grant, its token shown once.

    supervisor: {investigation_id, session_id} of the minting session (the
    supervisor's own investigation), sent as supervisor_investigation_id /
    supervisor_session_id so the storyboards link "Supervising" / "Supervised
    by". Only with the supervise scope; never the granted investigation."""
    if not ie.valid_investigation(investigation_id):
        raise ist.FetchError(f"not an investigation id: {investigation_id!r}")
    if not scopes or any(s not in SCOPES for s in scopes):
        raise ist.FetchError(f"the scopes are one or more of {', '.join(SCOPES)}")
    if EVIDENCE_READ in scopes and READ not in scopes:
        raise ValueError("evidence:read requires read")
    body: dict = {"investigation_id": investigation_id, "scopes": list(scopes)}
    if ttl_seconds is not None:
        if not TTL_MIN <= int(ttl_seconds) <= TTL_MAX:
            raise ist.FetchError(f"the TTL is {TTL_MIN} to {TTL_MAX} seconds")
        body["ttl_seconds"] = int(ttl_seconds)
    if label is not None:
        if not valid_label(label):
            raise ist.FetchError(f"the label is 1 to {MAX_LABEL} printable characters")
        body["label"] = label
    if supervisor is not None:
        sup_inv, sup_sid = supervisor.get("investigation_id"), supervisor.get("session_id")
        if SUPERVISE not in scopes:
            raise ValueError("a supervisor link needs the supervise scope")
        if not ie.valid_investigation(sup_inv) or not ie.valid_session(sup_sid):
            raise ValueError("the supervisor link needs an investigation id and a session id")
        if sup_inv == investigation_id:
            raise ValueError("an investigation cannot supervise itself")
        body["supervisor_investigation_id"] = sup_inv
        body["supervisor_session_id"] = sup_sid
    out = sync._post(conn, "grant-investigation-access", body, client=client, opener=opener)
    tok = out.get("token")
    if out.get("investigation_id") != investigation_id or not valid_grant_id(out.get("grant_id")) \
            or not isinstance(tok, str) or not _TOKEN_RE.fullmatch(tok):
        raise ist.FetchError("grant-investigation-access answered for another investigation or without a token", ambiguous=True)
    return out


def list_grants(conn: dict, investigation_id: str, *, client: str, opener=None) -> list:
    """list-investigation-grants: the investigation's grants (never tokens)."""
    if not ie.valid_investigation(investigation_id):
        raise ist.FetchError(f"not an investigation id: {investigation_id!r}")
    out = sync._post(conn, "list-investigation-grants", {"investigation_id": investigation_id}, client=client,
                     opener=opener)
    grants = out.get("grants")
    if out.get("investigation_id") not in (None, investigation_id) or not isinstance(grants, list):
        raise ist.FetchError("list-investigation-grants answered for another investigation or without grants")
    return [g for g in grants if isinstance(g, dict)]


def revoke(conn: dict, grant_id: str, *, client: str, opener=None) -> dict:
    """revoke-investigation-grant (idempotent)."""
    if not valid_grant_id(grant_id):
        raise ist.FetchError(f"not a grant id: {grant_id!r} (grt_ followed by 24 hex characters)")
    return sync._post(conn, "revoke-investigation-grant", {"grant_id": grant_id}, client=client, opener=opener)


# zod's message for unknown top-level keys (v4, then v3) naming a supervisor field.
_UNRECOGNIZED_SUPERVISOR_RE = re.compile(
    r"""^Unrecognized key(?:s|\(s\) in object)?: .*["']supervisor_(?:investigation|session)_id["']""")


def supervisor_unsupported(err: Any) -> bool:
    """An older maestro's strict grant schema refusing the supervisor fields:
    400 invalid_body with a top-level unrecognized-key issue naming them.
    Any other 400 is a real refusal."""
    body = getattr(err, "body", None)
    if not (isinstance(err, sync.ServerError) and err.status == 400 and isinstance(body, dict)
            and body.get("error") == "invalid_body"):
        return False
    issues = body.get("issues")
    return any(isinstance(i, dict) and i.get("path") in ("", None, [])
               and isinstance(i.get("message"), str) and _UNRECOGNIZED_SUPERVISOR_RE.match(i["message"])
               for i in (issues if isinstance(issues, list) else []))


def supervisor_invalid(err: Any) -> bool:
    """maestro refusing the supervisor link (not this author's investigation
    or session, or the granted investigation itself)."""
    body = getattr(err, "body", None)
    return (isinstance(err, sync.ServerError) and err.status == 422 and isinstance(body, dict)
            and body.get("error") == "supervisor_invalid")


def refusal(err: "sync.ServerError") -> Optional[str]:
    """A grant refusal that needs more than its code: grant_limit_reached
    names which limit (scope "active": unrevoked, unexpired grants;
    "lifetime": every grant ever made for the investigation) and how many.
    None for anything else (the caller says it as usual)."""
    body = err.body if isinstance(err.body, dict) else {}
    if err.status != 409 or body.get("error") != "grant_limit_reached":
        return None
    scope, limit = body.get("scope"), body.get("limit")
    n = f" ({limit})" if isinstance(limit, int) and not isinstance(limit, bool) else ""
    if scope == "active":
        return (f"this investigation already has its limit of active access grants{n}; revoke one "
                "(`cardinal-storyboard investigation grants`, then `investigation revoke <grant_id>`)")
    if scope == "lifetime":
        return (f"this investigation reached its lifetime limit of access grants{n}; revoking does not free one, "
                "so no more grants can be made for it")
    return f"this investigation reached a limit of access grants{n} (scope {ie._tok(scope)})"


def grant_id_of(g: dict) -> Any:
    return g.get("grant_id") if g.get("grant_id") is not None else g.get("id")


def grant_line(g: dict) -> str:
    """One grant, inert: its id, label (a JSON string), scopes, expiry and
    whether it is revoked or expired."""
    scopes = g.get("scopes")
    shown = ",".join(ie._tok(s) for s in scopes) if isinstance(scopes, list) else "unknown"
    label = g.get("label")
    state = "revoked" if g.get("revoked") is True or g.get("revoked_at") else \
        ("expired" if g.get("active") is False or g.get("expired") is True else "active")
    line = f"{ie._tok(grant_id_of(g))} {state} scopes {shown} expires {ie._tok(g.get('expires_at'))}"
    if isinstance(label, str) and label:
        line += f" label {ie._claimed(label)}"
    if g.get("granted_by"):
        line += f" granted by {ie._tok(g.get('granted_by'))}"
    return line


def env_line(token: str, origin: str) -> str:
    """The one line `investigation grant` prints on stdout."""
    return f"export {TOKEN_ENV}={shlex.quote(token)} {ORIGIN_ENV}={shlex.quote(origin)}"


# ---------------------------------------------------------------------------
# Grantee side (the token alone)
# ---------------------------------------------------------------------------

def _b64json(part: str) -> Any:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode("utf-8"))


def token_claims(token: str) -> dict:
    """The token's header typ and claims, decoded WITHOUT verifying the
    signature (the server verifies it on every request): {typ, org, inv,
    gid, scopes, exp}. ValueError when it is not an Investigation access
    token."""
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise ValueError(f"{TOKEN_ENV} is not an Investigation access token")
    head, body, _ = token.split(".")
    try:
        header, claims = _b64json(head), _b64json(body)
    except (ValueError, UnicodeDecodeError):
        raise ValueError(f"{TOKEN_ENV} is not an Investigation access token")
    if not isinstance(header, dict) or not isinstance(claims, dict) or header.get("typ") != TOKEN_TYPE:
        raise ValueError(f"{TOKEN_ENV} is not an Investigation access token ({TOKEN_TYPE})")
    org, inv = claims.get("org"), claims.get("inv")
    if not isinstance(org, str) or not _ORG_RE.fullmatch(org):
        raise ValueError(f"{TOKEN_ENV} names no org")
    if not ie.valid_investigation(inv):
        raise ValueError(f"{TOKEN_ENV} names no investigation")
    return {"typ": header.get("typ"), "org": org, "inv": inv, "gid": claims.get("gid"),
            "scopes": claims.get("scopes") if isinstance(claims.get("scopes"), list) else [],
            "exp": claims.get("exp")}


def token_set(environ: Optional[dict] = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(str(env.get(TOKEN_ENV) or "").strip())


def token_connection(environ: Optional[dict] = None, fallback_origin: Optional[str] = None) -> Optional[dict]:
    """{origin, org, token, investigation_id, grant_id, scopes} from
    CARDINAL_INVESTIGATION_TOKEN (org and investigation from its claims) and
    CARDINAL_INVESTIGATION_ORIGIN (else `fallback_origin`, the configured
    connection's). None when the token is not set. ValueError when it is
    set but unusable: the caller must never fall back to a key then."""
    env = os.environ if environ is None else environ
    token = str(env.get(TOKEN_ENV) or "").strip()
    if not token:
        return None
    claims = token_claims(token)
    origin = str(env.get(ORIGIN_ENV) or "").strip() or (fallback_origin or "")
    origin = origin.rstrip("/")
    if not origin:
        raise ValueError(f"set {ORIGIN_ENV} (`cardinal-storyboard investigation grant` prints it)")
    m = _ORIGIN_RE.fullmatch(origin)
    if not m:
        raise ValueError(f"{ORIGIN_ENV} is an https origin (scheme, host, port; no path)")
    if m.group(1) != "https" and m.group(2).lower() not in LOOPBACK:
        # The token is a bearer credential: never over plain http, except to
        # this machine.
        raise ValueError(f"{ORIGIN_ENV} must be https (plain http only for localhost, 127.0.0.1 or [::1])")
    return {"origin": origin, "org": claims["org"], "token": token, "investigation_id": claims["inv"],
            "grant_id": claims["gid"] if valid_grant_id(claims["gid"]) else None, "scopes": claims["scopes"]}


# ---------------------------------------------------------------------------
# Supervisor side: a ruling on the grantee's own challenge (issue-ruling)
# ---------------------------------------------------------------------------

RULING_UNSUPPORTED = ("this Cardinal server does not support rulings yet (it needs a newer Maestro with "
                      "delegated supervision); nothing was recorded")
_RULING_ROUTE_404S = ("investigation_not_found", "ruling_target_not_found", "ack_target_not_found")


def ruling_unsupported(err: "sync.ServerError") -> bool:
    """True when the refusal itself shows the server has no issue-ruling
    route (a 404 / 405 that is not one of the route's own). An older
    Maestro may instead answer a grant token on an unknown route 401
    "Invalid token": supports_rulings() tells that apart."""
    code = err.body.get("error") if isinstance(err.body, dict) else None
    return err.status in (404, 405) and code not in _RULING_ROUTE_404S


def supports_rulings(conn: dict, investigation_id: str, *, client: str, opener=None) -> Optional[bool]:
    """Whether the server advertises capabilities.delegated_supervision
    (get-investigation, with the same grant token); None when that cannot
    be read either (the token itself is refused, or the network)."""
    try:
        out = sync.get_investigation(conn, investigation_id, client=client, opener=opener)
    except Exception:
        return None
    caps = out.get("capabilities") if isinstance(out.get("capabilities"), dict) else {}
    cap = caps.get("delegated_supervision")
    return isinstance(cap, dict) and cap.get("enabled") is True


def ruling_payload(challenge_seq: int, outcome: str, text: str, refs: Optional[list] = None) -> dict:
    """{ruling_of, outcome, text, refs?}, checked before anything is sent."""
    if not isinstance(challenge_seq, int) or isinstance(challenge_seq, bool) or challenge_seq < 1:
        raise ist.FetchError("the challenge number is a positive integer")
    if outcome not in ("upheld", "withdrawn"):
        raise ist.FetchError("the outcome is upheld or withdrawn")
    payload = ie.text_payload(text, refs)
    return {"ruling_of": challenge_seq, "outcome": outcome, **payload}


def issue_ruling(conn: dict, investigation_id: str, challenge_seq: int, outcome: str, text: str, *,
                 refs: Optional[list] = None, client: str, opener=None) -> dict:
    """issue-ruling: {event, duplicate?}. Only with a supervise grant's token
    (conn["token"]), only on a challenge.added this grant posted (an
    owner_web grant: also one an earlier supervise grant posted); upheld
    only once the worker took a position on it; one ruling per challenge
    (the server checks each and answers ruling_target_not_own_challenge /
    ruling_exists / ruling_requires_position, formerly
    ruling_requires_decline). The idempotency key is per challenge, so a
    retry of the same ruling is deduplicated."""
    if not ie.valid_investigation(investigation_id):
        raise ist.FetchError(f"not an investigation id: {investigation_id!r}")
    if not conn.get("token"):
        raise ist.FetchError("a ruling needs a supervise grant's token (CARDINAL_INVESTIGATION_TOKEN or "
                             "--credential-file); the investigation's author asks for one with "
                             "`cardinal-storyboard investigation grant --scope supervise`")
    body = {"investigation_id": investigation_id, "payload": ruling_payload(challenge_seq, outcome, text, refs),
            "idempotency_key": f"rule:{challenge_seq}", "type": "ruling.issued", "client": client}
    out = sync._post(conn, "issue-ruling", body, client=client, opener=opener)
    ev = out.get("event")
    if not isinstance(ev, dict) or ev.get("investigation_id") != investigation_id or not isinstance(ev.get("seq"), int):
        raise ist.FetchError("issue-ruling answered without the ruling")
    return out
