"""Relayed owner decisions: the owner decides, in the supervisor session's
own terminal, for the worker that session supervises
(docs/specs/relayed-owner-decisions.md).

Two ways, and nothing but the prompt hook calls the route:

  verbatim: the owner types `decide: <text>` (or `decide <name>: <text>`)
      as the whole prompt in the supervisor session. The prompt hook relays
      <text>, the owner's own words, unchanged, to the registered worker
      target (`cardinal-storyboard investigation supervising`): relay
      source "verbatim", relay key dv_ + 16 hex. No draft, no approve step.
  proposal: the supervisor drafts (`cardinal-storyboard investigation
      propose-decision`, a local PROPOSAL dp_ + 16 hex; nothing is sent: a
      proposal never approves itself) and the owner types `approve
      dp_xxxxxxxxxxxxxxxx` (bare `approve` for the one draft made since the
      owner's previous prompt) or `reject dp_xxxxxxxxxxxxxxxx`. The hook
      relays it (relay source "proposal", relay key = the proposal id) or
      drops it.

Either way the hook posts maestro's relay-owner-decision with this
machine's API key and reports the outcome to the user and to the model. No
CLI subcommand and no MCP tool calls relay().

Trust (say exactly this, never more). The server checks that the key
belongs to a person who authors the worker's investigation, that this
person turned on relayed decisions from their signed-in web session, that
the claimed supervisor session is an author-claimed session of an
investigation the same person authors, and that the stored text is the
text whose sha256 the relay carries. It cannot tell who made the request:
the agent holds the same key and can always call the route itself. What
this plugin guarantees is narrower: it never relays a prompt it cannot
confirm a human typed in this terminal, and it refuses every prompt a model
created (scheduled tasks, /loop, peer messages, subagent prompts, task
notifications).

  Claude Code fires its prompt hook for model-created prompts too, and its
  payload says nothing about origin: in 2.1.300 a scheduled task's prompt
  and a peer session's message both arrived as BARE text, exactly like a
  typed one (only the transcript wraps a peer message). The session
  transcript is the defence: the user entry whose promptId is the hook's
  prompt_id carries origin.kind "human" only for what the person typed.
  That entry (the file itself, for a session's first prompt) is written
  after the hook returns, so the Claude hook defers
  (handle_prompt_deferred): it records the prompt as pending and starts a
  detached verifier (verify) that waits up to VERIFY_WINDOW for the entry
  and acts only when it is human-typed; anything else (another origin, not
  found in time, unreadable, unknown shape) is "not sent". The outcome is
  shown at the turn's end (at_stop) or with the next prompt, exactly once.
  Codex, Gemini CLI and Cursor give no origin signal and have no known
  peer-message or scheduled-prompt mechanism that fires their prompt hook
  (an assumption: their documentation and our payload captures show
  none): they relay synchronously (handle_prompt), trusting that their
  prompt hook sees what the person typed.

The worker's event is authority `owner_relayed` (attested by the owner's
machine), weaker than `owner_decision` (web session).

Local state, one file per supervisor session, next to its binding:

  ~/.cardinal/investigations/sessions/<sid>.proposals.json   (0600, under the binding's flock)
  {"proposals": [{id, investigation_id, to_session_id, text, sha256, answers?, refs,
                  created_at, expires_at, uncertain?}],
   "targets":   [{name?, investigation_id, to_session_id, created_at}],
   "targets_changed_at": when an existing target set last changed,
   "names_changed": {name: when that name last stopped naming its target},
   "verbatim":  [{key, investigation_id, to_session_id, sha256, at}],
   "recent":    [{id, outcome, message, prompt_sha256, at}],
   "prompt":    {at, sha, prev}                        (synchronous runtimes)
   "prompts":   [{id, at, off, human?}]                (Claude: prompts seen, origin once known)
   "pending":   [{pid, at, state, ex}]                 (Claude: prompts being confirmed; ex: a 40-char excerpt)
   "outcomes":  [{pid, message, context, at, ex, owner, model}]}  (Claude: to show once)

A proposal expires after PROPOSAL_TTL (30 min); at most MAX_PROPOSALS
pending (the oldest goes). `uncertain` marks a draft whose send got no
answer: it may have been delivered, and approving it again is safe (same
relay key). Targets (at most MAX_TARGETS; the oldest goes) are the workers
this session supervises: `supervising` registers one, propose-decision
registers its own. `verbatim` remembers the relay key of a `decide` that
was not relayed (no answer, or not enabled) for RECENT_TTL, so retyping the
same decision for the same target reuses its key: the server answers a
duplicate instead of storing it twice. `recent` keeps the last outcomes for
RECENT_TTL: `investigation proposals` lists them, and on a runtime that
re-fires its prompt hook (Gemini) the same prompt seen again within
REFIRE_WINDOW repeats a final outcome instead of acting twice.

Freshness: a bare `approve` / `reject` applies only to a draft made after
the owner's previous prompt (and before this one); a bare `decide:` is
refused when the target set changed since then, and `decide <name>:` when
that name's target did. On Claude only a prompt confirmed human counts as
the owner's previous prompt, so a scheduled prompt cannot make a draft
drafted in its own turn look fresh.

The prompt hook runs before, and independently of, owner-input capture:
owner input may be off. It refuses a subagent's prompt (a non-empty
agent_id) and a machine prompt (owner_input.normalize_prompt must reduce it
to typed text), and acts only on a whole stripped prompt matching
APPROVAL_RE or DECIDE_RE (case-insensitive). Anything else is left
untouched.

Standard library only. handle_prompt / handle_prompt_deferred never raise.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import investigation_events as ie
from . import investigation_state as ist
from . import investigation_state_sync as sync

ROUTE = "relay-owner-decision"
CAPABILITY = "owner_relay"            # capabilities.owner_relay.enabled
AUTHORITY = "owner_relayed"           # the authority the server stamps on a relayed owner.decided
SOURCE_PROPOSAL = "proposal"          # relay.source: a supervisor's draft the owner approved
SOURCE_VERBATIM = "verbatim"          # relay.source: the owner's own words (`decide: …`)
PROPOSAL_TTL = 30 * 60.0
MAX_PROPOSALS = 5
MAX_TARGETS = 5
MAX_NAMES = 10
RECENT_TTL = 30 * 60.0
MAX_RECENT = 10
MAX_VERBATIM = 5
REFIRE_WINDOW = 30.0
BUDGET = 3.0                          # wall-clock of the relay request, from the hook's start
VERIFY_WINDOW = 20.0                  # Claude: how long the verifier waits for the prompt's transcript entry
VERIFY_POLL = 0.2
VERIFY_BUDGET = 8.0                   # Claude: the verifier's relay request
PENDING_STALE = 60.0                  # a pending prompt older than this has no verifier left
STOP_WAIT = 3.0                       # the Stop hook waits this long for a pending outcome
TRANSCRIPT_SLACK = 256 * 1024         # read this much before the prompt's offset too
MAX_PROMPTS = 20
MAX_SENT = 50                         # the server's cap of relayed decisions per investigation
MAX_OUTCOMES = 10
MAX_TEXT = 4000
MAX_REFS = 20
MAX_REF = 300
ID_RE = re.compile(r"dp_[0-9a-f]{16}")                                              # fullmatch
VERBATIM_KEY_RE = re.compile(r"dv_[0-9a-f]{16}")                                    # fullmatch
NAME_RE = re.compile(r"[a-z0-9-]{1,32}")                                            # fullmatch
APPROVAL_RE = re.compile(r"(approve|reject)(?:\s+(dp_[0-9a-f]{16}))?[.!]?", re.IGNORECASE)  # fullmatch, stripped
DECIDE_RE = re.compile(r"decide(?:[ \t]+([a-z0-9-]{1,32}))?[ \t]*:(.*)", re.IGNORECASE | re.DOTALL)  # fullmatch
MAYBE_RE = re.compile(r"\s*(?:approve|reject|decide)\b", re.IGNORECASE)            # match: a cheap pre-check
ORIGIN_RE = re.compile(r"https?://[A-Za-z0-9.:-]{1,253}")                            # fullmatch
HUMAN_SOURCES = ("typed", "queued")   # transcript promptSource of a prompt the person typed (queued: while busy)
FINAL_OUTCOMES = ("relayed", "duplicate", "refused", "rejected")
URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)                    # a "?" inside one is fine
QUESTION_RE = re.compile(r"\?[\"')\]\u2019\u201d]*(?:\s|$)")                        # a sentence ending in "?"

PROPOSE_HELP = ("Draft an owner decision for the worker session you supervise. Nothing is sent: the owner approves "
                "it by typing `approve <id>` (or `reject <id>`) as a prompt in THIS session, and only then does this "
                "session's prompt hook relay it to the worker (authority owner_relayed). It uses this machine's own "
                "key, never an access grant: no --credential-file. It also registers the worker as a target "
                "(see `supervising`).")
SUPERVISING_HELP = ("Register the worker this session supervises (a target), so the owner can type `decide: <text>` "
                    "(or `decide <name>: <text>`) as a prompt here to send the worker their own words as an owner "
                    "decision. Local only; nothing is sent. Without arguments: list the targets. --remove drops one "
                    "(by --name, or by investigation and --to-session).")
INSTRUCTION = ("Show the owner this text verbatim and ask them to reply `approve {id}` or `reject {id}`. Never approve "
               "it yourself and never call the relay route: only the owner's own prompt approves.")
NOT_CAPABLE = ("this Cardinal cannot send owner decisions from here yet (it needs a newer Maestro). Type your "
               "decision in the worker's own terminal, or post it from the Investigation page on the web.")
NOT_ENABLED = "owner_relay_not_enabled"
UNSUPPORTED = NOT_CAPABLE
TOGGLE = "Accept owner decisions I send from my supervisor sessions"   # the web setting's label
CREDENTIAL = ("Cardinal: your decision looks like it contains a credential, so nothing was sent. Remove it and type "
              "the decision again; give the worker secrets through its environment, not a decision.")
QUESTION = "Cardinal: not sent: a decision is not a question; to ask the worker something, tell the supervisor."
ONE_AT_A_TIME = "Cardinal: not sent: send one decision at a time, as one paragraph (no blank line)."
UNSTORABLE_WHAT = ("a control character (such as a carriage return or escape; tab and newline are fine), an unpaired "
                   "surrogate, or an invisible direction or zero-width character")
UNSTORABLE = (f"Cardinal: not sent: the decision contains a character Cardinal does not accept: {UNSTORABLE_WHAT}. "
              "Type it again without it.")
NOT_CONFIRMED = "Cardinal: not sent: Cardinal could not confirm you typed this prompt in this terminal."
PENDING_CONTEXT = ("This prompt is a Cardinal relay command handled by a hook, not a task: acknowledge it briefly and "
                   "do nothing else.")
MAYBE_SENT = "may not have been sent: Cardinal did not answer in time or could not be reached"
NO_DRAFT = "Cardinal: no draft is waiting for your approval (drafts expire after 30 minutes); nothing was sent."
NO_WORKER = ("Cardinal: no worker is registered here yet; ask the supervisor to register the one it watches. Nothing "
             "was sent.")
SEES_IT = ("The worker sees it after its next tool call; if it is waiting for you, when it next runs (any prompt in "
           "its terminal). To change it, send another decide:; the worker sees both.")
EXCERPT = 160
_ROUTE_404S = ("investigation_not_found", "owner_decision_answer_not_found")

# The route's refusals, in words for the owner (never a raw status line or
# server text; the code goes to the debug log only).
REFUSALS = {
    "owner_relay_requires_api_key": "it needs this machine's Cardinal API key, not a web session or an access grant",
    "owner_relay_requires_user_key": ("this machine's Cardinal API key belongs to no person; reconnect it with a key "
                                      "of your own"),
    "investigation_not_found": "the worker's Investigation is not in this org",
    "owner_relay_requires_investigation_author": ("only the person who owns the worker's Investigation can send it "
                                                  "decisions, and this machine's key is not theirs"),
    "owner_relay_self": "it was addressed to this session's own Investigation",
    "owner_relay_supervisor_not_yours": "Cardinal could not confirm this supervisor session is yours",
    "owner_relay_target_not_author_session": "the worker session is not one of that Investigation's own sessions",
    "owner_relay_text_mismatch": "the text changed on the way",
    "owner_decision_answer_not_found": "the worker event it answers is not in the worker's Investigation",
    "idempotency_conflict": "this draft was already sent with different text",
    "owner_decision_limit_reached": "the worker's Investigation reached its limit of owner decisions",
    "event_limit_reached": "the worker's Investigation reached its limit of events",
    "Org mismatch": "this machine's Cardinal connection is for another org",
    "owner_relay_answers_ruling": ("it answers a supervisor's challenge or ruling, which only a decision posted from "
                                   "the Investigation page on the web can overrule"),
    "owner_relay_not_member": "this machine's key belongs to someone who is not a member of the worker's Cardinal org",
}
# event_limit_reached by scope (the body's `scope`).
LIMIT_SCOPES = {
    "owner_relayed_investigation": ("the worker's Investigation reached its limit of 50 relayed decisions; post it "
                                    "from the Investigation page on the web"),
    "owner_decision_investigation": "the worker's Investigation reached its limit of owner decisions",
}
UNAVAILABLE = "owner_relay_unavailable"   # 503: this Cardinal cannot store relayed decisions yet (certainly not stored)


# ---------------------------------------------------------------------------
# The local store
# ---------------------------------------------------------------------------

def proposals_path(home: Path, sid: str) -> Path:
    if not ie.valid_session(sid):
        raise ValueError(f"not a session id: {sid!r}")
    return ie.sessions_dir(home) / f"{sid}.proposals.json"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _entry_ok(e: Any, now: float) -> bool:
    return (isinstance(e, dict) and isinstance(e.get("id"), str) and bool(ID_RE.fullmatch(e["id"]))
            and ie.valid_investigation(e.get("investigation_id")) and ie.valid_session(e.get("to_session_id"))
            and isinstance(e.get("text"), str) and 0 < len(e["text"]) <= MAX_TEXT
            and e.get("sha256") == sha256_text(e["text"])
            and _num(e.get("created_at")) and _num(e.get("expires_at")) and now < e["expires_at"]
            and isinstance(e.get("refs", []), list)
            and (e.get("answers") is None or (isinstance(e["answers"], int) and not isinstance(e["answers"], bool))))


def _target_ok(t: Any) -> bool:
    return (isinstance(t, dict) and ie.valid_investigation(t.get("investigation_id"))
            and ie.valid_session(t.get("to_session_id")) and _num(t.get("created_at"))
            and (t.get("name") is None or (isinstance(t["name"], str) and bool(NAME_RE.fullmatch(t["name"])))))


def _verbatim_ok(v: Any, now: float) -> bool:
    return (isinstance(v, dict) and isinstance(v.get("key"), str) and bool(VERBATIM_KEY_RE.fullmatch(v["key"]))
            and ie.valid_investigation(v.get("investigation_id")) and ie.valid_session(v.get("to_session_id"))
            and isinstance(v.get("sha256"), str) and _num(v.get("at")) and 0 <= now - v["at"] < RECENT_TTL)


def _recent_ok(r: Any, now: float) -> bool:
    return (isinstance(r, dict) and isinstance(r.get("id"), str)
            and bool(ID_RE.fullmatch(r["id"]) or VERBATIM_KEY_RE.fullmatch(r["id"]))
            and isinstance(r.get("message"), str) and _num(r.get("at")) and 0 <= now - r["at"] < RECENT_TTL)


def _read(home: Path, sid: str, now: float) -> dict:
    """The store, without expired or malformed entries (an unreadable file
    is empty)."""
    try:
        data = json.loads(proposals_path(home, sid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    def kept(name: str, ok: Callable[[Any], bool], cap: int) -> list:
        got = data.get(name)
        return [x for x in got if ok(x)][-cap:] if isinstance(got, list) else []

    prompt = data.get("prompt")
    prompt = prompt if isinstance(prompt, dict) and _num(prompt.get("at")) and _num(prompt.get("prev")) \
        and isinstance(prompt.get("sha"), str) else {}
    # Drafts that expired: their ids, kept for RECENT_TTL so "approve <id>"
    # can say it expired.
    gone = [{"id": e["id"], "at": e["expires_at"]} for e in data.get("proposals") or []
            if isinstance(e, dict) and isinstance(e.get("id"), str) and ID_RE.fullmatch(e["id"])
            and _num(e.get("expires_at")) and now >= e["expires_at"]] \
        if isinstance(data.get("proposals"), list) else []
    expired = kept("expired", lambda x: isinstance(x, dict) and isinstance(x.get("id"), str)
                   and bool(ID_RE.fullmatch(x["id"])) and _num(x.get("at")), MAX_RECENT)
    expired = [x for x in expired + gone if 0 <= now - x["at"] < RECENT_TTL][-MAX_RECENT:]
    changed, names = data.get("targets_changed_at"), data.get("names_changed")
    return {"proposals": kept("proposals", lambda e: _entry_ok(e, now), MAX_PROPOSALS),
            "expired": expired,
            "targets": kept("targets", _target_ok, MAX_TARGETS),
            "targets_changed_at": changed if _num(changed) else 0,
            "names_changed": dict(sorted(((k, v) for k, v in names.items() if isinstance(k, str)
                                          and NAME_RE.fullmatch(k) and _num(v)), key=lambda kv: kv[1])[-MAX_NAMES:])
            if isinstance(names, dict) else {},
            "verbatim": kept("verbatim", lambda v: _verbatim_ok(v, now), MAX_VERBATIM),
            "recent": kept("recent", lambda r: _recent_ok(r, now), MAX_RECENT),
            "prompt": prompt,
            "prompts": kept("prompts", _prompt_ok, MAX_PROMPTS),
            "pending": kept("pending", _pending_ok, MAX_OUTCOMES),
            "outcomes": kept("outcomes", lambda o: _outcome_ok(o, now), MAX_OUTCOMES),
            "sent": kept("sent", lambda x: isinstance(x, dict) and ie.valid_investigation(x.get("investigation_id"))
                         and isinstance(x.get("seq"), int) and not isinstance(x["seq"], bool), MAX_SENT)}


def _sent(store: dict, outcome: dict) -> None:
    """Remember a relayed decision this session's hook sent (its event), so
    the supervisor can tell it from a relayed decision it did not send."""
    if isinstance(outcome.get("seq"), int) and outcome.get("investigation_id"):
        store["sent"] = (store["sent"] + [{"investigation_id": outcome["investigation_id"], "seq": outcome["seq"],
                                           "at": outcome["at"]}])[-MAX_SENT:]


def sent_by(home: Path, sid: Any) -> set:
    """{(investigation_id, seq)} of the relayed decisions supervisor session
    `sid`'s prompt hook sent (empty when unknown). Never raises."""
    try:
        if not ie.valid_session(sid):
            return set()
        return {(x["investigation_id"], x["seq"]) for x in _read(home, sid, time.time())["sent"]}
    except Exception:
        return set()


