"""Runtime-neutral turn logic of a session bound to an Investigation: what is
delivered to the agent at a tool boundary, at the turn's end and at the next
prompt. No adapter wiring and no output format here: each function returns a
Delivery, and the adapter maps it onto its runtime's channels (Claude's
hooks/investigation-events.py; investigation_agent.deliver / turn_end /
prompt for Codex, Cursor and Gemini).

Authority (the cross-agent parity plan, R1): advisory text never travels as
owner input. It reaches the model only through context channels (a tool
boundary's additional context, a prompt's additional context) or, at the
turn's end, as a block (the turn continues with the text as a user-role
message) ONLY where the runtime marks that text as hook feedback and the
block does not re-fire the prompt hook, so it can never be captured as owner
input: TURN_END_BLOCK. Claude (the Stop hook's reason is shown as hook
feedback) and Codex (a <hook_prompt>-wrapped item) may block; Gemini (an
AfterAgent block comes back as plain user text and re-fires BeforeAgent with
it as the prompt) and Cursor (stop's followup_message is a user turn) never
do: what would block there is STAGED instead (<sid>.staged.json, 0600,
bounded) and delivered at the next context-capable boundary: at_tool, or
at_prompt (Gemini BeforeAgent, Cursor sessionStart).

At a tool boundary (at_tool; a subagent's gets nothing, so an event waits for
the main thread): staged items first, then the poller's inbox
(investigation_events.deliver_inbox: no network), then, only when nothing
else was delivered, the re-surfacing of an owed challenge or directive
(investigation_events.resurface; the boundary is counted either way).

At the turn's end (at_turn_end): the inbox; without one, unless the poller
read the events moments ago, one synchronous check (investigation_events.check,
bounded by the deadline); when nothing was delivered, an open directive
(supervision_stop) or else the owner-decision reminders (remind_decisions),
on the session's own org and never under a grant token; then, whatever
happened, the owner-input outbox flush (bounded by the deadline) and the
end of the turn for the owner-input repeat check (owner_input.turn_ended).
Never raises: a failure delivers nothing.
"""
from __future__ import annotations

import contextlib
import json
import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Mapping, Optional

from . import investigation_events as ie

# Whether the runtime's turn end may block with advisory text (R1, above).
TURN_END_BLOCK = {"claude": True, "codex": True, "gemini": False, "cursor": False}

MAX_STAGED = 20                    # staged items kept: past it the oldest go
MAX_STAGED_CHARS = 4 * ie.RENDER_BUDGET
MIN_CHECK = 0.5                    # a turn end's synchronous check gets at least this long
TURN_END_RESERVE = 0.4             # of the deadline, kept for the owner-input flush after the check


@dataclass
class Delivery:
    """What one boundary delivers. agent_context: model-context texts, in
    order (the adapter joins them: Claude delivers at most one); user_message:
    a user-visible text (never model context), or None; turn_end_block: the
    turn end's block text, only where TURN_END_BLOCK[runtime] (elsewhere it
    is staged and this is None)."""
    agent_context: List[str] = field(default_factory=list)
    user_message: Optional[str] = None
    turn_end_block: Optional[str] = None


def can_block(runtime: str) -> bool:
    """TURN_END_BLOCK[runtime]; an unknown runtime never blocks."""
    return TURN_END_BLOCK.get(runtime) is True


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def staged_path(home: Path, sid: str) -> Path:
    return ie.sessions_dir(home) / f"{ie.binding_path(home, sid).stem}.staged.json"


