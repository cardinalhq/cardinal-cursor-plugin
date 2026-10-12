"""Automatic Investigation sessions for adapters using storyboard_agent.Wiring
(Codex, Cursor, Gemini; Claude's hooks call investigation_text and
investigation_turn directly).

The session owns visualization authoring after user consent. This adapter bootstraps the session, describes
its capabilities (investigation_text.session_text, the one guidance builder), delivers advisory events at its
boundaries (investigation_turn: deliver at a tool boundary, turn_end at the turn's end, prompt at the next
prompt), and submits explicit semantic checkpoints with cited evidence. Every text these return has
`cardinal-storyboard investigation` replaced by this adapter's installed command. No transcript or user
prompt is read or uploaded here (owner input is owner_input.capture, called by the prompt hook).
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import investigation_bootstrap as boot
from . import investigation_events as ie
from . import investigation_grants as grants
from . import investigation_poller as poller
from . import investigation_text as text
from . import investigation_turn as turn


def home(wiring) -> Path:
    return wiring.paths.home.parent


def enabled(wiring) -> bool:
    return not wiring._off("CARDINAL_STORYBOARD_SESSION_START") and not grants.token_set(wiring.environ)


def command(wiring) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(wiring.cli)} investigation"


def runtime(wiring) -> str:
    """The wiring's runtime ("codex" / "cursor" / "gemini"; "" when it has none)."""
    value = getattr(wiring, "runtime", None)
    return value if isinstance(value, str) else ""


def owner_input_recorded(wiring, binding: dict) -> bool:
    """Whether this session's prompts are recorded as owner input: the
    binding enables it, the kill switch is not set and, where the runtime
    requires it, the user consented. False when it cannot be told. Never
    raises."""
    try:
        from . import owner_input
        rt = runtime(wiring)
        return (owner_input.enabled(binding) and rt in owner_input.RUNTIMES
                and not owner_input.kill_switch(rt, home(wiring), wiring.environ)
                and owner_input.consent(rt, home(wiring)))
    except Exception:
        return False


def describe(wiring, sid: str, binding: dict) -> str:
    """The session guidance (investigation_text.session_text) with this
    adapter's command and an explicit --session."""
    return text.session_text(sid, binding, cmd=command(wiring), target=f"--session {shlex.quote(sid)}",
                             runtime=runtime(wiring), owner_input_recorded=owner_input_recorded(wiring, binding))


def start(wiring, sid: str, source: Optional[str] = None, *, cwd: Optional[str] = None) -> Optional[str]:
    if not enabled(wiring) or not ie.valid_session(sid):
        return None
    conn = wiring.connection()
    if not conn:
        return None
    wanted = wiring.environ.get("CARDINAL_INVESTIGATION_ID") or None
    # Invalid explicit targets fail closed; never silently create another investigation.
    if wanted and not ie.valid_investigation(wanted):
        return None
    context = {} if wiring.context_disabled() else wiring.collect(cwd or os.getcwd(), sid, gh=False)
    result = boot.ensure(home(wiring), sid, conn, wiring.client, wanted=wanted, context=context,
                         started_at=boot.started_now() if source in (None, "startup") else None)
    binding = result.get("binding")
    if binding:
        from . import session_repos
        session_repos.replay(home(wiring), sid, wiring.runtime, wiring.client, env=wiring.environ)
    if binding and binding.get("bootstrap", {}).get("status") == "ok":
        return describe(wiring, sid, binding)
    return None


def spawn_poller(wiring, sid: str, script: str) -> None:
    if not enabled(wiring) or wiring._off("CARDINAL_INVESTIGATION_POLLER") or not ie.valid_session(sid):
        return
    root = home(wiring)
    if not (ie.read_binding(root, sid) or boot.read_pending(root, sid)):
        return
    ie.touch_activity(root, sid)
    if poller.running(root, sid):
        return
    subprocess.Popen([sys.executable, script, "--session", sid, "--anchor", str(os.getppid())],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, start_new_session=True)


def _bound(wiring, sid: str) -> Optional[dict]:
    """The session's binding when this adapter delivers for it: enabled, a
    valid id, bound, and on this connection's org; else None."""
    if not enabled(wiring) or not ie.valid_session(sid):
        return None
    binding = ie.read_binding(home(wiring), sid)
    if not binding or binding.get("org") != wiring.connection().get("org"):
        return None
    return binding


def _local(wiring, value: str) -> str:
    return value.replace("cardinal-storyboard investigation", command(wiring))


def _localized(wiring, d: "turn.Delivery") -> "turn.Delivery":
    return turn.Delivery([_local(wiring, x) for x in d.agent_context],
                         _local(wiring, d.user_message) if d.user_message else d.user_message,
                         _local(wiring, d.turn_end_block) if d.turn_end_block else d.turn_end_block)


def deliver(wiring, sid: str, emit, *, is_subagent: bool = False) -> None:
    """A tool boundary (investigation_turn.at_tool): emit(text) for each
    model-context text, in order. is_subagent (the payload names an agent):
    nothing, and nothing changes. Only on-disk state: tool completion never
    waits on the network. Never raises."""
    try:
        binding = None if is_subagent else _bound(wiring, sid)
        if binding is None:
            return
        d = turn.at_tool(home(wiring), sid, binding.get("org"), runtime(wiring), is_subagent=False)
        for item in _localized(wiring, d).agent_context:
            emit(item)
    except Exception:
        pass


def turn_end(wiring, sid: str, *, stop_hook_active: bool, deadline: float, cwd: Optional[str] = None,
             owner_input: bool = True) -> "turn.Delivery":
    """The turn's end (investigation_turn.at_turn_end) on this adapter's
    runtime: a Delivery whose turn_end_block is set only where the runtime
    may block (investigation_turn.TURN_END_BLOCK; elsewhere what is
    deliverable is staged for the next boundary). owner_input: also flush
    the owner-input outbox by `deadline` (time.monotonic()), or delete it
    when owner input is off here (owner_input.flush_at_turn_end; cwd for the
    kill switch). Never raises."""
    try:
        if _bound(wiring, sid) is None:
            return turn.Delivery()
        from . import owner_input as oi
        root, rt, conn = home(wiring), runtime(wiring), wiring.connection()
        flush = None
        if owner_input and rt in oi.RUNTIMES:
            def flush(d: float) -> None:
                oi.flush_at_turn_end(root, sid, d, runtime=rt, conn=conn, client=wiring.client,
                                     environ=wiring.environ, cwd=cwd)
        d = turn.at_turn_end(root, sid, conn, wiring.client, rt, environ=wiring.environ,
                             stop_hook_active=stop_hook_active, deadline=deadline, owner_input_flush=flush)
        return _localized(wiring, d)
    except Exception:
        return turn.Delivery()


def prompt(wiring, sid: str) -> "turn.Delivery":
    """The owner's next prompt (investigation_turn.at_prompt): what was
    staged, as model context. No network. Never raises."""
    try:
        if _bound(wiring, sid) is None:
            return turn.Delivery()
        return _localized(wiring, turn.at_prompt(home(wiring), sid, runtime(wiring)))
    except Exception:
        return turn.Delivery()


def cli_main(argv, wiring, session_env=(), *, legacy=False) -> int:
    from . import investigation_cli
    return investigation_cli.cli_main(argv, wiring, session_env, legacy=legacy)