def _prompt_ok(p: Any) -> bool:
    return (isinstance(p, dict) and (p.get("id") is None or (isinstance(p["id"], str) and 0 < len(p["id"]) <= 128))
            and _num(p.get("at")) and (p.get("off") is None or (isinstance(p["off"], int) and p["off"] >= 0))
            and p.get("human") in (None, True, False))


def _pending_ok(p: Any) -> bool:
    return (isinstance(p, dict) and isinstance(p.get("pid"), str) and 0 < len(p["pid"]) <= 128 and _num(p.get("at"))
            and p.get("state") in ("verifying", "sending") and isinstance(p.get("ex", ""), str))


def _outcome_ok(o: Any, now: float) -> bool:
    return (isinstance(o, dict) and isinstance(o.get("message"), str) and isinstance(o.get("context"), str)
            and _num(o.get("at")) and 0 <= now - o["at"] < RECENT_TTL
            and isinstance(o.get("owner"), bool) and isinstance(o.get("model"), bool)
            and not (o["owner"] and o["model"]) and isinstance(o.get("ex", ""), str))


def _write(home: Path, sid: str, store: dict) -> None:
    path = proposals_path(home, sid)
    if not any(store.values()):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    ie._ensure_dirs(home)
    ie.write_json(path, store)


@contextlib.contextmanager
def _updating(home: Path, sid: str, now: float, wait: float = 1.0):
    """The store under the session's flock, written back on a clean exit.
    Raises OSError when the lock cannot be had."""
    with ie.locked(home, sid, wait=wait) as got:
        if not got:
            raise OSError("the session's proposals are locked by another process")
        store = _read(home, sid, now)
        yield store
        _write(home, sid, store)


