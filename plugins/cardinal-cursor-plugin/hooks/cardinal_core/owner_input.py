"""Owner input: what the session owner typed, recorded into the session's
Investigation by the adapter's prompt hook, and by nothing else.

maestro record-owner-input (same mcp-tools base as the other investigation
routes; the plugin's API key, the investigation's author only, never a
grantee): {investigation_id, session_id, payload: {text, truncated,
original_length, prompt_sha256, source: "user_prompt", turn, captured_at,
slash_command?}} -> {event, duplicate}. The server stamps type
owner_input.recorded, class owner_input and authority
owner_input_client_attested: it cannot tell this hook from the agent running
curl with the same key, so the record says "the author's plugin recorded
this as what the owner typed", never more. One row per (investigation,
session, turn): the same turn with the same prompt_sha256 is a replay (200
duplicate: true), with another one 409 owner_input_turn_conflict.

capture() is the one entry point of every runtime's prompt hook (Claude,
Codex, Cursor, Gemini) and the whole gate, in this order (see "The gate"
below): the kill switch (kill_switch: CARDINAL_OWNER_INPUT=0 in the
environment, ~/.cardinal/owner-input.json, and on Claude its settings
`env`), a subagent's prompt (a non-empty agent_id) refused, the prompt
reduced to what the owner typed (normalize_prompt; unsure: not captured),
enabled(), the runtime's connect-time consent (consent; Cursor only, which
has no per-session user-visible channel), a re-fired repeat of the same
prompt within a turn skipped (DEDUPE_REPEATS, Gemini only), disclosed() (if not yet: the
disclosure is returned for the adapter to show and this prompt is not
captured), then submit() (bounded) or, detached, the prompt queued with its
turn and posted by a detached process. Capture happens only when ALL of
these hold:
  - the session's binding says it authors its Investigation (is_author true:
    a joined investigation never gets the joiner's prompts);
  - the server advertised capabilities.owner_input.enabled: true (on
    ensure-session-investigation; refreshed once per session start). An
    older Cardinal, a capability absent or off: nothing is captured, sent or
    queued;
  - the user was told, in a message shown to them, that this session's
    prompts are recorded: the binding's `owner_input_disclosed` (set by
    mark_disclosed when the adapter shows DISCLOSURE, at SessionStart or on
    the first prompt that would otherwise be captured; that prompt itself is
    not captured). Cleared whenever the capability is seen off, so turning
    it back on mid-session discloses again;
  - on a runtime in CONSENT_REQUIRED (Cursor): the user consented in their
    own terminal (`cardinal-connect --owner-input on`, set_consent) to the
    current CONSENT_TEXT;
  - the kill switch is not set, the prompt is not a subagent's, and it
    reduces confidently to what the owner typed.

What is sent: the prompt made wire-safe (NUL removed, an unpaired surrogate
becomes U+FFFD) and scrubbed of credentials (evidence_capture.scrub_prompt);
`text` is that, cut to 32 KiB of UTF-8. `prompt_sha256` is the sha256 of the
SCRUBBED text before that cut (never of the original, so a redacted secret
cannot be brute-forced from it); `original_length` is the original
prompt's UTF-8 length. To check a record against a transcript: sha256 of
scrubbed_of(the transcript's prompt). A prompt longer than HASHED_CHARS
characters is cut to that before the scrub (bounded time) and loses its
last SCRUB_SLACK characters after it, so no secret split by the cut
survives; such a record is truncated.

Posting (submit): synchronously, with a hard wall-clock bound of ~1.5 s for
everything on the network (a worker thread joined with a timeout: slow DNS
or a trickling server count). A timeout or a transient failure
(network, timeout, 5xx, 408, 429) puts the prompt in the session's outbox
(<sid>.owner-input-outbox.json, 0600, bounded: the oldest entries go
first), posted oldest first by the next prompt or at Stop (flush), only
while the capability is enabled; later prompts queue without asking until a
short back-off passes. Anything else is dropped, never queued: 404 (an older
Cardinal without the route, or owner_input_disabled), 401, 403 and 405 drop
this prompt AND the whole outbox and turn the capability off in the binding
until the next session start; 400, 413, 422 or a 409 other than a turn
conflict drop that one entry. Every drop leaves one line in
<sid>.owner-input.log (0600, bounded): the code, the HTTP status and the
turn, never the prompt or its hash.

Turns: the binding's `owner_input_turn` counts the session's captured
prompts. A lost binding restarts it, so the server may already hold another
prompt at that turn (409 owner_input_turn_conflict, or
owner_input_key_conflict): this session's
owner_input rows are read (class owner_input; its author may) and the
entry, with everything queued after it, is renumbered past both the
server's highest turn and the conflicted one (never that turn again), at
most MAX_SHIFTS times per drain; without time for that it stays queued.

When the capability turns off (refresh, ensure, a 404), the outbox is
deleted (drop_outbox).

No agent-facing path writes owner input: no MCP tool, no cardinal-storyboard
subcommand, and investigation_events.append_event refuses the type. Standard
library only. capture(), submit() and flush() never raise.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from . import investigation_events as ie
from . import investigation_state as ist
from . import investigation_state_sync as sync

TYPE = ie.OWNER_INPUT_TYPES[0]               # owner_input.recorded
CLASS = ie.OWNER_INPUT                       # owner_input
CAPABILITY = "owner_input"                   # capabilities.owner_input.enabled
ROUTE = "record-owner-input"
SOURCE = "user_prompt"
TURN_CONFLICT = "owner_input_turn_conflict"
# 409s that mean "this session's turn is taken on the server" (a lost
# binding restarted the counter): renumbered past the server's highest turn.
TURN_CONFLICTS = (TURN_CONFLICT, "owner_input_key_conflict")

MAX_TEXT_BYTES = 32768                       # payload.text, UTF-8 bytes, after scrubbing
HASHED_CHARS = 4 * MAX_TEXT_BYTES            # a longer prompt is cut to this before the scrub (and the hash)
SCRUB_SLACK = 1024                           # then this much more, so a secret the cut split is gone
BUDGET = 1.5                                 # wall-clock of one prompt's requests (outbox + its own)
MIN_REQUEST = 0.2                            # less left than this: queue instead of asking
MAX_OUTBOX_ENTRIES = 50
MAX_OUTBOX_BYTES = 2 << 20
MAX_LOG_BYTES = 64 << 10
RETRY_FAILURE = 30.0                         # network, 5xx: later prompts queue without asking
RETRY_MAX = 600.0
MAX_SHIFTS = 3                               # renumberings per drain (each moves past the conflicted turn)
SLASH_COMMAND_RE = re.compile(r"/[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}(?=\s|$)")  # match at the prompt's start


# ---------------------------------------------------------------------------
# Paths, gate
# ---------------------------------------------------------------------------

def outbox_path(home: Path, sid: str) -> Path:
    return ie.sessions_dir(home) / f"{ie.binding_path(home, sid).stem}.owner-input-outbox.json"


def log_path(home: Path, sid: str) -> Path:
    return ie.sessions_dir(home) / f"{ie.binding_path(home, sid).stem}.owner-input.log"


def enabled(binding: Optional[dict]) -> bool:
    """Whether this session's prompts are captured into its binding's
    investigation: it authors it (is_author true) and the server advertised
    capabilities.owner_input.enabled: true. Anything else (no binding, a
    join, an older Cardinal, the capability absent or off) is off."""
    from . import investigation_bootstrap as boot
    return isinstance(binding, dict) and binding.get("is_author") is True \
        and boot.capability_enabled(binding.get("capabilities"), CAPABILITY)


DISCLOSURE = ("This session's prompts are recorded to Investigation {inv} as owner input (credentials scrubbed, "
              "up to 32 KiB); only you and grantees you authorize can read them.")


def disclosed(binding: Optional[dict]) -> bool:
    """The user was shown DISCLOSURE for this session's binding."""
    return isinstance(binding, dict) and binding.get("owner_input_disclosed") is True


