"""The per-session background poller: keeps a bound session's inbox current
so a tool boundary never waits on the network.

Why: every connected session is bound to its Investigation, and a
synchronous read at every tool call costs ~0.5 s (a Python start and one
HTTPS round trip). Instead one detached process per session polls
read-investigation-events after the session's cursor and, when something is
deliverable, writes it to the session's inbox (investigation_events
.poll_once). The hook's POSIX sh fast path only checks for the inbox file;
Python starts only when it exists and renders it (deliver_inbox), so the
cursor still advances only after the events reached the session.

Lifecycle (one poller per session, an flock on <sid>.poller.lock):
  - started at SessionStart (bootstrap) and, when its pid in <sid>.poller.pid
    is not alive, by the sh fast path at the next tool boundary;
  - polls every POLL_INTERVAL (5 s) only while the session is active: its
    <sid>.active file, touched at every tool boundary, is younger than
    IDLE_AFTER (2 min). An idle session costs no requests; the first tool
    call after idling is noticed within a second;
  - backs off after failures: 5 s doubling to 5 min, the server's
    Retry-After after a 429, 10 min after 401 / 403 / an unknown
    investigation, 5 min when the server lacks the routes;
  - retries a failed bootstrap (investigation_bootstrap) when it is due,
    silently: nothing it does is model-visible;
  - once per session start on a reused binding (the <sid>.refresh marker
    ensure() leaves), asks the server again what it advertises and stores
    it in the binding's capabilities (refresh_capabilities), whether or not
    the session is active, so the next start reads a current answer;
    a transient failure is retried with the same back-off;
  - exits when the agent process it watches (its anchor: the first
    non-shell ancestor of the hook that started it) is gone, when the
    session has neither a binding nor a pending bootstrap, or after
    MAX_LIFETIME (24 h; the next tool boundary starts a fresh one).

Server load: at most one read-investigation-events per active session per
5 s (12 a minute), none while idle; one row-range query (seq > cursor,
limit 20) each. The synchronous design it replaces read once per tool call
(up to one a second).

Never uploads anything; never writes model-visible output.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from . import investigation_bootstrap as boot
from . import investigation_events as ie
from . import investigation_state_sync as sync

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

POLL_INTERVAL = 5.0
MIN_INTERVAL = 1.0          # the floor for an override (CARDINAL_INVESTIGATION_POLL_INTERVAL)
IDLE_AFTER = 120.0
TICK = 1.0
MAX_LIFETIME = 24 * 3600.0
BACKOFF_MAX = 300.0
LOCK_WAIT = 10.0            # a poller of the previous process may still hold the lock for a tick
FRESH = POLL_INTERVAL + 1.0  # a successful poll this recent covers the Stop backstop

SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "mksh", "fish", "busybox"}


def pid_path(home: Path, sid: str) -> Path:
    return ie.sessions_dir(home) / f"{ie.binding_path(home, sid).stem}.poller.pid"


def status_path(home: Path, sid: str) -> Path:
    return ie.sessions_dir(home) / f"{ie.binding_path(home, sid).stem}.poller.json"


def read_status(home: Path, sid: str) -> dict:
    try:
        data = json.loads(status_path(home, sid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def fresh(home: Path, sid: str, now: Optional[float] = None, within: float = FRESH) -> bool:
    """The poller read the events within `within` seconds and found nothing
    deliverable (the Stop backstop may skip its own read)."""
    now = time.time() if now is None else now
    st = read_status(home, sid)
    ok = st.get("ok_at")
    return st.get("last") == "none" and isinstance(ok, (int, float)) and 0 <= now - ok <= within


def alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def running(home: Path, sid: str) -> bool:
    try:
        pid = int(pid_path(home, sid).read_text().strip())
    except (OSError, ValueError):
        return False
    return alive(pid)


def resolve_anchor(pid: int, ps: Optional[Callable[[int], Optional[tuple]]] = None) -> int:
    """The process to outlive: `pid`, or its first ancestor that is not a
    shell (a hook may run under a transient `sh -c`)."""
    ps = ps or _ps
    for _ in range(4):
        got = ps(pid)
        if not got:
            return pid
        ppid, comm = got
        name = os.path.basename(comm).lstrip("-")
        if name in SHELLS and ppid > 1:
            pid = ppid
            continue
        return pid
    return pid


def _ps(pid: int) -> Optional[tuple]:
    try:
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=2, check=False).stdout.strip()
        ppid, _, comm = out.partition(" ")
        return int(ppid), comm.strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


@contextlib.contextmanager
def _single(home: Path, sid: str, wait: float):
    if fcntl is None:  # pragma: no cover
        yield True
        return
    d = ie._ensure_dirs(home)
    fd = os.open(str(d / f"{ie.binding_path(home, sid).stem}.poller.lock"), os.O_RDWR | os.O_CREAT, 0o600)
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
                time.sleep(0.2)
        yield got
    finally:
        if got:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _delay(err: Exception, failures: int) -> float:
    """How long to wait after the `failures`-th failed read in a row."""
    if isinstance(err, sync.ServerError):
        if err.status == 429:
            return max(POLL_INTERVAL, err.retry_after or 60.0)
        fixed = ie.retry_delay(err)  # 401 / 403 / unknown investigation / no routes
        if fixed > ie.RETRY_AFTER_FAILURE:
            return fixed
    return min(BACKOFF_MAX, ie.RETRY_AFTER_FAILURE * (2 ** max(0, failures - 1)))


def run(home: Path, sid: str, *, connection: Callable[[], dict], client: str, anchor: Optional[int] = None,
        connected: Callable[[], bool] = lambda: True, interval: float = POLL_INTERVAL, idle: float = IDLE_AFTER,
        max_lifetime: float = MAX_LIFETIME, once: bool = False, tick: float = TICK, opener=None) -> str:
    """The poller loop (once=True: one due step, then return). Returns why it
    stopped: "duplicate", "anchor", "unbound", "lifetime" or "once"."""
    if not ie.valid_session(sid):
        return "unbound"
    with _single(home, sid, 0.0 if once else LOCK_WAIT) as got:
        if not got:
            return "duplicate"
        if not once:
            with contextlib.suppress(OSError):
                _write_pid(home, sid)
        try:
            return _loop(home, sid, connection, client, anchor, connected, max(0.0, interval), idle,
                         max_lifetime, once, tick, opener)
        finally:
            if not once:
                with contextlib.suppress(OSError, ValueError):
                    if pid_path(home, sid).read_text().strip() == str(os.getpid()):
                        pid_path(home, sid).unlink()


def _write_pid(home: Path, sid: str) -> None:
    ie._ensure_dirs(home)
    p = pid_path(home, sid)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(f"{os.getpid()}\n")
    os.replace(str(tmp), str(p))


def _loop(home, sid, connection, client, anchor, connected, interval, idle, max_lifetime, once, tick,
          opener) -> str:
    started = time.time()
    status = {"pid": os.getpid(), "anchor": anchor, "started_at": started, "polled_at": None, "ok_at": None,
              "failures": 0, "next_at": 0.0, "last": None, "refresh_failures": 0, "refresh_at": 0.0}
    while True:
        now = time.time()
        if anchor is not None and not alive(anchor):
            return "anchor"
        if now - started > max_lifetime:
            return "lifetime"
        bound = ie.read_binding(home, sid) is not None
        if not bound and boot.read_pending(home, sid) is None:
            return "unbound"
        if bound and now >= status["refresh_at"] and boot.wants_refresh(home, sid) and connected():
            _refresh(home, sid, connection, client, opener, status, now)
            with contextlib.suppress(OSError, ValueError):
                ie.write_json(status_path(home, sid), status)
        active = now - ie.last_activity(home, sid) <= idle
        if (once or active) and now >= status["next_at"] and connected():
            _step(home, sid, connection, client, opener, status, interval, now)
            with contextlib.suppress(OSError, ValueError):
                ie.write_json(status_path(home, sid), status)
        if once:
            return "once"
        time.sleep(tick)


def _refresh(home, sid, connection, client, opener, status, now) -> None:
    """The session's capabilities, asked again once per session start."""
    try:
        status["refresh"] = boot.refresh_capabilities(home, sid, connection() or {}, client, opener=opener)
        status["refresh_failures"] = 0
    except Exception as e:  # transient: the marker stays; back off, silently
        status["refresh_failures"] = int(status.get("refresh_failures") or 0) + 1
        status["refresh"] = f"error {getattr(e, 'status', type(e).__name__)}"
        status["refresh_at"] = now + _delay(e, status["refresh_failures"])


def _step(home, sid, connection, client, opener, status, interval, now) -> None:
    conn = connection() or {}
    # A failed bootstrap first: retried silently when due.
    if boot.needs_retry(home, sid) and boot.due(home, sid, now):
        boot.ensure(home, sid, conn, client, opener=opener, timeout=10.0, now=now)
    if ie.read_binding(home, sid) is None:
        status["next_at"] = now + interval
        return
    status["polled_at"] = now
    try:
        status["last"] = ie.poll_once(home, sid, conn, client, opener=opener, now=now)
        status["ok_at"] = now
        status["failures"] = 0
        status["next_at"] = now + interval
    except Exception as e:  # a failed read: back off, silently
        status["failures"] = int(status.get("failures") or 0) + 1
        status["last"] = f"error {getattr(e, 'status', type(e).__name__)}"
        status["next_at"] = now + max(interval, _delay(e, status["failures"]))