def has_store(home: Path, sid: str) -> bool:
    """Whether session `sid` keeps owner-relay state (it supervises: a draft
    or a target was made here). Never raises."""
    try:
        return proposals_path(home, sid).exists()
    except (ValueError, OSError):
        return False


def _expired(store: dict, pid: str) -> bool:
    """Whether draft `pid` expired lately."""
    return any(x["id"] == pid for x in store["expired"])


def pending(home: Path, sid: str, now: Optional[float] = None) -> list:
    """The unexpired proposals of session `sid`, oldest first."""
    return _read(home, sid, time.time() if now is None else now)["proposals"]


def recent(home: Path, sid: str, now: Optional[float] = None) -> list:
    """The latest outcomes of session `sid`, oldest first."""
    return _read(home, sid, time.time() if now is None else now)["recent"]


def targets(home: Path, sid: str, now: Optional[float] = None) -> list:
    """The worker targets session `sid` registered, oldest first."""
    return _read(home, sid, time.time() if now is None else now)["targets"]


def validate(investigation_id: Any, to_session_id: Any, text: Any, answers: Any = None,
             refs: Any = None) -> None:
    """Raises ValueError (in words) when the proposal cannot be relayed as
    given."""
    validate_target(investigation_id, to_session_id)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("--text is the decision, 1 to 4000 characters")
    if len(text) > MAX_TEXT:
        raise ValueError(f"--text is at most {MAX_TEXT} characters")
    if not storable(text):
        raise ValueError(f"--text contains {UNSTORABLE_WHAT}")
    if has_credential(text):
        raise ValueError("not proposed: the decision looks like it contains a credential. Remove it; the worker gets "
                         "secrets through its environment, never through an owner decision")
    if answers is not None and (not isinstance(answers, int) or isinstance(answers, bool) or answers < 1):
        raise ValueError("--answers is an event number (a positive integer)")
    refs = refs or []
    if not isinstance(refs, list) or len(refs) > MAX_REFS:
        raise ValueError(f"at most {MAX_REFS} --ref")
    for r in refs:
        if not isinstance(r, str) or not r.strip() or len(r) > MAX_REF or not storable(r):
            raise ValueError(f"each --ref is 1 to {MAX_REF} characters, without {UNSTORABLE_WHAT}")


def storable(text: str) -> bool:
    """Whether the server accepts `text` in a relay: no unpaired surrogate
    (wire_safe), no control character but tab and newline (its
    /[\u0000-\u0008\u000b-\u001f]/ rule: NUL, CR, ESC…), and none of the
    invisible characters its invalid_owner_relay rule refuses (INVISIBLE:
    direction controls, tag characters, zero-width space / word joiner /
    BOM; ZWJ and ZWNJ are fine). Normalize CRLF first (crlf)."""
    from .owner_input import wire_safe
    return wire_safe(text) == text and not CONTROL_RE.search(text) and not any(_invisible(c) for c in text)


CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f]")


def crlf(text: str) -> str:
    """A pasted CRLF (or CR) line break as a newline."""
    return text.replace("\r\n", "\n")


INVISIBLE = frozenset((0x200B, 0x200E, 0x200F, 0x061C, 0x2060, 0xFEFF))


def _invisible(c: str) -> bool:
    o = ord(c)
    return o in INVISIBLE or 0x202A <= o <= 0x202E or 0x2066 <= o <= 0x2069 or 0xE0000 <= o <= 0xE007F


def has_credential(text: str) -> bool:
    """Whether the credential scrubber would change `text` (owner input's
    scrubbed_of): a decision never carries a secret."""
    from .owner_input import scrubbed_of
    return scrubbed_of(text)[0] != text


def validate_target(investigation_id: Any, to_session_id: Any, name: Any = None) -> None:
    if not ie.valid_investigation(investigation_id):
        raise ValueError(f"not an investigation id: {investigation_id!r} (inv_ followed by 24 hex characters)")
    if not ie.valid_session(to_session_id):
        raise ValueError("--to-session is the worker's session id (letters, digits, _ and -; at most 128)")
    if name is not None and not (isinstance(name, str) and NAME_RE.fullmatch(name)):
        raise ValueError("--name is 1 to 32 characters: a-z, 0-9 and -")


def _targets_sig(ts: list) -> list:
    return sorted((t["investigation_id"], t["to_session_id"], t.get("name") or "") for t in ts)


def _targets_changed(store: dict, before: list, now: float) -> None:
    """Note when a non-empty target set changed, and when a name stopped
    naming its target (moved, renamed away, removed): a bare `decide:` /
    `decide <name>:` typed before then is refused, as the owner may not know
    where it goes now."""
    after = _targets_sig(store["targets"])
    if before and before != after:
        store["targets_changed_at"] = now
    now_named = {n: (i, s) for i, s, n in after if n}
    for i, s, n in before:
        if n and now_named.get(n) != (i, s):
            store["names_changed"] = dict(list({k: v for k, v in store["names_changed"].items() if k != n}.items())
                                          [-(MAX_NAMES - 1):] + [(n, now)])


def _upsert_target(store: dict, investigation_id: str, to_session_id: str, name: Optional[str],
                   now: float, replace: bool = False) -> dict:
    """The target for (investigation, session), registered or renamed
    (no name given: an existing name is kept). A name names one target
    only: one held by a target of another investigation moves only with
    `replace` (else ValueError); one held by another session of the same
    investigation moves. Keeps the newest MAX_TARGETS."""
    before = _targets_sig(store["targets"])
    holder = next((t for t in store["targets"] if name is not None and t.get("name") == name
                   and t["investigation_id"] != investigation_id), None)
    if holder is not None and not replace:
        raise ValueError(f"--name {name} already names the worker session {holder['to_session_id']} of "
                         f"{holder['investigation_id']}; pass --replace to move the name here, or choose another")
    old = next((t for t in store["targets"] if t["investigation_id"] == investigation_id
                and t["to_session_id"] == to_session_id), None)
    entry = {"investigation_id": investigation_id, "to_session_id": to_session_id,
             "created_at": old["created_at"] if old else now}
    keep_name = name if name is not None else (old or {}).get("name")
    if keep_name is not None:
        entry["name"] = keep_name
    store["targets"] = [t for t in store["targets"] if t is not old
                        and not (keep_name is not None and t.get("name") == keep_name)] + [entry]
    store["targets"] = store["targets"][-MAX_TARGETS:]
    _targets_changed(store, before, now)
    return entry


def add_target(home: Path, sid: str, investigation_id: str, to_session_id: str, name: Optional[str] = None,
               now: Optional[float] = None, replace: bool = False) -> dict:
    """Register (or rename) the worker target (investigation, session) of
    supervisor session `sid` (_upsert_target says when a name moves). Sends
    nothing. Raises ValueError / OSError."""
    validate_target(investigation_id, to_session_id, name)
    now = time.time() if now is None else now
    with _updating(home, sid, now, wait=2.0) as store:
        return _upsert_target(store, investigation_id, to_session_id, name, now, replace)


def remove_target(home: Path, sid: str, *, name: Optional[str] = None, investigation_id: Optional[str] = None,
                  to_session_id: Optional[str] = None, now: Optional[float] = None) -> list:
    """Drop the targets named `name`, or matching (investigation_id,
    to_session_id); the dropped ones. Raises OSError when locked."""
    now = time.time() if now is None else now

    def hit(t: dict) -> bool:
        if name is not None:
            return t.get("name") == name
        return t["investigation_id"] == investigation_id and (to_session_id is None
                                                              or t["to_session_id"] == to_session_id)

    with _updating(home, sid, now) as store:
        before = _targets_sig(store["targets"])
        gone = [t for t in store["targets"] if hit(t)]
        store["targets"] = [t for t in store["targets"] if not hit(t)]
        _targets_changed(store, before, now)
        return gone