def mark_disclosed(home: Path, sid: str) -> Optional[str]:
    """Record that DISCLOSURE is being shown to the user now, if this
    session records owner input (enabled): returns the disclosure text to
    show, or None (nothing recorded; show nothing). The caller must show it
    (a user-visible systemMessage). Never raises."""
    try:
        with ie.locked(home, sid, wait=1.0) as got:
            if not got:
                return None
            b = ie.read_binding(home, sid)
            if not enabled(b):
                return None
            if b.get("owner_input_disclosed") is not True:
                b["owner_input_disclosed"] = True
                ie.write_binding(home, sid, b)
            return DISCLOSURE.format(inv=b["investigation_id"])
    except Exception:
        return None


def disclosure_text(home: Path, sid: str) -> Optional[str]:
    """DISCLOSURE for this session when it records owner input (enabled),
    WITHOUT recording that it was shown: the caller shows it and then calls
    mark_disclosed once the message has been flushed to the user (capture's
    mark_disclosure=False). None: nothing to show. Never raises."""
    try:
        b = ie.read_binding(home, sid)
        return DISCLOSURE.format(inv=b["investigation_id"]) if enabled(b) else None
    except Exception:
        return None


def forget_disclosure(home: Path, sid: str) -> None:
    """The user turned owner input off (the kill switch): forget that they
    were told, so capture resumes only after they are told again. Never
    raises."""
    with contextlib.suppress(Exception):
        b = ie.read_binding(home, sid)
        if b is None or "owner_input_disclosed" not in b:
            return
        with ie.locked(home, sid, wait=1.0) as got:
            b = ie.read_binding(home, sid) if got else None
            if b is not None and b.pop("owner_input_disclosed", None) is not None:
                ie.write_binding(home, sid, b)


def drop_outbox(home: Path, sid: str) -> bool:
    """Delete this session's outbox (the capability went off, the kill
    switch is set). Whether one was there. Never raises."""
    try:
        outbox_path(home, sid).unlink()
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