def _read_staged(home: Path, sid: str) -> list:
    try:
        data = json.loads(staged_path(home, sid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return [x for x in items if isinstance(x, str) and x] if isinstance(items, list) else []


def stage(home: Path, sid: str, text: str) -> None:
    """Keep `text` for this session's next context-capable boundary (at_tool,
    at_prompt), after what is staged already. Bounded: past MAX_STAGED items
    or MAX_STAGED_CHARS the oldest go. Never called while the session lock is
    held (it takes it). Never raises."""
    if not isinstance(text, str) or not text or not ie.valid_session(sid):
        return
    with contextlib.suppress(Exception):
        with ie.locked(home, sid, wait=1.0) as got:
            if not got:
                return
            items = _read_staged(home, sid) + [text]
            while len(items) > 1 and (len(items) > MAX_STAGED or sum(map(len, items)) > MAX_STAGED_CHARS):
                items.pop(0)
            ie._ensure_dirs(home)
            ie.write_json(staged_path(home, sid), {"items": items})


def consume_staged(home: Path, sid: str) -> List[str]:
    """This session's staged texts, oldest first; the file is removed. []
    when there are none or the lock is busy (they wait for the next
    boundary). Never raises."""
    try:
        if not ie.valid_session(sid) or not staged_path(home, sid).exists():
            return []
        with ie.locked(home, sid, wait=0.5) as got:
            if not got:
                return []
            items = _read_staged(home, sid)
            with contextlib.suppress(OSError):
                os.unlink(str(staged_path(home, sid)))
            return items
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------

def naming_reminder(home: Path, sid: str, *, repeat: bool = False) -> Optional[str]:
    """One local reminder per turn until the author states the question.

    Never read a transcript or send a prompt. Unknown status and joined
    investigations are deliberately silent. A failed naming request leaves
    the status provisional, so the next turn can retry.
    """
    try:
        with ie.locked(home, sid, wait=0) as got:
            if not got:
                return None
            b = ie.read_binding(home, sid)
            if (not b or b.get("is_author") is not True or b.get("question_status") != "provisional"
                    or (b.get("question_reminded") and not repeat)):
                return None
            b["question_reminded"] = True
            ie.write_binding(home, sid, b)
        return (
            "This session's investigation still has the default title. Once the user's task is clear, "
            "name it now with `cardinal-storyboard investigation question --session "
            f"{shlex.quote(sid)} \"<short description of the user's task>\"`. "
            "Use a concise, one-line description of the actual request, including implementation or review tasks. "
            "Do not wait for findings or a request to publish. This is your statement of the task, not owner "
            "authority. If the user has not stated a task yet, leave it provisional; if the request fails, "
            "continue the user's work and retry on a later turn."
        )
    except Exception:
        return None


def at_tool(home: Path, sid: str, org: Optional[str], runtime: str, *, is_subagent: bool) -> Delivery:
    """A tool boundary of session `sid`. A subagent's (is_subagent) gets
    nothing and changes nothing: no output, the inbox, the cursor and the
    staged items are left for the main thread. Otherwise: staged items,
    then the inbox, then (only when nothing else was delivered) one owed
    challenge or directive re-surfaced; the boundary is counted for
    re-surfacing either way. org: the connection's org; a binding of
    another org gets nothing (None: not checked). No network. Never
    raises."""
    out = Delivery()
    try:
        if is_subagent or not ie.valid_session(sid):
            return out
        b = ie.read_binding(home, sid)
        if b is None or (org is not None and b.get("org") != org):
            return out
        ie.touch_activity(home, sid)
        out.agent_context.extend(consume_staged(home, sid))
        delivered = ie.deliver_inbox(home, sid, out.agent_context.append)
        # Count the boundary; re-surface what is still owed only when nothing
        # else was delivered at it.
        ie.resurface(home, sid, None if (delivered or out.agent_context) else out.agent_context.append)
        reminder = naming_reminder(home, sid)
        if reminder:
            out.agent_context.append(reminder)
    except Exception:
        pass
    return out


def at_turn_end(home: Path, sid: str, conn: Optional[dict], client: str, runtime: str, *, environ: Mapping,
                stop_hook_active: bool, deadline: float,
                owner_input_flush: Optional[Callable[[float], None]] = None) -> Delivery:
    """The turn's end of session `sid` (Claude / Codex Stop, Gemini
    AfterAgent, Cursor stop). What is deliverable: the inbox; without one,
    unless the poller read the events moments ago (investigation_poller.fresh),
    one synchronous check bounded by `deadline` (time.monotonic(); at least
    MIN_CHECK, TURN_END_RESERVE of it kept for the flush); when nothing was,
    an open directive or else the owner-decision reminders (only on the
    connection's own org, never under a grant token in `environ`).
    can_block(runtime): it is the turn_end_block (stop_hook_active bounds
    consecutive blocks); otherwise each item is staged for the next boundary
    and nothing is returned. Then, always: owner_input_flush(deadline) (the
    adapter's owner_input.flush_at_turn_end; None: none) and
    owner_input.turn_ended. Never raises."""
    out = Delivery()
    parts: List[str] = []
    try:
        if not ie.valid_session(sid) or ie.read_binding(home, sid) is None:
            return out
        try:
            _turn_end_events(home, sid, conn or {}, client, environ, stop_hook_active, deadline, parts.append)
        except Exception:
            pass
        b = ie.read_binding(home, sid)
        from . import investigation_grants
        if (can_block(runtime) and not stop_hook_active and b and b.get("org") == (conn or {}).get("org")
                and not investigation_grants.token_set(environ)):
            reminder = naming_reminder(home, sid, repeat=True)
            if reminder:
                parts.append(reminder)
        if parts:
            if can_block(runtime):
                out.turn_end_block = "\n\n".join(parts)
            else:
                for text in parts:   # after every lock above is released (stage takes it)
                    stage(home, sid, text)
    finally:
        if owner_input_flush is not None:
            with contextlib.suppress(Exception):
                owner_input_flush(deadline)
        with contextlib.suppress(Exception):
            from . import owner_input
            owner_input.turn_ended(home, sid)
        with contextlib.suppress(Exception):
            with ie.locked(home, sid, wait=0) as got:
                b = ie.read_binding(home, sid) if got else None
                if b:
                    b["question_reminded"] = False
                    ie.write_binding(home, sid, b)
    return out


def _turn_end_events(home, sid, conn, client, environ, stop_hook_active, deadline, emit) -> None:
    emitted: list = []

    def tracked(text: str) -> None:
        emitted.append(True)
        emit(text)

    if ie.deliver_inbox(home, sid, tracked, stop=True, stop_hook_active=stop_hook_active) is None:
        from . import investigation_poller as ip
        if not ip.fresh(home, sid):
            budget = max(MIN_CHECK, min(ie.CHECK_BUDGET, deadline - time.monotonic() - TURN_END_RESERVE))
            ie.check(home, sid, conn, client, tracked, stop=True, stop_hook_active=stop_hook_active, budget=budget)
    if emitted:
        return
    from . import investigation_grants
    b = ie.read_binding(home, sid)
    if b and not investigation_grants.token_set(environ) and b.get("org") == conn.get("org"):
        if not ie.supervision_stop(home, sid, tracked):
            ie.remind_decisions(home, sid, tracked)


def at_prompt(home: Path, sid: str, runtime: str) -> Delivery:
    """The owner's next prompt (Gemini BeforeAgent, Codex UserPromptSubmit,
    Cursor beforeSubmitPrompt / sessionStart): what was staged, as model
    context. No network. Never raises."""
    out = Delivery()
    out.agent_context.extend(consume_staged(home, sid))
    reminder = naming_reminder(home, sid, repeat=True)
    if reminder:
        out.agent_context.append(reminder)
    return out