def propose(home: Path, sid: str, investigation_id: str, to_session_id: str, text: str, *,
            answers: Optional[int] = None, refs: Optional[list] = None, now: Optional[float] = None) -> dict:
    """Store a proposal of supervisor session `sid` (and register its
    target) and return it. Sends nothing. Raises ValueError (invalid input)
    or OSError (the store is locked or cannot be written). A CRLF in `text`
    is stored as a newline."""
    text = crlf(text) if isinstance(text, str) else text
    validate(investigation_id, to_session_id, text, answers, refs)
    now = time.time() if now is None else now
    with _updating(home, sid, now, wait=2.0) as store:
        taken = {e["id"] for e in store["proposals"]} | {r["id"] for r in store["recent"]}
        pid = "dp_" + secrets.token_hex(8)
        while pid in taken:
            pid = "dp_" + secrets.token_hex(8)
        entry: dict = {"id": pid, "investigation_id": investigation_id, "to_session_id": to_session_id,
                       "text": text, "sha256": sha256_text(text), "refs": list(refs or []),
                       "created_at": now, "expires_at": now + PROPOSAL_TTL}
        if answers is not None:
            entry["answers"] = answers
        store["proposals"] = (store["proposals"] + [entry])[-MAX_PROPOSALS:]
        _upsert_target(store, investigation_id, to_session_id, None, now)
        return entry


def withdraw(home: Path, sid: str, pid: str, now: Optional[float] = None) -> Optional[dict]:
    """Remove pending proposal `pid` (nothing is sent); the entry, or None
    when it is not pending. Raises OSError when the store is locked."""
    return _take(home, sid, pid, time.time() if now is None else now)


def _take(home: Path, sid: str, pid: str, now: float, outcome: Optional[dict] = None) -> Optional[dict]:
    with _updating(home, sid, now) as store:
        entry = next((e for e in store["proposals"] if e["id"] == pid), None)
        store["proposals"] = [e for e in store["proposals"] if e["id"] != pid]
        if outcome is not None:
            store["recent"] = (store["recent"] + [outcome])[-MAX_RECENT:]
            _sent(store, outcome)
        return entry


def _note(home: Path, sid: str, outcome: dict, now: float, verbatim: Optional[dict] = None,
          forget_key: Optional[str] = None) -> None:
    """Remember an outcome, and the relay key of an unsent `decide`
    (verbatim) or forget one (forget_key). Never raises."""
    with contextlib.suppress(Exception):
        with _updating(home, sid, now) as store:
            store["recent"] = (store["recent"] + [outcome])[-MAX_RECENT:]
            _sent(store, outcome)
            if verbatim is not None or forget_key is not None:
                key = verbatim["key"] if verbatim is not None else forget_key
                store["verbatim"] = [v for v in store["verbatim"] if v["key"] != key]
                if verbatim is not None:
                    store["verbatim"] = (store["verbatim"] + [verbatim])[-MAX_VERBATIM:]


# ---------------------------------------------------------------------------
# The route (called only by handle_prompt, i.e. the prompt hook)
# ---------------------------------------------------------------------------

def relay_body(entry: dict, *, relay_key: str, source: str, supervisor_session_id: str,
               supervisor_investigation_id: str, prompt_sha256: str, approved_at: str, client: str) -> dict:
    payload: dict = {"text": entry["text"]}
    if entry.get("answers") is not None:
        payload["answers"] = entry["answers"]
    if entry.get("refs"):
        payload["refs"] = list(entry["refs"])
    return {"investigation_id": entry["investigation_id"], "to_session_id": entry["to_session_id"],
            "relay_key": relay_key, "payload": payload,
            "relay": {"supervisor_session_id": supervisor_session_id,
                      "supervisor_investigation_id": supervisor_investigation_id,
                      "proposal_sha256": sha256_text(entry["text"]), "prompt_sha256": prompt_sha256,
                      "approved_at": approved_at, "source": source},
            "client": client}


def relay(conn: dict, body: dict, *, client: str, opener=None, timeout: float = BUDGET) -> dict:
    """relay-owner-decision: {event, duplicate}. Only with this machine's
    key, never a grant token. Raises sync.ServerError (a refusal) or
    ist.FetchError (network, or an answer without the event)."""
    if conn.get("token") or not conn.get("key"):
        raise ist.FetchError("an owner decision is relayed only with this machine's connection")
    out = sync._post(conn, ROUTE, body, client=client, opener=opener, timeout=timeout)
    ev = out.get("event")
    if not isinstance(ev, dict) or ev.get("investigation_id") != body["investigation_id"] \
            or not isinstance(ev.get("seq"), int) or isinstance(ev.get("seq"), bool) \
            or ev.get("type") != ie.OWNER_DECIDED:
        raise ist.FetchError(f"{ROUTE} answered without the event")
    return out


def _bounded(fn: Callable[[], Any], deadline: float) -> tuple:
    """("done", value) | ("raised", exception) | ("timeout", None): fn in a
    daemon thread joined until `deadline` (time.monotonic())."""
    out: dict = {}

    def work() -> None:
        try:
            out["v"] = ("done", fn())
        except BaseException as e:   # noqa: BLE001 (reported to the caller)
            out["v"] = ("raised", e)

    t = threading.Thread(target=work, name="owner-relay", daemon=True)
    t.start()
    t.join(max(0.0, deadline - time.monotonic()))
    if t.is_alive() or "v" not in out:
        return "timeout", None
    return out["v"]


def transient(err: Exception) -> bool:
    """Kept for a retry: network, timeout, 5xx (503 owner_relay_unavailable:
    the server is not migrated yet), 408, 429."""
    if isinstance(err, ist.FetchError):
        return True
    return isinstance(err, sync.ServerError) and (err.status >= 500 or err.status in (408, 429))


def unsupported(err: "sync.ServerError") -> bool:
    code = err.body.get("error") if isinstance(err.body, dict) else None
    if err.status == 403 and code == "insufficient_scope":
        return True
    return err.status in (404, 405) and code not in _ROUTE_404S


def refusal(err: "sync.ServerError") -> tuple:
    """(the refusal in words for the owner, the code for the debug log):
    inert, one line, never the server's own text."""
    code = err.body.get("error") if isinstance(err.body, dict) else None
    safe = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", code) else f"http_{int(err.status)}"
    if unsupported(err):
        return "this Cardinal cannot send owner decisions from here yet (it needs a newer Maestro)", safe
    said = REFUSALS.get(code) if isinstance(code, str) else None
    if code == "event_limit_reached" and isinstance(err.body.get("scope"), str):
        said = LIMIT_SCOPES.get(err.body["scope"], said)
    if code == "invalid_owner_relay":
        said = f"Cardinal could not read the request (a decision cannot contain {UNSTORABLE_WHAT})"
    return said or "the reason is one this plugin version does not know", safe


def settings_link(url: Any, origin: Any = None) -> Optional[str]:
    """A settings URL safe to show: a plain http(s) URL, or a site path made
    absolute with this connection's origin; None otherwise."""
    from .investigation_bootstrap import SETTINGS_URL_RE
    if not isinstance(url, str) or not SETTINGS_URL_RE.fullmatch(url):
        return None
    if url.startswith("/"):
        return origin.rstrip("/") + url if isinstance(origin, str) and ORIGIN_RE.fullmatch(origin.rstrip("/")) \
            else None
    return url


def _off_line(link: Optional[str]) -> str:
    where = f" at {link}" if link else " in your Cardinal settings on the web (Settings, Investigations)"
    return f"decisions from your supervisor sessions are off for your account. Turn on \"{TOGGLE}\" once{where}"


def _send(conn: dict, body: dict, client: str, deadline: float, opener) -> tuple:
    """("ok", answer) | ("transient", None): no answer in time, a network
    error, 5xx, 408 or 429: it may have reached the server | ("unavailable",
    None): 503 owner_relay_unavailable, certainly not stored |
    ("not_enabled", the line for the owner) | ("refused", (reason in words,
    code))."""
    timeout = max(0.1, min(VERIFY_BUDGET, deadline - time.monotonic()))
    state, value = _bounded(lambda: relay(conn, body, client=client, opener=opener, timeout=timeout), deadline)
    if state == "done":
        return "ok", value
    err = value if state == "raised" else None
    code = err.body.get("error") if isinstance(err, sync.ServerError) and isinstance(err.body, dict) else None
    if isinstance(err, sync.ServerError) and err.status == 503 and code == UNAVAILABLE:
        return "unavailable", None
    if err is None or transient(err) or not isinstance(err, sync.ServerError):
        return "transient", None
    if err.status == 403 and code == NOT_ENABLED:
        body_url = err.body.get("settings_url") if isinstance(err.body, dict) else None
        return "not_enabled", _off_line(settings_link(body_url, conn.get("origin")))
    return "refused", refusal(err)


UNAVAILABLE_LINE = ("this Cardinal cannot store relayed decisions yet (its server needs an update); post it from the "
                    "Investigation page on the web, or try again later")