def _iso_ms(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + ".%03dZ" % int((now % 1) * 1000)


def slash_command(prompt: str) -> Optional[str]:
    """The slash command the prompt starts with ("/foo", "/cardinal:connect"), or None."""
    m = SLASH_COMMAND_RE.match(prompt.lstrip()) if isinstance(prompt, str) else None
    return m.group(0) if m else None


def wire_safe(s: str) -> str:
    """`s` without what record-owner-input refuses in the text: NUL is
    removed (Postgres jsonb cannot store U+0000) and an unpaired surrogate
    becomes U+FFFD; a surrogate pair split over two code points is joined.
    Everything else stays as typed."""
    if "\x00" in s:
        s = s.replace("\x00", "")
    if any("\ud800" <= ch <= "\udfff" for ch in s):
        s = s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return s


def scrubbed_of(prompt: str) -> tuple:
    """(scrubbed, cut): the prompt made wire-safe (wire_safe) and scrubbed
    (evidence_capture.scrub_prompt). A prompt longer than HASHED_CHARS is
    cut to that before the scrub (bounded time) and then SCRUB_SLACK more
    after it, so nothing of a secret the cut split stays; cut is then True.
    prompt_sha256 is the sha256 of this text, so a record can be checked by
    computing the same from the transcript."""
    from .evidence_capture import scrub_prompt
    text, cut = wire_safe(prompt), False
    if len(text) > HASHED_CHARS:
        text, cut = text[:HASHED_CHARS], True
    text = scrub_prompt(text)
    if cut:
        text = text[:max(0, len(text) - SCRUB_SLACK)]
    return text, cut


def text_of(prompt: str) -> tuple:
    """(text, truncated, prompt_sha256): the scrubbed prompt (scrubbed_of)
    cut to MAX_TEXT_BYTES of UTF-8 without splitting a character, and the
    sha256 of the scrubbed text before that cut."""
    scrubbed, truncated = scrubbed_of(prompt)
    data = scrubbed.encode("utf-8", "replace")   # wire_safe left no lone surrogate
    sha = hashlib.sha256(data).hexdigest()
    if len(data) > MAX_TEXT_BYTES:
        data, truncated = data[:MAX_TEXT_BYTES], True
    return data.decode("utf-8", "ignore"), truncated, sha


def payload(prompt: str, turn: int, now: Optional[float] = None) -> dict:
    """record-owner-input's payload for one prompt. prompt_sha256 is of the
    scrubbed text (scrubbed_of), never of the original; original_length is
    the original prompt's UTF-8 length."""
    if not isinstance(prompt, str):
        raise ValueError("the prompt is a string")
    if not isinstance(turn, int) or isinstance(turn, bool) or turn < 1:
        raise ValueError("the turn is an integer >= 1")
    text, truncated, sha = text_of(prompt)
    out: dict = {"text": text, "truncated": truncated,
                 "original_length": len(prompt.encode("utf-8", "surrogatepass")),
                 "prompt_sha256": sha, "source": SOURCE, "turn": turn,
                 "captured_at": _iso_ms(time.time() if now is None else now)}
    cmd = slash_command(prompt)
    if cmd:
        out["slash_command"] = cmd
    return out


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

def record(conn: dict, investigation_id: str, session_id: str, body_payload: dict, *, client: str,
           opener=None, timeout: float = ie.HTTP_TIMEOUT) -> dict:
    """record-owner-input: {event, duplicate}. Raises sync.ServerError (a
    refusal) or ist.FetchError (network, or an answer without this event).
    Only with the plugin's key: never a grant token."""
    if not ie.valid_investigation(investigation_id):
        raise ist.FetchError(f"not an investigation id: {investigation_id!r}")
    if not ie.valid_session(session_id):
        raise ist.FetchError(f"not a session id: {session_id!r}")
    if conn.get("token") or not conn.get("key"):
        raise ist.FetchError("owner input is recorded only with this machine's connection")
    body = {"investigation_id": investigation_id, "session_id": session_id, "payload": body_payload}
    out = sync._post(conn, ROUTE, body, client=client, opener=opener, timeout=timeout)
    ev = out.get("event")
    if not isinstance(ev, dict) or ev.get("investigation_id") != investigation_id \
            or not isinstance(ev.get("seq"), int) or ev.get("type") != TYPE:
        raise ist.FetchError("record-owner-input answered without the event")
    return out


def disposition(err: Exception) -> str:
    """What a failed post means:
      conflict   409 owner_input_turn_conflict / owner_input_key_conflict
                 (renumber past the server's highest turn, once)
      transient  network, timeout, a malformed answer, 5xx, 408, 429: queue
      off        404 (no route, owner_input_disabled, no such investigation),
                 401, 403, 405: drop this and the whole outbox, capability off
      drop       any other refusal (400, 413, 422, a 409 cap): drop this entry"""
    if not isinstance(err, sync.ServerError):
        return "transient"
    if err.status == 409 and err.body.get("error") in TURN_CONFLICTS:
        return "conflict"
    if err.status in (408, 429) or err.status >= 500:
        return "transient"
    if err.status in (401, 403, 404, 405):
        return "off"
    return "drop"


_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")   # fullmatch: a server error code, nothing else


def _code(err: Exception) -> str:
    """The server's error code when it is a plain identifier, else
    http_<status>: never server-supplied text."""
    if isinstance(err, sync.ServerError):
        code = err.body.get("error")
        return code if isinstance(code, str) and _CODE_RE.fullmatch(code) else f"http_{err.status}"
    return "network"


def _retry_at(err: Exception, now: float) -> float:
    if isinstance(err, sync.ServerError) and err.status == 429 and err.retry_after:
        return now + min(max(err.retry_after, RETRY_FAILURE), RETRY_MAX)
    return now + RETRY_FAILURE


def _log(home: Path, sid: str, what: str, **fields: Any) -> None:
    """One JSON line in <sid>.owner-input.log (0600, at most ~64 KiB: the
    older half goes). Codes, statuses and turns only: never the prompt,
    never its hash."""
    with contextlib.suppress(Exception):
        ie._ensure_dirs(home)
        path = log_path(home, sid)
        line = json.dumps({"at": ie._now_iso(time.time()), "what": what, **fields}, default=str) + "\n"
        try:
            old = path.read_text(encoding="utf-8")
        except OSError:
            old = ""
        if len(old) + len(line) > MAX_LOG_BYTES:
            old = old[len(old) // 2:].split("\n", 1)[-1]
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(old + line)
        os.replace(str(tmp), str(path))


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------

def _entry_ok(e: Any) -> bool:
    if not isinstance(e, dict) or not isinstance(e.get("payload"), dict):
        return False
    turn = e["payload"].get("turn")
    return ie.valid_investigation(e.get("investigation_id")) and isinstance(turn, int) \
        and not isinstance(turn, bool) and turn >= 1 and isinstance(e["payload"].get("prompt_sha256"), str)


def read_outbox(home: Path, sid: str) -> dict:
    """{entries: [...oldest first], retry_after}; empty when there is none
    or it is malformed."""
    try:
        data = json.loads(outbox_path(home, sid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    raw = data.get("entries")
    entries = [e for e in raw if _entry_ok(e)] if isinstance(raw, list) else []
    return {"entries": entries, "retry_after": ie._num(data.get("retry_after"))}


def _entry_key(e: dict) -> tuple:
    return e.get("investigation_id"), e["payload"]["turn"], e["payload"]["prompt_sha256"]


def write_outbox(home: Path, sid: str, box: dict) -> None:
    """Atomic, 0600; no entries: the file goes. Bounded: past
    MAX_OUTBOX_ENTRIES / MAX_OUTBOX_BYTES the oldest entries go (logged)."""
    entries = list(box.get("entries") or [])
    sizes = [len(json.dumps(e, indent=2)) + 8 for e in entries]   # as write_json writes it (ASCII)
    while entries and (len(entries) > MAX_OUTBOX_ENTRIES or sum(sizes) > MAX_OUTBOX_BYTES):
        gone = entries.pop(0)
        sizes.pop(0)
        _log(home, sid, "dropped", code="outbox_full", turn=gone["payload"]["turn"])
    path = outbox_path(home, sid)
    if not entries:
        with contextlib.suppress(OSError):
            path.unlink()
        return
    ie._ensure_dirs(home)
    ie.write_json(path, {"entries": entries, "retry_after": ie._num(box.get("retry_after"))})


def server_turns(conn: dict, client: str, inv: str, sid: str, deadline: float, opener=None) -> Optional[dict]:
    """{turn: prompt_sha256} of every owner_input row the server holds for
    session `sid` in `inv` (class owner_input, page by page to the end);
    None when `deadline` (time.monotonic()) comes first. Raises what
    read_events raises."""
    rows: dict = {}
    after = 0
    while True:
        t = min(ie.HTTP_TIMEOUT, deadline - time.monotonic())
        if t < MIN_REQUEST:
            return None
        page = ie.read_events(conn, inv, after=after, limit=ie.PAGE_LIMIT, client=client, opener=opener, timeout=t,
                              class_=CLASS)
        for e in page["events"]:
            p, pr = e.get("payload"), e.get("producer")
            if ie.event_class(e) == CLASS and isinstance(p, dict) and isinstance(pr, dict) \
                    and pr.get("session_id") == sid and isinstance(p.get("turn"), int):
                rows[p["turn"]] = p.get("prompt_sha256")
        if page["page_size"] < ie.PAGE_LIMIT or page["last_seq"] >= page["head_seq"]:
            return rows
        after = page["last_seq"]


def _shift(home: Path, sid: str, pending: list, rows: dict) -> bool:
    """After a turn or key conflict on pending[0]'s turn (the binding was
    lost and its counter restarted, or the turn's key is held by something
    that is not an owner_input row): renumber `pending` (the entries not yet
    sent, oldest first; mutated in place) consecutively after BOTH the
    server's highest owner_input turn for this session and the conflicted
    turn, so that turn is never tried again, keeping their order; an entry
    the server already holds at its turn with its hash keeps it (a replay).
    The binding's counter moves past them. False when the session lock is
    busy."""
    with ie.locked(home, sid, wait=0.5) as got:
        if not got:
            return False
        turn = max([max(rows) if rows else 0, pending[0]["payload"]["turn"] if pending else 0])
        for e in pending:
            p = e["payload"]
            if rows.get(p["turn"]) == p["prompt_sha256"]:
                continue
            turn += 1
            p["turn"] = turn
        b = ie.read_binding(home, sid)
        if b is not None and turn > int(ie._num(b.get("owner_input_turn"))):
            b["owner_input_turn"] = turn
            ie.write_binding(home, sid, b)
    return True


def _post_entry(conn: dict, client: str, entry: dict, inv: str, sid: str, deadline: float, opener) -> str:
    """One entry to the server: "posted", "duplicate" (a replay of a row the
    server has), "conflict" (409 owner_input_turn_conflict), "late" (no
    time left); any other failure raises."""
    t = min(ie.HTTP_TIMEOUT, deadline - time.monotonic())
    if t < MIN_REQUEST:
        return "late"
    try:
        out = record(conn, inv, sid, entry["payload"], client=client, opener=opener, timeout=t)
    except sync.ServerError as e:
        if disposition(e) == "conflict":
            return "conflict"
        raise
    return "duplicate" if out.get("duplicate") is True else "posted"


def _turn_off(home: Path, sid: str, inv: str) -> None:
    """The server does not take owner input from this session (404, 401,
    403): the outbox goes and the binding's owner_input capability is off
    until the next session start asks again."""
    drop_outbox(home, sid)
    with ie.locked(home, sid, wait=1.0) as got:
        if not got:
            return
        b = ie.read_binding(home, sid)
        if b is None or b["investigation_id"] != inv:
            return
        caps = b.get("capabilities") if isinstance(b.get("capabilities"), dict) else {}
        caps = dict(caps)
        caps[CAPABILITY] = {"enabled": False}
        b["capabilities"] = caps
        b.pop("owner_input_disclosed", None)
        ie.write_binding(home, sid, b)
    drop_outbox(home, sid)


def _drain(home: Path, sid: str, conn: dict, client: str, deadline: float, now: float, opener,
           extra: Optional[dict] = None) -> tuple:
    """(outbox empty afterwards, what happened to `extra`, the back-off a
    transient failure set or None).

    The outbox, oldest first, then `extra` (the prompt being submitted, not
    in the outbox; mutated in place if renumbered): each is posted until one
    fails transiently, time runs out or the back-off of an earlier failure
    holds. `extra`'s outcome is "posted" / "duplicate" / "dropped", or None
    (the caller queues it). Off (not enabled(), or a 404 / 401 / 403):
    everything is dropped and the outbox deleted."""
    b = ie.read_binding(home, sid)
    if not enabled(b):
        drop_outbox(home, sid)
        return True, "dropped" if extra is not None else None, None
    box = read_outbox(home, sid)
    queued = box["entries"]
    if not queued and extra is None:
        drop_outbox(home, sid)
        return True, None, None
    entries = queued + ([extra] if extra is not None else [])
    keys = [_entry_key(e) for e in queued]   # as the outbox file has them
    inv = b["investigation_id"]
    done: set = set()      # indices settled now
    spent: list = []       # their turns as sent
    retry_after = None
    shifts = 0             # renumberings this drain (at most MAX_SHIFTS)
    status = None
    if now >= box["retry_after"]:
        i = 0
        while i < len(entries):
            e = entries[i]
            if e.get("investigation_id") != inv:
                _log(home, sid, "dropped", code="rebound", turn=e["payload"]["turn"])
                done.add(i)
                if i == len(queued):
                    status = "dropped"
                i += 1
                continue
            try:
                got = _post_entry(conn, client, e, inv, sid, deadline, opener)
            except Exception as err:
                d = disposition(err)
                if d == "transient":
                    retry_after = _retry_at(err, now)
                    break
                status_code = err.status if isinstance(err, sync.ServerError) else None
                if d == "off":
                    _log(home, sid, "dropped_all", code=_code(err), status=status_code, count=len(entries) - i)
                    _turn_off(home, sid, inv)
                    return True, "dropped" if extra is not None else None, None
                _log(home, sid, "dropped", code=_code(err), status=status_code, turn=e["payload"]["turn"])
                done.add(i)
                if i == len(queued):
                    status = "dropped"
                i += 1
                continue
            if got == "conflict" and shifts < MAX_SHIFTS:
                try:
                    rows = server_turns(conn, client, inv, sid, deadline, opener)
                except Exception:
                    rows = None
                if rows is not None and _shift(home, sid, entries[i:], rows):
                    shifts += 1
                    _log(home, sid, "renumbered", turn=e["payload"]["turn"], server_top=max(rows or [0]),
                         count=len(entries) - i)
                    continue   # this entry again, with its new turn
            if got in ("late", "conflict"):
                break
            done.add(i)
            spent.append(e["payload"]["turn"])
            if i == len(queued):
                status = got
            i += 1
    if not queued:
        return True, status, retry_after   # only `extra`: the caller queues it with the back-off
    if not (done or shifts) and retry_after is None:
        return False, status, None   # nothing changed (the back-off holds, or no time): the outbox stays as it is
    with ie.locked(home, sid, wait=1.0) as got_lock:
        if not got_lock:
            return False, status, retry_after  # posted entries are replays next time (duplicate: true)
        box = read_outbox(home, sid)
        index = {k: i for i, k in enumerate(keys)}
        kept = []
        for e in box["entries"]:
            i = index.get(_entry_key(e))
            if i is None:
                kept.append(e)            # queued meanwhile by another hook
            elif i not in done:
                kept.append(queued[i])    # renumbered, perhaps
        box["entries"] = kept
        box["retry_after"] = retry_after if retry_after is not None else 0.0
        # The turns sent from here are spent: the binding's counter never
        # hands one of them out again.
        b2 = ie.read_binding(home, sid)
        if b2 is not None and spent and max(spent) > int(ie._num(b2.get("owner_input_turn"))):
            b2["owner_input_turn"] = max(spent)
            ie.write_binding(home, sid, b2)
        write_outbox(home, sid, box)
        return not box["entries"], status, retry_after


def _usable(conn: Any) -> bool:
    return isinstance(conn, dict) and bool(conn.get("origin") and conn.get("org") and conn.get("key")) \
        and not conn.get("token")


def _bounded(fn, deadline: float) -> tuple:
    """(finished, fn's result): fn run in a daemon worker thread joined until
    `deadline` (time.monotonic()), so nothing on the network (slow DNS, a
    server trickling its answer) holds the hook past it. Unfinished: the
    worker is abandoned (it dies with the hook's process; anything it posted
    is a replay next time) and the caller treats it as a transient failure.
    fn's own exception counts as finished with None."""
    out: dict = {}

    def work() -> None:
        try:
            out["v"] = fn()
        except Exception:
            out["v"] = None

    t = threading.Thread(target=work, name="owner-input-post", daemon=True)
    t.start()
    t.join(max(0.0, deadline - time.monotonic()))
    if t.is_alive():
        return False, None
    return True, out.get("v")


def flush(home: Path, sid: str, conn: dict, client: str, *, deadline: Optional[float] = None,
          now: Optional[float] = None, opener=None) -> bool:
    """Post this session's queued owner input, oldest first (at Stop),
    while the capability is enabled; when it is not, the outbox is deleted.
    Bounded by `deadline` in wall-clock time (_bounded). Returns True when
    the outbox is empty afterwards. Never raises."""
    try:
        if not ie.valid_session(sid) or not outbox_path(home, sid).exists():
            return True
        b = ie.read_binding(home, sid)
        if not enabled(b):
            drop_outbox(home, sid)
            return True
        if not _usable(conn):
            return False
        deadline = time.monotonic() + BUDGET if deadline is None else deadline
        now = time.time() if now is None else now
        done, got = _bounded(lambda: _drain(home, sid, conn, client, deadline, now, opener), deadline)
        if not done:
            _hold_back(home, sid, now + RETRY_FAILURE)
            return False
        return bool(got and got[0])
    except Exception:
        return False


def _hold_back(home: Path, sid: str, until: float) -> None:
    """A timed-out drain: later prompts queue without asking until `until`."""
    with contextlib.suppress(Exception):
        with ie.locked(home, sid, wait=0.2) as got:
            if got:
                box = read_outbox(home, sid)
                if box["entries"]:
                    box["retry_after"] = max(box["retry_after"], until)
                    write_outbox(home, sid, box)


def submit(home: Path, sid: str, prompt: str, conn: dict, client: str, *, budget: float = BUDGET,
           now: Optional[float] = None, opener=None, started: Optional[float] = None) -> str:
    """Record one owner prompt of session `sid` (the prompt hook). Returns a
    code for the debug log (never the prompt):

      posted / duplicate   recorded (a replay counts)
      queued               in the outbox (a transient failure or a timeout, the
                           back-off of an earlier one, no time, a turn
                           conflict that could not be recovered now)
      dropped              refused for good (logged by code); nothing queued
      unbound / not_author / capability_off / not_disclosed / no_connection /
      busy / invalid       nothing recorded, nothing queued

    Everything on the network finishes by started + budget (_bounded). The
    turn is taken from the binding (`owner_input_turn`, under the session
    lock) before anything is sent. Never raises."""
    started = time.monotonic() if started is None else started
    now = time.time() if now is None else now
    try:
        return _submit(home, sid, prompt, conn, client, started + budget, now, opener)
    except Exception:
        return "error"


def _gate(home: Path, sid: str, b: Optional[dict]) -> Optional[str]:
    """Why `b` does not capture (and its outbox goes), or None."""
    if b is None:
        return "unbound"     # no capability is known without a binding: nothing is kept
    if b.get("is_author") is not True:
        drop_outbox(home, sid)
        return "not_author"
    if not enabled(b):
        drop_outbox(home, sid)
        return "capability_off"
    if not disclosed(b):
        return "not_disclosed"
    return None


def _claim(home: Path, sid: str, prompt: str, now: float, *, enqueue: bool = False) -> tuple:
    """(entry, None), or (None, why it is not captured): the prompt's
    payload with the session's next turn, taken from the binding under the
    session lock (the binding's counter moves to it). enqueue: the entry is
    also put in the outbox under that same lock (a detached capture: the
    outbox is how it reaches the server)."""
    body = payload(prompt, 1, now)   # the scrub, outside the lock; its turn is set under it
    with ie.locked(home, sid, wait=1.0) as got:
        if not got:
            return None, "busy"
        b = ie.read_binding(home, sid)
        why = _gate(home, sid, b)
        if why:
            return None, why
        box = read_outbox(home, sid)
        top = max([int(ie._num(b.get("owner_input_turn")))] + [e["payload"]["turn"] for e in box["entries"]])
        body["turn"] = turn = top + 1
        entry = {"investigation_id": b["investigation_id"], "payload": body, "queued_at": ie._now_iso(now)}
        b["owner_input_turn"] = turn
        ie.write_binding(home, sid, b)
        if enqueue:
            box["entries"].append(entry)
            write_outbox(home, sid, box)
    return entry, None


def _submit(home, sid, prompt, conn, client, deadline, now, opener) -> str:
    if not ie.valid_session(sid) or not isinstance(prompt, str):
        return "invalid"
    why = _gate(home, sid, ie.read_binding(home, sid))
    if why:
        return why
    if not _usable(conn):
        return "no_connection"
    entry, why = _claim(home, sid, prompt, now)
    if why:
        return why
    done, got = _bounded(lambda: _drain(home, sid, conn, client, deadline, now, opener, extra=entry), deadline)
    if done and got:
        _, status, retry_after = got
    else:
        # Timed out (or failed unexpectedly): transient, with the back-off saved.
        status, retry_after = None, now + RETRY_FAILURE
    if status is not None:
        return status
    with ie.locked(home, sid, wait=1.0) as got_lock:
        if not got_lock:
            return "busy"    # never an unlocked write; the prompt is not kept
        if _gate(home, sid, ie.read_binding(home, sid)):
            return "capability_off"
        box = read_outbox(home, sid)
        if _entry_key(entry) not in {_entry_key(e) for e in box["entries"]}:
            box["entries"].append(entry)
        if retry_after is not None:
            box["retry_after"] = max(box["retry_after"], retry_after)
        write_outbox(home, sid, box)
    return "queued"


# ---------------------------------------------------------------------------
# The gate: every runtime's prompt hook (capture), the kill switch, consent
# ---------------------------------------------------------------------------

RUNTIMES = ("claude", "codex", "cursor", "gemini", "copilot")
RUNTIME_NAMES = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor", "gemini": "Gemini CLI",
                 "copilot": "Copilot CLI"}
KILL_ENV = "CARDINAL_OWNER_INPUT"
OFF_VALUES = ("0", "false", "off", "no")
KILL_FILE = Path(".cardinal") / "owner-input.json"   # under the user's home: {"<runtime>": "off"}
OPT_OUT = "Set CARDINAL_OWNER_INPUT=0 to turn this off on this machine."

# A runtime with no user-visible, non-blocking message per session (Cursor:
# CORE_GAPS.md; Copilot CLI: sessionStart context reaches only the model and
# userPromptSubmitted output is dropped) cannot show DISCLOSURE before the first capture: it captures
# only after the user consented, in their own terminal, to CONSENT_TEXT
# (`cardinal-connect --owner-input on`). The consent is versioned by the
# text's sha256: changing the text asks again.
CONSENT_REQUIRED = frozenset({"cursor", "copilot"})
CONSENT_TEXT = ("Cardinal records the prompts you type in each {runtime} session to that session's Cardinal "
                "Investigation as owner input (credentials scrubbed, up to 32 KiB); only you and grantees you "
                "authorize can read them. Run `cardinal-connect --owner-input off` to stop.")

# Runtimes whose prompt hook may fire again for a prompt already seen this
# turn: the same scrubbed prompt again before the turn ends, and within
# REPEAT_WINDOW seconds of when it was first seen, is skipped. Gemini only:
# gemini-cli 0.50.0 fires BeforeAgent once per prompt_id (its loop recovery
# reuses the id), but that guard lives in per-prompt state it drops and
# re-creates. Claude, Codex and Cursor fire their prompt hook once per
# prompt, so an owner retyping a prompt (after an interrupt, no turn end) is
# captured again. The window bounds the skip on Gemini too: the same prompt
# typed again later, after a turn whose end was missed, is recorded.
DEDUPE_REPEATS = frozenset({"gemini"})
REPEAT_WINDOW = 30.0

# Prompts a runtime submits by itself, never a turn the owner took.
MACHINE_PROMPTS = {
    # Claude Code re-enters UserPromptSubmit with a synthetic body when a
    # background task finishes ("<task-notification>…" / "task-notification …").
    "claude": (re.compile(r"^\s*<?task-notification[\s>]", re.IGNORECASE),),
    # gemini-cli's loop recovery sends this as the request (_recoverFromLoop),
    # and /init submits its GEMINI.md template (performInit, gemini-cli
    # 0.63.0 packages/core/src/commands/init.ts). An MCP server's prompt
    # (McpPromptLoader) is submitted as JSON.stringify(text): see
    # _is_json_string, applied in normalize_prompt.
    "gemini": (
        re.compile(r"^\s*System: Potential loop detected\b"),
        re.compile(r"^\s*You are an AI agent that brings the power of Gemini directly into the terminal\. "
                   r"Your task is to analyze the current directory and generate a comprehensive GEMINI\.md file"),
    ),
    # Codex submits these template prompts for /review's presets and /init's
    # AGENTS.md (captured from codex-cli 0.160.1 / 0.162.1 UserPromptSubmit
    # payloads; no agent_id on them). A /review with custom instructions
    # submits exactly what the owner typed, and is recorded.
    "codex": (
        re.compile(r"^Review the current code changes \(staged, unstaged, and untracked files\) "
                   r"and provide prioritized findings\.$"),
        re.compile(r"^Review the code changes (?:introduced by commit|against the base branch) "
                   r"[\s\S]*Provide prioritized, actionable findings\.$"),
        re.compile(r"^Generate a file named AGENTS\.md that serves as a contributor guide for this repository\."),
    ),
}

# gemini-cli wraps hook context in <hook_context>…</hook_context> and inlines
# @file references between REFERENCE_CONTENT_START and _END, always appended
# after the typed text (atCommandProcessor). A referenced file may itself
# contain either marker, so the section is cut from its first start marker to
# the END of the prompt, never to the first end marker (which could be inside
# a file and leak the rest of it).
HOOK_CONTEXT_RE = re.compile(r"<hook_context>[\s\S]*?</hook_context>")
REFERENCE_CONTENT_START = "--- Content from referenced files ---"
REFERENCE_CONTENT_END = "--- End of content ---"
_GEMINI_MARKERS = ("<hook_context>", "</hook_context>", REFERENCE_CONTENT_START, REFERENCE_CONTENT_END)


def _cut_referenced_files(s: str) -> str:
    """`s` without gemini-cli's inlined @file section: everything from the
    first "\n--- Content from referenced files ---" (or the start, when `s`
    begins with it) to the end."""
    if s.lstrip().startswith(REFERENCE_CONTENT_START):
        return ""
    i = s.find("\n" + REFERENCE_CONTENT_START)
    return s if i < 0 else s[:i]

DETACHED_BUDGET = 8.0   # wall-clock of a detached process's posts (capture(detached=True))


def _off(v: Any) -> bool:
    return v is not None and not isinstance(v, (dict, list)) and str(v).strip().lower() in OFF_VALUES


def _settings_env(path: Path) -> dict:
    try:
        env = json.loads(path.read_text(encoding="utf-8")).get("env")
    except (OSError, ValueError, AttributeError):
        return {}
    return env if isinstance(env, dict) else {}


def kill_switch_path(home: Path) -> Path:
    return home / KILL_FILE


def kill_switch(runtime: str, home: Path, environ: Mapping, *, cwd: Optional[str] = None) -> bool:
    """Whether the user turned owner input off for `runtime` on this
    machine. Any ONE of these counts (values 0 / false / off / no, of any
    JSON type: `false` and `0` count):
      - CARDINAL_OWNER_INPUT in `environ`;
      - ~/.cardinal/owner-input.json {"<runtime>": "off"} (for GUI apps that
        never see a shell's environment); a file there that is not a JSON
        object counts as off;
      - Claude only: CARDINAL_OWNER_INPUT in ~/.claude/settings.json `env`,
        or in the project's .claude/settings.json / settings.local.json
        `env` (the project: `cwd`, and $CLAUDE_PROJECT_DIR in `environ`). A
        repo's settings `env` can override a shell export, so ~/.claude
        is the reliable place.
    Set, the prompt hook captures nothing, the outbox is deleted instead of
    flushed and nothing says prompts are recorded. Cannot tell (any error):
    True, never capture. Never raises."""
    try:
        if _off(environ.get(KILL_ENV)):
            return True
        try:
            data = json.loads(kill_switch_path(home).read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        if not isinstance(data, dict) or _off(data.get(runtime)):
            return True
        if runtime != "claude":
            return False
        files = [home / ".claude" / "settings.json"]
        for d in sorted({str(x) for x in (cwd, environ.get("CLAUDE_PROJECT_DIR")) if isinstance(x, str) and x}):
            files += [Path(d) / ".claude" / "settings.json", Path(d) / ".claude" / "settings.local.json"]
        return any(_off(_settings_env(f).get(KILL_ENV)) for f in files)
    except Exception:
        return True   # cannot tell: off, never capture


def consent_path(runtime: str, home: Path) -> Path:
    """~/.<runtime>/cardinal/owner-input-consent.json."""
    from .paths import AgentPaths
    return AgentPaths(home / f".{runtime}").owner_input_consent_path


def consent_text(runtime: str) -> str:
    """What the user consents to (print it in full where they consent)."""
    return CONSENT_TEXT.format(runtime=RUNTIME_NAMES.get(runtime, runtime))


def _consent_sha(runtime: str) -> str:
    return hashlib.sha256(consent_text(runtime).encode("utf-8")).hexdigest()


def consent(runtime: str, home: Path) -> bool:
    """Whether `runtime` may capture as far as consent goes: True for a
    runtime that discloses per session instead (not in CONSENT_REQUIRED);
    for one in it, only when its consent file says on for the CURRENT
    consent_text (another text: asked again). Never raises."""
    if runtime not in CONSENT_REQUIRED:
        return True
    try:
        data = json.loads(consent_path(runtime, home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("on") is True and data.get("text_sha256") == _consent_sha(runtime)


def set_consent(runtime: str, home: Path, on: bool) -> None:
    """Record the user's answer, given in their own terminal after
    consent_text(runtime) was printed in full: {on, text_sha256, at}, 0600.
    Raises ValueError for an unknown runtime and OSError when it cannot be
    written (the terminal command says so)."""
    if runtime not in RUNTIMES:
        raise ValueError(f"unknown runtime: {runtime!r}")
    from .paths import atomic_write_secret
    data = {"on": bool(on), "text_sha256": _consent_sha(runtime), "at": ie._now_iso(time.time())}
    atomic_write_secret(consent_path(runtime, home), json.dumps(data, indent=2) + "\n")


def disclosure_message(runtime: str, text: str) -> str:
    """The user-visible disclosure: DISCLOSURE's text (mark_disclosed) and
    how to turn it off on this runtime."""
    if runtime == "claude":
        return f"Cardinal: {text} {OPT_OUT}"
    return (f"Cardinal: {text} Set CARDINAL_OWNER_INPUT=0, or put {{\"{runtime}\": \"off\"}} in "
            "~/.cardinal/owner-input.json, to turn this off on this machine.")


def is_machine_prompt(runtime: str, prompt: Any) -> bool:
    """A prompt `runtime` submitted by itself (MACHINE_PROMPTS)."""
    return isinstance(prompt, str) and any(r.match(prompt) for r in MACHINE_PROMPTS.get(runtime, ()))


def _is_json_string(s: str) -> bool:
    """`s` (stripped) is a JSON string literal: how gemini-cli submits an MCP
    server's prompt (McpPromptLoader: submit_prompt with
    JSON.stringify(text)), never what the owner typed."""
    s = s.strip()
    if len(s) < 2 or not (s.startswith('"') and s.endswith('"')):
        return False
    try:
        return isinstance(json.loads(s), str)
    except ValueError:
        return False


def normalize_prompt(runtime: str, prompt: Any) -> Optional[str]:
    """What the owner typed, from the text `runtime`'s prompt hook got, or
    None when that cannot be told confidently (then nothing is captured):
      - not a string, or only whitespace: None;
      - gemini: every <hook_context>…</hook_context> block (context hooks
        added; in -p mode the plugin's own SessionStart context) is removed,
        then the inlined @file section: from the first "\n--- Content from
        referenced files ---" to the END of the prompt (gemini-cli always
        appends it after the typed text; a file's contents may hold either
        marker). The rest is stripped of surrounding whitespace; any marker
        left over (an unpaired or nested hook_context, a stray "--- End of
        content ---"): None;
      - gemini: a prompt that is a JSON string literal (an MCP server's
        prompt, which gemini-cli submits as JSON.stringify(text)): None;
      - a machine prompt of the runtime (MACHINE_PROMPTS: Claude's task
        notifications, Gemini's loop-recovery request and /init template):
        None.
    Anything else is returned unchanged."""
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    if runtime == "gemini":
        cut = _cut_referenced_files(HOOK_CONTEXT_RE.sub("", prompt))
        if any(m in cut for m in _GEMINI_MARKERS):
            return None
        if cut != prompt:
            prompt = cut.strip()
            if not prompt:
                return None
        if _is_json_string(prompt):
            return None
    if is_machine_prompt(runtime, prompt):
        return None
    return prompt


def _repeat(home: Path, sid: str, sha: str, now: float) -> Optional[bool]:
    """Whether `sha` (a scrubbed prompt's) is the prompt this session's hook
    already saw in this turn (since turn_ended) less than REPEAT_WINDOW
    seconds before `now` (wall clock); if not, it is remembered as this
    turn's (the binding's owner_input_last, with when). None: the lock is
    busy or the session unbound (capture nothing)."""
    with ie.locked(home, sid, wait=1.0) as got:
        if not got:
            return None
        b = ie.read_binding(home, sid)
        if b is None:
            return None
        last = b.get("owner_input_last")
        if isinstance(last, dict) and last.get("sha") == sha and last.get("ended") is not True \
                and 0 <= now - ie._num(last.get("at")) < REPEAT_WINDOW:
            return True
        b["owner_input_last"] = {"sha": sha, "ended": False, "at": now}
        ie.write_binding(home, sid, b)
        return False


def turn_ended(home: Path, sid: str) -> None:
    """The session's turn ended (investigation_turn.at_turn_end): the same
    prompt typed again is a new owner turn, no longer a re-fire. Never
    raises."""
    with contextlib.suppress(Exception):
        b = ie.read_binding(home, sid)
        last = b.get("owner_input_last") if b else None
        if not isinstance(last, dict) or last.get("ended") is True:
            return
        with ie.locked(home, sid, wait=0.5) as got:
            b = ie.read_binding(home, sid) if got else None
            if b is not None and isinstance(b.get("owner_input_last"), dict):
                b["owner_input_last"]["ended"] = True
                ie.write_binding(home, sid, b)


@dataclass
class CaptureResult:
    """What capture() did. status: a code for the debug log, never the
    prompt. disclosure: the user-visible message the adapter must show NOW
    (a systemMessage, or the runtime's equivalent; never model context, never
    a block), or None."""
    status: str
    disclosure: Optional[str] = None


def capture(home: Path, sid: str, prompt: Any, *, runtime: str, conn: Optional[dict], client: str,
            environ: Mapping, agent_id: Optional[str] = None, deadline: float, detached: bool = False,
            cwd: Optional[str] = None, now: Optional[float] = None, opener=None,
            mark_disclosure: bool = True) -> CaptureResult:
    """The prompt hook of every runtime: record `prompt` as owner input of
    session `sid`, or say why not. The gate, in order (the first that
    applies decides):

      invalid         an unknown runtime or an invalid session id
      disabled        kill_switch: the outbox is deleted and the disclosure
                      forgotten (removing the switch tells the user again
                      before capture resumes)
      subagent        a non-empty agent_id: a subagent's prompt (Codex
                      UserPromptSubmit carries one) is never the owner's
      no_prompt       not a string, or only whitespace
      not_typed       normalize_prompt could not reduce it to typed text
                      (or it is a machine prompt)
      unbound / not_author / capability_off
                      not enabled(); a join or a capability off deletes the
                      outbox
      no_consent      CONSENT_REQUIRED and no current consent: the outbox
                      is deleted
      repeat          DEDUPE_REPEATS (Gemini): the same scrubbed prompt
                      again before the turn ended and within REPEAT_WINDOW
                      seconds (busy: the lock could not be had)
      disclosed       not disclosed(): mark_disclosed, and `disclosure` is
                      returned for the adapter to show now; this prompt is
                      not captured (a CONSENT_REQUIRED runtime's consent
                      stands in for it: recorded, and this prompt goes on).
                      mark_disclosure=False: the disclosure is returned but
                      NOT recorded as shown; the adapter calls
                      mark_disclosed after it flushed the message to the
                      user, so a failed write never counts as told
      no_connection   no usable connection (origin, org, key; never a token)
      posted / duplicate / queued / dropped / busy
                      submit(): everything on the network finishes by
                      `deadline` (time.monotonic())
      detached        detached=True: the turn is taken and the prompt put
                      in the outbox under the session lock now; a detached
                      process (spawn_flush) posts it, so nothing here waits
                      on the network (queued: that process did not start;
                      the next prompt or turn end posts it)

    What is sent is the normalized prompt (its sha256 too). Never raises
    (error)."""
    try:
        return _capture(home, sid, prompt, runtime, conn, client, environ, agent_id, deadline, detached, cwd,
                        time.time() if now is None else now, opener, mark_disclosure)
    except Exception:
        return CaptureResult("error")


def _capture(home, sid, prompt, runtime, conn, client, environ, agent_id, deadline, detached, cwd, now,
             opener, mark_disclosure=True) -> CaptureResult:
    if runtime not in RUNTIMES or not ie.valid_session(sid):
        return CaptureResult("invalid")
    if kill_switch(runtime, home, environ, cwd=cwd):
        drop_outbox(home, sid)
        forget_disclosure(home, sid)
        return CaptureResult("disabled")
    if agent_id:
        return CaptureResult("subagent")
    if not isinstance(prompt, str) or not prompt.strip():
        return CaptureResult("no_prompt")
    typed = normalize_prompt(runtime, prompt)
    if typed is None:
        return CaptureResult("not_typed")
    b = ie.read_binding(home, sid)
    why = _gate(home, sid, b)
    if why and why != "not_disclosed":
        return CaptureResult(why)
    if not consent(runtime, home):
        drop_outbox(home, sid)
        return CaptureResult("no_consent")
    if runtime in DEDUPE_REPEATS:
        seen = _repeat(home, sid, text_of(typed)[2], now)
        if seen is None:
            return CaptureResult("busy")
        if seen:
            return CaptureResult("repeat")
    if not disclosed(b):
        # A CONSENT_REQUIRED runtime shows nothing: its consent is recorded
        # as the disclosure now and this prompt goes on.
        if mark_disclosure or runtime in CONSENT_REQUIRED:
            text = mark_disclosed(home, sid)
        else:
            text = disclosure_text(home, sid)
        if not text:
            return CaptureResult("capability_off")
        if runtime not in CONSENT_REQUIRED:
            return CaptureResult("disclosed", disclosure_message(runtime, text))
    if not _usable(conn):
        return CaptureResult("no_connection")
    if detached:
        entry, why = _claim(home, sid, typed, now, enqueue=True)
        if why:
            return CaptureResult(why)
        return CaptureResult("detached" if spawn_flush(home, sid, conn, client) else "queued")
    start = time.monotonic()
    return CaptureResult(submit(home, sid, typed, conn, client, budget=max(0.0, deadline - start), now=now,
                                opener=opener, started=start))


_DETACHED_CODE = ("import sys; sys.path.insert(0, sys.argv[1]); "
                  "from cardinal_core import owner_input; owner_input._flush_detached(sys.stdin.read())")


def spawn_flush(home: Path, sid: str, conn: dict, client: str) -> bool:
    """Start a detached process (its own session; isolated Python; no
    stdout or stderr) that posts this session's outbox (flush, within
    DETACHED_BUDGET). The connection reaches it on a pipe, never on its
    command line or in its environment. Whether it started. Never raises."""
    try:
        root = str(Path(__file__).resolve().parent.parent)
        proc = subprocess.Popen([sys.executable, "-I", "-c", _DETACHED_CODE, root], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                                start_new_session=True)
        try:
            proc.stdin.write(json.dumps({"home": str(home), "sid": sid, "conn": conn, "client": client}).encode())
        finally:
            proc.stdin.close()
        return True
    except Exception:
        return False


def _flush_detached(raw: str) -> None:
    """The detached process (spawn_flush): post the outbox. Never raises."""
    with contextlib.suppress(Exception):
        d = json.loads(raw)
        flush(Path(d["home"]), d["sid"], d["conn"], d["client"], deadline=time.monotonic() + DETACHED_BUDGET)


def flush_at_turn_end(home: Path, sid: str, deadline: float, *, runtime: str, conn: Optional[dict], client: str,
                      environ: Mapping, cwd: Optional[str] = None) -> None:
    """At the turn end (the owner_input_flush investigation_turn.at_turn_end
    is given): post this session's queued owner input with what is left
    until `deadline` (time.monotonic()), or delete it when owner input is
    off here (the kill switch, which also forgets the disclosure; no
    consent). Nothing to do without an outbox. Never raises; writes
    nothing to stdout."""
    try:
        if not ie.valid_session(sid) or not outbox_path(home, sid).exists():
            return
        if kill_switch(runtime, home, environ, cwd=cwd):
            drop_outbox(home, sid)
            forget_disclosure(home, sid)
            return
        if not consent(runtime, home):
            drop_outbox(home, sid)
            return
        if deadline - time.monotonic() < MIN_REQUEST:
            return
        flush(home, sid, conn or {}, client, deadline=deadline)
    except Exception:
        pass