_WHY = {
    "unbound": "this session has no Cardinal Investigation yet",
    "not_author": "this session joined someone else's Investigation, so it cannot send decisions",
    "own_investigation": "it is addressed to this session's own Investigation",
    "no_connection": "this machine is not connected to Cardinal",
}


def _precheck(home: Path, sid: str, worker_inv: str, conn: Any) -> tuple:
    """(status, None) when this session cannot send to `worker_inv` now
    (_WHY says why), else (None, its binding)."""
    b = ie.read_binding(home, sid)
    if not b:
        return "unbound", None
    if b.get("is_author") is not True:
        return "not_author", None
    if b.get("investigation_id") == worker_inv:
        return "own_investigation", None
    if not (isinstance(conn, dict) and conn.get("origin") and conn.get("org") and conn.get("key")
            and not conn.get("token")):
        return "no_connection", None
    return None, b


# ---------------------------------------------------------------------------
# The prompt hook
# ---------------------------------------------------------------------------

@dataclass
class RelayResult:
    """What the prompt hook did. status: a debug code (code: the server's
    refusal code, for the debug log only). message: the outcome to show the
    user (systemMessage, or the runtime's equivalent), or None (the prompt
    is not an approval or a decision: nothing to say). context: what the
    model is given, when it differs from message."""
    status: str
    message: Optional[str] = None
    ids: list = field(default_factory=list)
    code: Optional[str] = None
    context: Optional[str] = None

    def model_text(self) -> Optional[str]:
        return self.context if self.context is not None else self.message


def maybe(prompt: Any) -> bool:
    """A cheap pre-check before anything is read: could `prompt` be one?"""
    return isinstance(prompt, str) and bool(MAYBE_RE.match(prompt))


def wanted(home: Path, sid: str, prompt: Any) -> bool:
    """Whether an adapter should call handle_prompt for this prompt: it may
    be an approval or a decision, or this session keeps relay state (every
    owner prompt is then noted, for the stale-approve rule)."""
    return maybe(prompt) or has_store(home, sid)


def match(prompt: Any) -> Optional[tuple]:
    """(verb "approve"|"reject", id or None) when the whole stripped prompt
    is an approval, else None."""
    if not isinstance(prompt, str) or len(prompt) > 200:
        return None
    m = APPROVAL_RE.fullmatch(prompt.strip())
    if not m:
        return None
    return m.group(1).lower(), (m.group(2).lower() if m.group(2) else None)


def match_decide(prompt: Any) -> Optional[tuple]:
    """(name or None, text) when the whole stripped prompt is `decide:
    <text>` / `decide <name>: <text>`; text is what follows the colon,
    without surrounding whitespace (possibly empty; a CRLF is a newline),
    else verbatim."""
    if not isinstance(prompt, str):
        return None
    m = DECIDE_RE.fullmatch(prompt.strip())
    if not m:
        return None
    return (m.group(1).lower() if m.group(1) else None), crlf(m.group(2)).strip()


def _short(sid: str) -> str:
    return sid[:8]


def _hhmm(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def excerpt(text: str, cap: int = EXCERPT) -> str:
    """`text` on one line (whitespace runs become one space) as an escaped
    JSON string of at most `cap` characters, … when cut."""
    lit, cut = ie.clipped_json_text(" ".join(text.split()), cap)
    return lit[:-1] + '…"' if cut else lit


def _who(store: dict, inv: str, to_sid: str) -> str:
    """How the owner knows the worker: its target name, and its session."""
    t = next((t for t in store["targets"] if t["investigation_id"] == inv and t["to_session_id"] == to_sid), None)
    name = t.get("name") if t else None
    return f"{name} (session {_short(to_sid)})" if name else f"the worker (session {_short(to_sid)})"


def _target_label(t: dict) -> str:
    return f"{t['name']} (session {_short(t['to_session_id'])})" if t.get("name") \
        else f"session {_short(t['to_session_id'])} (unnamed: ask the supervisor to name it)"


def _targets_line(ts: list) -> str:
    return "; ".join(_target_label(t) for t in ts)


def _drafts_line(entries: list) -> str:
    return "; ".join(f"{e['id']} {excerpt(e['text'], 40)} (expires {_hhmm(e['expires_at'])} local)" for e in entries)


def _seen_prompt(home: Path, sid: str, sha: str, runtime: str, now: float) -> float:
    """Synchronous runtimes: note an owner prompt (the store's `prompt`:
    {at, sha, prev}) and return the time of the owner's PREVIOUS prompt (0:
    none noted). A re-fired prompt hook (DEDUPE_REPEATS) for the same prompt
    keeps the previous one. Only for a session that keeps relay state. When
    the store cannot be read or locked: `now` (fail closed: no draft counts
    as fresh). Never raises."""
    from .owner_input import DEDUPE_REPEATS
    try:
        if not has_store(home, sid):
            return 0.0
        with _updating(home, sid, now) as store:
            last = store["prompt"]
            if last and runtime in DEDUPE_REPEATS and last["sha"] == sha and 0 <= now - last["at"] < REFIRE_WINDOW:
                return last["prev"]
            prev = last["at"] if last else 0.0
            store["prompt"] = {"at": now, "sha": sha, "prev": prev}
            return prev
    except Exception:
        return now


def _classify(runtime: str, prompt: Any, agent_id: Any):
    """(typed text, its scrubbed sha256), or a silent RelayResult for a
    subagent's or a machine prompt."""
    from . import owner_input
    if agent_id:
        return RelayResult("subagent")
    typed = owner_input.normalize_prompt(runtime, prompt)
    if typed is None:
        return RelayResult("not_typed")
    return typed, hashlib.sha256(owner_input.scrubbed_of(typed)[0].encode("utf-8", "replace")).hexdigest()


def _ordinary(decision: Optional[tuple], store: dict) -> bool:
    """`decide <word>: …` while no target has a name: an ordinary prompt."""
    return decision is not None and decision[0] is not None and not any(t.get("name") for t in store["targets"])


def _text_refusal(text: str) -> Optional[RelayResult]:
    """Why `decide:` text is not sent (nothing sent, nothing read), or None."""
    if not text:
        return RelayResult("bad_text", "Cardinal: not sent: `decide:` needs your decision after the colon.")
    if len(text) > MAX_TEXT:
        return RelayResult("bad_text", f"Cardinal: not sent: a decision is at most {MAX_TEXT} characters (this one "
                                       f"has {len(text)}). Shorten it and type it again.")
    if not storable(text):
        return RelayResult("bad_text", UNSTORABLE)
    if re.search(r"\n\s*\n", text):
        return RelayResult("several", ONE_AT_A_TIME)
    if QUESTION_RE.search(URL_RE.sub("", text)):
        return RelayResult("question", QUESTION)
    if has_credential(text):
        return RelayResult("credential", CREDENTIAL)
    return None


def handle_prompt(home: Path, sid: str, prompt: Any, *, runtime: str, conn: Optional[dict], client: str,
                  agent_id: Optional[str] = None, deadline: float, now: Optional[float] = None,
                  opener=None) -> RelayResult:
    """The relay half of a SYNCHRONOUS runtime's prompt hook (Codex, Gemini,
    Cursor; Claude uses handle_prompt_deferred): matches, and acts at once.
    Every owner prompt of a session with relay state is noted (freshness).
    Statuses:

      invalid / subagent / not_typed / no_match / none
                      nothing said: not an approval or a decision here (a
                      bare approve in a session that never had a draft or a
                      target; `decide <word>:` when no target has a name)
      repeat          DEDUPE_REPEATS runtimes (Gemini) only: the same prompt
                      again within REFIRE_WINDOW after a final outcome, with
                      no newer draft: the outcome is repeated, nothing is
                      sent again
      no_draft        approve / reject matched no fresh draft
      ambiguous       approve: several fresh drafts; decide: several targets
                      and no name. Says which
      unknown_id      approve: the id is not waiting (expired, rejected, sent)
      no_target / unknown_target / target_changed
                      decide: no target, none by that name, or (bare) the
                      target set changed since the owner's previous prompt
      bad_text / several / question / credential
                      decide: empty, too long, a character the server
                      refuses; a blank line; a "?"; the scrubber would change it
      rejected        removed; nothing sent
      relayed / duplicate
                      sent (201 / 200); a draft is removed
      transient       no answer, network, 5xx, 408, 429: it may have been
                      sent; a draft is kept (marked uncertain); a decide's
                      relay key is kept, so repeating it is safe
      unavailable     503 owner_relay_unavailable: not stored; a draft is kept
      not_enabled     403 owner_relay_not_enabled: kept, with the settings link
      refused         any other refusal: a draft is removed
      unbound / not_author / own_investigation / no_connection / busy
                      not sent; a draft is kept
    Never raises (error)."""
    try:
        return _handle(home, sid, prompt, runtime, conn, client, agent_id, deadline,
                       time.time() if now is None else now, opener)
    except Exception:
        return RelayResult("error")


def _handle(home, sid, prompt, runtime, conn, client, agent_id, deadline, now, opener) -> RelayResult:
    from . import owner_input
    if runtime not in owner_input.RUNTIMES or not ie.valid_session(sid):
        return RelayResult("invalid")
    got = _classify(runtime, prompt, agent_id)
    if isinstance(got, RelayResult):
        return got
    typed, typed_sha = got
    previous = _seen_prompt(home, sid, typed_sha, runtime, now)
    approval, decision = match(typed), match_decide(typed)
    if approval is None and decision is None:
        return RelayResult("no_match")
    store = _read(home, sid, now)
    if _ordinary(decision, store):
        return RelayResult("no_match")
    if runtime in owner_input.DEDUPE_REPEATS:
        # A runtime that re-fires its prompt hook for one prompt: the same
        # prompt seen again moments later repeats its final outcome.
        pid = approval[1] if approval else None
        again = next((r for r in reversed(store["recent"]) if r.get("prompt_sha256") == typed_sha
                      and 0 <= now - r["at"] < REFIRE_WINDOW and r.get("outcome") in FINAL_OUTCOMES
                      and (pid is None or r["id"] == pid)), None)
        if again is not None and not any(e["id"] == again["id"] or e["created_at"] > again["at"]
                                         for e in store["proposals"]):
            return RelayResult("repeat", again["message"], [again["id"]],
                               context=again["message"].replace(" " + SEES_IT, ""))
    return _act(home, sid, approval, decision, store, previous, now, typed_sha, conn, client, deadline, now, opener)


def _act(home, sid, approval, decision, store, previous, prompt_at, typed_sha, conn, client, deadline, now,
         opener) -> RelayResult:
    """Act on an owner's approval or decision. `previous`: when the owner's
    previous prompt was; `prompt_at`: when this one was seen."""
    if decision is not None:
        refused = _text_refusal(decision[1])
        if refused is not None:
            return refused
        return _decide(home, sid, decision, store, previous, prompt_at, typed_sha, conn, client, deadline, now,
                       opener)
    return _approve(home, sid, approval, store, previous, prompt_at, typed_sha, conn, client, deadline, now, opener)


def _decide(home, sid, decision, store, previous, prompt_at, typed_sha, conn, client, deadline, now,
            opener) -> RelayResult:
    """`decide: <text>` / `decide <name>: <text>`: the owner's own words,
    sent verbatim to the registered target."""
    name, text = decision
    ts = store["targets"]
    if name is not None:
        target = next((t for t in ts if t.get("name") == name), None)
        if target is None:
            return RelayResult("unknown_target", f"Cardinal: no worker named {name} is registered here "
                                                 f"({_targets_line(ts)}); nothing was sent.")
    elif not ts:
        return RelayResult("no_target", NO_WORKER)
    elif len(ts) > 1:
        return RelayResult("ambiguous", f"Cardinal: {len(ts)} workers are registered here; say which with "
                                        f"`decide <name>: …`: {_targets_line(ts)}. Nothing was sent.")
    else:
        target = ts[0]
    if store["targets_changed_at"] > prompt_at:
        return RelayResult("target_changed", "Cardinal: not sent: the workers registered here changed after you "
                                             f"typed this; now: {_targets_line(ts)}. Type it again to send it.")
    if name is not None and store["names_changed"].get(name, 0) > previous:
        return RelayResult("target_changed", f"Cardinal: not sent: the worker named {name} changed since your last "
                                             f"message; it is now {_target_label(target)}. Type it again to send "
                                             "it there.")
    if name is None and store["targets_changed_at"] > previous:
        again = f"as `decide {target['name']}: …`" if target.get("name") else "to send it there"
        return RelayResult("target_changed", "Cardinal: not sent: the worker registered here changed since your "
                                             f"last message; it is now {_target_label(target)}. Type it again "
                                             f"{again}.")
    who = _who(store, target["investigation_id"], target["to_session_id"])
    retry = "Type the same decide: again to retry; it will not be sent twice."
    status, b = _precheck(home, sid, target["investigation_id"], conn)
    if status:
        return RelayResult(status, f"Cardinal: not sent: {_WHY[status]}.")
    sha = sha256_text(text)
    prior = next((v for v in store["verbatim"] if v["investigation_id"] == target["investigation_id"]
                  and v["to_session_id"] == target["to_session_id"] and v["sha256"] == sha), None)
    key = prior["key"] if prior else "dv_" + secrets.token_hex(8)
    entry = {"investigation_id": target["investigation_id"], "to_session_id": target["to_session_id"], "text": text}
    body = relay_body(entry, relay_key=key, source=SOURCE_VERBATIM, supervisor_session_id=sid,
                      supervisor_investigation_id=b["investigation_id"], prompt_sha256=typed_sha,
                      approved_at=ie._now_iso(now), client=client)
    got, detail = _send(conn, body, client, deadline, opener)

    def outcome(s: str, message: str, seq: Optional[int] = None) -> dict:
        out = {"id": key, "outcome": s, "message": message, "prompt_sha256": typed_sha, "at": now,
               "investigation_id": target["investigation_id"]}
        if seq is not None:
            out["seq"] = seq
        return out

    unsent = {"key": key, "investigation_id": target["investigation_id"],
              "to_session_id": target["to_session_id"], "sha256": sha, "at": now}
    if got == "ok":
        ev, dup = detail["event"], detail.get("duplicate") is True
        message = (f"Cardinal: {'already sent' if dup else 'sent'} your decision to {who} as owner decision "
                   f"#{ev['seq']}: {excerpt(text)}. {SEES_IT}")
        status = "duplicate" if dup else "relayed"
        _note(home, sid, outcome(status, message, ev["seq"]), now, forget_key=key)
        return RelayResult(status, message, [key], context=message.replace(" " + SEES_IT, ""))
    if got in ("transient", "unavailable", "not_enabled"):
        if got == "transient":
            message = f"Cardinal: your decision for {who} {MAYBE_SENT}. Type the same decide: again; it will not " \
                      "be sent twice."
        elif got == "unavailable":
            message = f"Cardinal: not sent: {UNAVAILABLE_LINE}."
        else:
            message = f"Cardinal: not sent: {detail}; then {retry[0].lower()}{retry[1:]}"
        _note(home, sid, outcome(got, message), now, verbatim=unsent)
        return RelayResult(got, message, [key])
    reason, code = detail
    if code == "idempotency_conflict":
        reason = "a decision with different text was already sent under the same key"
    message = (f"Cardinal refused your decision for {who}: {reason}. Nothing was sent; type it in the worker's "
               "terminal instead.")
    _note(home, sid, outcome("refused", message), now, forget_key=key)
    return RelayResult("refused", message, [key], code)


def _unknown_id(home, sid, pid, store, now) -> str:
    """Why draft `pid` is not waiting, as far as this session knows."""
    r = next((r for r in reversed(store["recent"]) if r["id"] == pid), None)
    if r is not None and r.get("outcome") == "rejected":
        return f"you rejected it at {_hhmm(r['at'])}"
    if r is not None and r.get("outcome") in ("relayed", "duplicate") and isinstance(r.get("seq"), int):
        return f"it was already sent as owner decision #{r['seq']}"
    if r is not None and r.get("outcome") == "refused":
        return f"Cardinal refused it at {_hhmm(r['at'])}"
    if _expired(store, pid):
        return "it expired (drafts expire after 30 minutes)"
    return "it is not waiting here (it expired, was withdrawn, or was never made in this session)"


def _mark_uncertain(home: Path, sid: str, pid: str, outcome: dict, now: float) -> None:
    """Draft `pid`'s send got no answer: it stays, marked uncertain (it may
    have been delivered). Never raises."""
    with contextlib.suppress(Exception):
        with _updating(home, sid, now) as store:
            for e in store["proposals"]:
                if e["id"] == pid:
                    e["uncertain"] = True
            store["recent"] = (store["recent"] + [outcome])[-MAX_RECENT:]


def _approve(home, sid, approval, store, previous, prompt_at, typed_sha, conn, client, deadline, now,
             opener) -> RelayResult:
    """`approve [dp_…]` / `reject [dp_…]` of a supervisor's draft. A bare one
    applies only to a draft made after the owner's previous prompt and
    before this one."""
    verb, pid = approval
    entries = store["proposals"]
    known = has_store(home, sid)
    if pid is None:
        fresh = [e for e in entries if previous < e["created_at"] <= prompt_at]
        if not fresh:
            return RelayResult("no_draft", NO_DRAFT) if known else RelayResult("none")
        if len(fresh) > 1:
            return RelayResult("ambiguous", (
                f"Cardinal: {len(fresh)} drafts are waiting; nothing was {'sent' if verb == 'approve' else 'discarded'}. "
                f"Reply `{verb} <id>` with one of: {_drafts_line(fresh)}."), [e["id"] for e in fresh])
        entry = fresh[0]
    else:
        entry = next((e for e in entries if e["id"] == pid), None)
        if entry is None:
            left = f" Waiting: {_drafts_line(entries)}." if entries else ""
            return RelayResult("unknown_id", f"Cardinal: not sent: draft {pid}: {_unknown_id(home, sid, pid, store, now)}."
                                             f"{left}", [pid])
    pid = entry["id"]
    who = _who(store, entry["investigation_id"], entry["to_session_id"])

    def outcome(status: str, message: str, seq: Optional[int] = None) -> dict:
        out = {"id": pid, "outcome": status, "message": message, "prompt_sha256": typed_sha, "at": now,
               "investigation_id": entry["investigation_id"]}
        if seq is not None:
            out["seq"] = seq
        return out

    if verb == "reject":
        message = f"Cardinal: discarded your draft {pid} for {who}; nothing was sent."
        if entry.get("uncertain"):
            message = (f"Cardinal: discarded your draft {pid} for {who}. It may already have been delivered (an "
                       "earlier send got no answer); check the worker's Investigation.")
        try:
            _take(home, sid, pid, now, outcome("rejected", message))
        except OSError:
            return RelayResult("busy", f"Cardinal: draft {pid} could not be discarded just now; type `reject {pid}` "
                                       "again.", [pid])
        return RelayResult("rejected", message, [pid])

    retry = f"type `approve {pid}` to try again"
    status, b = _precheck(home, sid, entry["investigation_id"], conn)
    if status:
        return RelayResult(status, f"Cardinal: not sent: {_WHY[status]}. Your draft {pid} is still waiting.", [pid])
    body = relay_body(entry, relay_key=pid, source=SOURCE_PROPOSAL, supervisor_session_id=sid,
                      supervisor_investigation_id=b["investigation_id"], prompt_sha256=typed_sha,
                      approved_at=ie._now_iso(now), client=client)
    got, detail = _send(conn, body, client, deadline, opener)
    if got == "ok":
        ev, dup = detail["event"], detail.get("duplicate") is True
        answers = f" (answers #{entry['answers']})" if entry.get("answers") is not None else ""
        message = (f"Cardinal: {'already sent' if dup else 'sent'} your approved draft {pid} to {who} as owner "
                   f"decision #{ev['seq']}{answers}: {excerpt(entry['text'])}. {SEES_IT}")
        with contextlib.suppress(OSError):
            _take(home, sid, pid, now, outcome("duplicate" if dup else "relayed", message, ev["seq"]))
        return RelayResult("duplicate" if dup else "relayed", message, [pid],
                           context=message.replace(" " + SEES_IT, ""))
    if got == "transient":
        message = (f"Cardinal: your approved draft {pid} for {who} {MAYBE_SENT}. It is still waiting: type "
                   f"`approve {pid}` again; it will not be sent twice.")
        _mark_uncertain(home, sid, pid, outcome("transient", message), now)
        return RelayResult("transient", message, [pid])
    if got in ("unavailable", "not_enabled"):
        line = UNAVAILABLE_LINE if got == "unavailable" else f"{detail}; then {retry}"
        message = f"Cardinal: not sent: {line}. Your draft {pid} is still waiting."
        _note(home, sid, outcome(got, message), now)
        return RelayResult(got, message, [pid])
    reason, code = detail
    message = (f"Cardinal refused your draft {pid} for {who}: {reason}. Nothing was sent; ask the supervisor to "
               "draft it again, or type it in the worker's terminal.")
    with contextlib.suppress(OSError):
        _take(home, sid, pid, now, outcome("refused", message))
    return RelayResult("refused", message, [pid], code)


# ---------------------------------------------------------------------------
# Claude: confirm the prompt's origin in the transcript, then act (deferred)
# ---------------------------------------------------------------------------

def handle_prompt_deferred(home: Path, sid: str, prompt: Any, *, prompt_id: Any, transcript_path: Any,
                           conn: Optional[dict], client: str, agent_id: Optional[str] = None,
                           now: Optional[float] = None, spawn: Optional[Callable[[dict], bool]] = None,
                           blocked: bool = False) -> RelayResult:
    """Claude Code's prompt hook. Its prompt hook also fires for prompts no
    person typed (a scheduled task, /loop, a peer message), with no origin
    in the payload; the transcript records the origin, but only after the
    hook returns. So nothing is sent here: a matching prompt is recorded as
    pending and a detached verifier (spawn, default spawn_verifier) waits
    for its transcript entry and acts only when it is human-typed (verify).
    Text refusals and "nothing to act on" answer at once (nothing is sent).
    Outcomes of earlier prompts not shown yet are shown now: message (the
    owner's systemMessage) and context (the model's) each carry them once.
    `blocked`: another hook blocked this prompt: nothing is acted on and
    no outcome is taken (2.1.300 shows neither the owner nor the model
    anything from this hook then); a subagent's prompt takes none either.
    Never raises."""
    now = time.time() if now is None else now
    shown, told = [], []
    if not (blocked or agent_id):
        with contextlib.suppress(Exception):
            shown, told = take_outcomes(home, sid, now)
    try:
        got = RelayResult("blocked") if blocked else _defer(home, sid, prompt, prompt_id, transcript_path, conn,
                                                            client, agent_id, now, spawn or spawn_verifier)
    except Exception:
        got = RelayResult("error")
    owner = [m for m in shown + [got.message] if m]
    model = [m for m in told + [got.model_text()] if m]
    return RelayResult(got.status, "\n".join(owner) or None, got.ids, got.code, "\n".join(model) or None)


def _transcript_size(path: Any) -> Optional[int]:
    """The transcript's size now; 0 when it does not exist yet (Claude Code
    creates it only after a session's first prompt hook returns)."""
    if not isinstance(path, str) or not path:
        return None
    try:
        return os.stat(path).st_size
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        return None


def _defer(home, sid, prompt, prompt_id, path, conn, client, agent_id, now, spawn) -> RelayResult:
    if not ie.valid_session(sid):
        return RelayResult("invalid")
    got = _classify("claude", prompt, agent_id)
    if isinstance(got, RelayResult):
        return got
    typed, typed_sha = got
    pid = prompt_id if isinstance(prompt_id, str) and 0 < len(prompt_id) <= 128 else None
    off = _transcript_size(path)
    if has_store(home, sid):
        with contextlib.suppress(Exception):
            with _updating(home, sid, now) as st:   # every prompt; its origin is looked up when it matters
                st["prompts"] = (st["prompts"] + [{"id": pid, "at": now, "off": off}])[-MAX_PROMPTS:]
    approval, decision = match(typed), match_decide(typed)
    if approval is None and decision is None:
        return RelayResult("no_match")
    store = _read(home, sid, now)
    if _ordinary(decision, store):
        return RelayResult("no_match")
    if decision is not None:
        refused = _text_refusal(decision[1])
        if refused is not None:
            return refused
        if not store["targets"]:
            return RelayResult("no_target", NO_WORKER)
    elif not store["proposals"]:   # nothing to act on: says so (or nothing), sends nothing
        return _approve(home, sid, approval, store, now, now, typed_sha, conn, client, 0.0, now, None)
    if pid is None or off is None:
        return RelayResult("unconfirmed", NOT_CONFIRMED)
    if approval is None or approval[0] == "approve":
        status, _ = _precheck(home, sid, None, conn)
        if status:
            return RelayResult(status, f"Cardinal: not sent: {_WHY[status]}.")
    what = _pending_what(store, approval, decision)
    with _updating(home, sid, now) as st:
        st["pending"] = (st["pending"] + [{"pid": pid, "at": now, "state": "verifying",
                                           "ex": excerpt(typed, 40)}])[-MAX_OUTCOMES:]
    job = {"home": str(home), "sid": sid, "prompt_id": pid, "transcript_path": path, "offset": off,
           "prompt": prompt, "typed": typed, "typed_sha": typed_sha, "at": now, "conn": conn, "client": client}
    if not spawn(job):
        with contextlib.suppress(Exception):
            with _updating(home, sid, now) as st:
                st["pending"] = [p for p in st["pending"] if p["pid"] != pid]
        return RelayResult("unconfirmed", NOT_CONFIRMED)
    return RelayResult("verifying", f"Cardinal: checking this prompt was typed in this terminal before {what}…",
                       context=PENDING_CONTEXT)


def _pending_what(store: dict, approval: Optional[tuple], decision: Optional[tuple]) -> str:
    """What the verifier will do, as far as can be told now."""
    if decision is not None:
        ts, name = store["targets"], decision[0]
        t = next((t for t in ts if t.get("name") == name), None) if name else (ts[0] if len(ts) == 1 else None)
        return f"sending it to {_who(store, t['investigation_id'], t['to_session_id'])}" if t else "sending it"
    verb, pid = approval
    e = next((e for e in store["proposals"] if e["id"] == pid), None) if pid else None
    if verb == "reject":
        return f"discarding draft {pid}" if e else "discarding the draft"
    return f"sending draft {pid} to {_who(store, e['investigation_id'], e['to_session_id'])}" if e \
        else "sending the draft"


_VERIFY_CODE = ("import sys; sys.path.insert(0, sys.argv[1]); "
                "from cardinal_core import owner_relay; owner_relay._verify_detached(sys.stdin.read())")


def spawn_verifier(job: dict) -> bool:
    """Start the detached verifier (its own session; isolated Python; no
    stdout or stderr; the job, connection included, on a pipe, never on its
    command line or in its environment), like owner_input.spawn_flush.
    Whether it started. Never raises."""
    try:
        root = str(Path(__file__).resolve().parent.parent)
        proc = subprocess.Popen([sys.executable, "-I", "-c", _VERIFY_CODE, root], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                                start_new_session=True)
        try:
            proc.stdin.write(json.dumps(job).encode())
        finally:
            proc.stdin.close()
        return True
    except Exception:
        return False


def _verify_detached(raw: str) -> None:
    with contextlib.suppress(Exception):
        verify(json.loads(raw))


def _content_text(message: Any) -> Optional[str]:
    """The text of a transcript user message, None when it is not plain
    text (or holds a tool result)."""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
        return None
    texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    return "".join(t for t in texts if isinstance(t, str)) if texts else None


def entry_origin(obj: Any, prompt_id: str, typed: Optional[str] = None) -> Optional[str]:
    """For one transcript line: None when it is not the user entry of prompt
    `prompt_id` (another entry, a tool result); else "human" when the
    person typed it in this terminal (origin.kind "human", promptSource
    "typed" or "queued" or none, not isMeta, turnOrigin "human" or none;
    with `typed`, its text must be that prompt) or "other" (fails closed on
    anything else)."""
    if not isinstance(obj, dict) or obj.get("type") != "user" or obj.get("promptId") != prompt_id \
            or "toolUseResult" in obj:
        return None
    content = obj["message"].get("content") if isinstance(obj.get("message"), dict) else None
    if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
        return None
    origin = obj.get("origin")
    human = (isinstance(origin, dict) and origin.get("kind") == "human" and obj.get("isMeta") is not True
             and obj.get("promptSource", "typed") in HUMAN_SOURCES and obj.get("turnOrigin", "human") == "human")
    if human and typed is not None:
        said = _content_text(obj.get("message"))
        human = isinstance(said, str) and said.strip() == typed.strip()
    return "human" if human else "other"


def _lines_from(path: str, start: int):
    """The complete lines of `path` from byte `start` on, and where the
    unread rest begins."""
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        if start > size:
            start = 0
        f.seek(start)
        data = f.read()
    cut = data.rfind(b"\n") + 1
    return data[:cut].split(b"\n"), start + cut


def find_origin(path: str, offset: int, prompt_id: str, prompt: str, *, window: float = VERIFY_WINDOW,
                poll: float = VERIFY_POLL, sleep: Callable[[float], None] = time.sleep) -> Optional[str]:
    """Wait up to `window` seconds for prompt `prompt_id`'s user entry in
    the transcript at `path` (appended after `offset`, the size when the
    hook ran; TRANSCRIPT_SLACK before it is read too; a file not created
    yet is waited for): "human" / "other" (entry_origin), or None (not
    found in time, unreadable)."""
    deadline = time.monotonic() + window
    pos = max(0, offset - TRANSCRIPT_SLACK)
    needle = prompt_id.encode()
    while True:
        try:
            lines, pos = _lines_from(path, pos)
        except FileNotFoundError:
            lines = []
        except OSError:
            return None
        for raw in lines:
            if needle not in raw:
                continue
            try:
                got = entry_origin(json.loads(raw), prompt_id, prompt)
            except ValueError:
                continue
            if got:
                return got
        if time.monotonic() >= deadline:
            return None
        sleep(poll)


def scan_origins(path: str, offset: int, ids: set) -> dict:
    """{prompt id: "human"|"other"} for the earlier prompts `ids`, read once
    from `offset`. Raises OSError."""
    found: dict = {}
    lines, _ = _lines_from(path, max(0, offset - TRANSCRIPT_SLACK))
    for raw in lines:
        hit = next((i for i in ids if i not in found and i.encode() in raw), None)
        if hit is None:
            continue
        try:
            got = entry_origin(json.loads(raw), hit)
        except ValueError:
            continue
        if got:
            found[hit] = got
    return found


def _previous_human(home: Path, sid: str, prompt_at: float, path: str) -> float:
    """When the owner's previous HUMAN-typed prompt was (before
    `prompt_at`), from the noted prompts whose transcript entries say
    origin human. A scheduled or peer prompt never counts, so it cannot make
    a draft from its own turn look fresh. No noted prompt: 0 (nothing seen
    since this session began supervising). Noted prompts, none confirmed
    human, or any failure: `prompt_at` (fail closed: nothing is fresh)."""
    try:
        cands = [p for p in _read(home, sid, time.time())["prompts"] if p["at"] < prompt_at]
        if not cands:
            return 0.0
        todo = [p for p in cands if p.get("human") is None and p.get("id") and p.get("off") is not None]
        if todo:
            found = scan_origins(path, min(p["off"] for p in todo), {p["id"] for p in todo})
            for p in todo:
                p["human"] = found.get(p["id"]) == "human"
            with contextlib.suppress(Exception):
                with _updating(home, sid, time.time()) as st:
                    for p in st["prompts"]:
                        if p.get("human") is None and p.get("id") in found:
                            p["human"] = found[p["id"]] == "human"
        human = [p["at"] for p in cands if p.get("human") is True]
        return max(human) if human else prompt_at
    except Exception:
        return prompt_at


def _set_human(home: Path, sid: str, prompt_id: str, human: bool) -> None:
    with contextlib.suppress(Exception):
        with _updating(home, sid, time.time(), wait=5.0) as st:
            for p in st["prompts"]:
                if p.get("id") == prompt_id:
                    p["human"] = human


def _claim(home: Path, sid: str, prompt_id: str) -> bool:
    """Mark pending prompt `prompt_id` as being sent; False when it is no
    longer pending (reported as stale meanwhile) or the store is locked."""
    try:
        with _updating(home, sid, time.time(), wait=5.0) as st:
            p = next((p for p in st["pending"] if p["pid"] == prompt_id and p["state"] == "verifying"), None)
            if p is None:
                return False
            p["state"] = "sending"
            return True
    except Exception:
        return False


def _finish(home: Path, sid: str, prompt_id: str, res: RelayResult, now: float) -> None:
    """Pending prompt `prompt_id` is done: its outcome waits to be shown
    (nothing when it is no longer pending: already reported)."""
    with contextlib.suppress(Exception):
        with _updating(home, sid, now, wait=5.0) as st:
            p = next((p for p in st["pending"] if p["pid"] == prompt_id), None)
            if p is not None:
                st["pending"].remove(p)
                if res.message:
                    st["outcomes"] = (st["outcomes"] + [{"pid": prompt_id, "message": res.message,
                                                         "context": res.model_text() or res.message, "at": now,
                                                         "ex": p.get("ex", ""), "owner": False,
                                                         "model": False}])[-MAX_OUTCOMES:]


STOPPED = ("Cardinal: your last decision may not have been sent (Cardinal stopped before it got an answer). Check the "
           "worker's Investigation before sending it again.")


def verify(job: dict, *, window: float = VERIFY_WINDOW, poll: float = VERIFY_POLL,
           sleep: Callable[[float], None] = time.sleep, opener=None) -> RelayResult:
    """The detached verifier (spawn_verifier): find the prompt's transcript
    entry; act (_act) only when it is human-typed; else "not sent". The
    outcome waits in the store for take_outcomes / at_stop. Never raises."""
    try:
        home, sid, pid = Path(job["home"]), job["sid"], job["prompt_id"]
        origin = None
        with contextlib.suppress(Exception):
            origin = find_origin(job["transcript_path"], int(job["offset"]), pid, job["prompt"], window=window,
                                 poll=poll, sleep=sleep)
        _set_human(home, sid, pid, origin == "human")
        if origin != "human":
            res = RelayResult("unconfirmed", NOT_CONFIRMED)
        elif not _claim(home, sid, pid):
            return RelayResult("stale")
        else:
            now = time.time()
            previous = _previous_human(home, sid, job["at"], job["transcript_path"])
            typed = job["typed"]
            try:
                res = _act(home, sid, match(typed), match_decide(typed), _read(home, sid, now), previous, job["at"],
                           job["typed_sha"], job["conn"], job["client"], time.monotonic() + VERIFY_BUDGET, now,
                           opener)
            except Exception:
                res = RelayResult("error", "Cardinal: not sent: Cardinal failed before sending it. Type it again.")
        _finish(home, sid, pid, res, time.time())
        return res
    except Exception:
        return RelayResult("error")


def _expire_pending(store: dict, now: float) -> None:
    """A pending prompt whose verifier is gone (PENDING_STALE): reported as
    not confirmed, or as perhaps sent when it had started sending."""
    for p in [p for p in store["pending"] if now - p["at"] >= PENDING_STALE]:
        store["pending"].remove(p)
        message = STOPPED if p["state"] == "sending" else NOT_CONFIRMED
        store["outcomes"] = (store["outcomes"] + [{"pid": p["pid"], "message": message, "context": message,
                                                   "at": now, "ex": p.get("ex", ""), "owner": False,
                                                   "model": False}])[-MAX_OUTCOMES:]


def take_outcomes(home: Path, sid: str, now: float, *, model: bool = True) -> tuple:
    """(owner lines, model lines) not shown yet, now marked shown (each
    outcome reaches each once). A model line names the earlier prompt it
    answers, so it is not taken for the current one. model=False (the Stop
    hook: no model channel) leaves the model's. Raises OSError when
    locked."""
    if not has_store(home, sid):
        return [], []
    with _updating(home, sid, now) as st:
        _expire_pending(st, now)
        owner = [o["message"] for o in st["outcomes"] if not o["owner"]]
        told = [f'Earlier prompt ({o["ex"]}): {o["context"]}' if o.get("ex") else o["context"]
                for o in st["outcomes"] if model and not o["model"]]
        for o in st["outcomes"]:
            o["owner"] = True
            o["model"] = o["model"] or model
        st["outcomes"] = [o for o in st["outcomes"] if not (o["owner"] and o["model"])]
    return owner, told


def at_stop(home: Path, sid: str, *, wait: float = STOP_WAIT, sleep: Callable[[float], None] = time.sleep) -> Optional[str]:
    """The Stop hook: wait up to `wait` seconds for a pending prompt's
    outcome, then the owner lines not shown yet (None: nothing). Never
    raises."""
    try:
        if not ie.valid_session(sid) or not has_store(home, sid):
            return None
        end = time.monotonic() + wait
        while time.monotonic() < end:
            now = time.time()
            if not any(now - p["at"] < PENDING_STALE for p in _read(home, sid, now)["pending"]):
                break
            sleep(0.1)
        owner, _ = take_outcomes(home, sid, time.time(), model=False)
        return "\n".join(owner) or None
    except Exception:
        return None
