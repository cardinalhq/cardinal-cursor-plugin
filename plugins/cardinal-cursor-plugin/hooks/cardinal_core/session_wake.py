"""Owner-woken sessions: deliver an owner decision addressed to a session,
never launch it (docs/specs/resume-and-wake.md §5, v4).

Why: an Investigation outlives the session that did its work, and an owner
decision posted from the web reaches a session only at its own hook points.
A stopped session receives nothing until the owner types into that window.
The waker tells the owner, on this machine, that a decision is waiting for
a session, and gives a one-step way to resume it in the owner's own
terminal. It never starts an agent by itself.

Authorization (§5.1). Only an `owner.decided` event the owner posted on the
Investigation page from their signed-in web session, addressed to the
session (`to_session_id`), is acted on. maestro accepts that route only
from an OIDC web session of the investigation's author (proof C-1: the
plugin's API key, the only Cardinal credential an agent holds, is refused).
Nothing is armed from inside a session. The machine opt-in is the owner
running `cardinal-wake enable` (a per-user launchd agent).

The daemon itself (B1). `enable` vets what launchd will run: the interpreter
is this Python or the real one behind /usr/bin/python3, resolved outside
$HOME, git work trees under $HOME, temp dirs and version-manager shims, a
native executable, run with -I -S; PATH (the plist's) is the system dirs
plus the interpreter's directory, never the shell's; the interpreter's
path, size, mtime, inode, ctime and sha256 are recorded (`status` checks
them again: binaries_problem).

One pass (run_once; launchd runs `cardinal-wake run --once` every
RUN_INTERVAL seconds from the stable copy in ~/.cardinal/wake/lib/), with
the opt-in on, for every candidate session: a binding touched within
CANDIDATE_WINDOW, is_author true, the owner_decision capability on (a
~/.cardinal binding decides alone; a relocated one only for sessions with no
Codex or Claude records). Its events are read straight from Cardinal
(read-investigation-events with the plugin's key, that runtime's first, the
binding's org), never from the local inbox, so a local file cannot fake a
decision. A decision e is notified for sid only when (§5.2):
verified_owner_decision(e) (server-stamped fields); e.to_session_id == sid (a
broadcast never notifies); e is still deliverable() to sid; e.seq of its
investigation was never handled for sid (no ledger entry and above the
never-pruned high-water mark, B3); the caps hold (SESSION_DAILY_CAP
notifications per session and GLOBAL_DAILY_CAP across sessions per rolling
day). Decisions are handled in ascending seq and a pass stops at the first
one it cannot record (the caps, a busy lock), so the high-water mark never
passes a decision that was not notified. Each decision gets exactly one
notification (NOTIFIED, never reconsidered).

The notification (never the decision's text) names the runtime, the
session's short id and the investigation (its id and the connection's host,
never a link the binding carries). For a live session it says to switch to
that window; for a session of an unrecognized runtime it offers nothing;
for a stopped one it gives `cardinal-wake resume <sid8>` (or, when the
stable copy in ~/.cardinal/wake/lib is missing or out of date: run
`cardinal-wake enable` again), and with terminal-notifier installed
(/opt/homebrew/bin or /usr/local/bin) and the interpreter still vetted, a
click opens Terminal on ~/.cardinal/wake/resume/<sid>.command (a real,
user-owned directory), which execs the stable runner's `resume <sid>`.
Otherwise osascript shows it. A decision
older than DECISION_MAX_AGE (or undated) or posted without "Resume this
session if it has stopped" (payload.wake) is notified the same way; the
reason is kept in the ledger and the session.wake report.

`cardinal-wake resume <session>` (attended, in the owner's terminal): the
runtime by its own records (RECORDS), the working directory only from those
records (a Claude transcript's cwd, the Codex rollout's session_meta.cwd,
Gemini's project root; Cursor's records when they carry one), never from the
agent-writable binding; it must exist (no control characters). Then
os.chdir + os.execvp of the runtime's own resume (its absolute path, from
the absolute PATH entries) with the WAKE_PROMPT of a decision the session
has not received yet (pending_prompt; a MACHINE_PROMPT on every runtime, so
the turn is never owner input) as the first prompt:
  claude --resume <sid> "<prompt>"; codex resume <sid> "<prompt>" (a Codex
  Desktop session: /usr/bin/open codex://threads/<sid>); gemini --resume
  <ident> "<prompt>"; cursor-agent --resume <sid> "<prompt>" (experimental).
The resumed session receives the decision through its own investigation
hooks (PostToolUse / Stop delivery), as any session does. `--print` shows
the command and the directory and runs nothing.

Ledger ~/.cardinal/wake/ledger/<sid>.json keyed by investigation_id:seq,
with a per-investigation high-water mark that is never pruned; every
read-modify-write holds an flock on <sid>.lock (the caps' check also holds
.global.lock, after the session's): DECISION_SEEN -> NOTIFIED. Entries of
the launch states an earlier version wrote (CLAIMED, RESUMING, TIMED_OUT,
ACK_RELAYED, ACKNOWLEDGED, FAILED) load and count as terminal.

Liveness (the notification's wording): live when ANY of: the poller status
anchor pid is alive and started before the status file was written; a
process's argv contains the session id; the runtime's records, or the
binding's .active file, changed within LIVE_WINDOW. A signal that cannot be
read counts as live.

Reporting: each NOTIFIED entry is posted once as a `session.wake` event
(state "notified", a fixed-vocabulary reason) when the binding advertises
session_wake (conductor#2110).

Standard library only; Python 3.9. Every external effect (clock, process
table, process start times, notifier, Cardinal reads, launchctl, the
resume's exec) is injectable for tests.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import os
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator, List, Mapping, Optional, Tuple

from . import investigation_events as ie
from . import investigation_poller as poller
from . import investigation_state_sync as sync
from . import paths
from .owner_input import wake_prompt

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------

CANDIDATE_WINDOW = 7 * 86400.0   # a binding untouched this long is never considered
RECENT_WINDOW = 86400.0          # bindings touched this recently are read every pass ...
OLD_CHECK_EVERY = 300.0          # ... older candidates at most this often
LIVE_WINDOW = 120.0
SESSION_DAILY_CAP = 3            # notifications per session per rolling day
GLOBAL_DAILY_CAP = 10            # ... and across sessions
DAY = 86400.0
RUN_INTERVAL = 45                # launchd StartInterval (seconds)
LEDGER_KEEP = 30 * 86400.0       # terminal entries kept this long (a repeat of their seq never notifies)
LEDGER_MAX_ENTRIES = 200
READ_TIMEOUT = 10.0
READ_BUDGET = 25.0
MAX_LINE = 16 * 1024 * 1024      # a record line longer than this is skipped
MAX_RECORD_SCAN = 64 * 1024 * 1024   # bytes of a runtime record read to find its working directory
MAX_LAUNCHD_LOG = 1024 * 1024
DECISION_MAX_AGE = 7 * 86400.0   # an owner decision older than this (created_at) is notified as old
WAKE_LOGS_KEEP = 50              # older logs (from earlier versions' wakes) are deleted
RESUME_FILE_KEEP = CANDIDATE_WINDOW   # a resume .command untouched this long is deleted

PLIST_LABEL = "io.cardinalhq.wake"

# The daemon's interpreter (enable resolves and vets it). PATH for the
# daemon: these system directories plus the interpreter's, never the shell's.
SYSTEM_PATH_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
UNSAFE_TEMP_DIRS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")
# N2: version-manager shims pick the real binary from files in the cwd
# (.tool-versions, .nvmrc, package.json "volta"), which an agent can write;
# the daemon's interpreter may not resolve through them.
SHIM_DIRS = (".asdf", ".local/share/mise", ".mise", ".nodenv", ".volta", ".nvm", ".pyenv", ".rbenv", ".fnm",
             ".local/share/fnm", ".proto", ".local/share/aquaproj-aqua")
SHIM_BINARIES = frozenset(("volta-shim", "mise", "asdf", "proto-shim", "rtx"))
# Mach-O (32/64-bit, both byte orders; universal) and ELF (Linux, where the
# tests run; macOS cannot execute it, so it gains nothing there).
_NATIVE_MAGICS = frozenset((b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
                            b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\x7fELF"))

DECISION_SEEN = "DECISION_SEEN"
NOTIFIED = "NOTIFIED"
# States an earlier version (unattended launch, removed in v4) wrote. A
# ledger holding them still loads; they are terminal and never reported again.
LEGACY_STATES = ("CLAIMED", "RESUMING", "TIMED_OUT", "ACK_RELAYED", "ACKNOWLEDGED", "FAILED")
TERMINAL = (NOTIFIED,) + LEGACY_STATES

CODEX = "codex"
CLAUDE = "claude"
CURSOR = "cursor"
GEMINI = "gemini"
UNKNOWN = "unknown"
RUNTIMES = (CODEX, CLAUDE, CURSOR, GEMINI)   # every adapter with the Investigation layer
LABELS = {CODEX: "Codex", CLAUDE: "Claude Code", CURSOR: "Cursor", GEMINI: "Gemini CLI", UNKNOWN: "Unknown-runtime"}
# Originators of Codex's own CLI (codex exec, the TUI): `codex resume`. Any
# other (the desktop app, "Codex Desktop", or unrecognized) opens the thread
# in the app instead.
CODEX_ORIGINATORS = frozenset({"codex_exec", "codex_cli_rs"})
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")  # fullmatch
PREFIX_MIN = 8                   # hex characters of a session id `resume` accepts as a prefix

PS = "/bin/ps"
OSASCRIPT = "/usr/bin/osascript"
OPEN = "/usr/bin/open"
LAUNCHCTL = "/bin/launchctl"
TERMINAL_NOTIFIERS = ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier")
# Each runtime's own binary `resume` runs (looked up on the owner's PATH).
# Cursor: only `cursor-agent`, never `agent` (an unrelated tool on some
# machines). Its resume is unverified: experimental.
RESUME_BINARIES = {CLAUDE: "claude", CODEX: "codex", GEMINI: "gemini", CURSOR: "cursor-agent"}
TITLE = "Cardinal wake"

NOTIFY_TEXT = (
    "Each addressed decision is one notification, never its text: for a stopped session it names "
    "`cardinal-wake resume <session>`, which resumes it in your own terminal with the runtime's own resume "
    "(Claude Code, Codex CLI, Gemini CLI; Cursor's cursor-agent is experimental; a Codex desktop session opens "
    "in the app); for a session whose window is still open it says to switch to that window. Nothing is ever "
    "launched unattended.")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def wake_dir(home: Path) -> Path:
    return home / ".cardinal" / "wake"


def config_path(home: Path) -> Path:
    return wake_dir(home) / "config.json"


def ledger_dir(home: Path) -> Path:
    return wake_dir(home) / "ledger"


def ledger_path(home: Path, sid: str) -> Path:
    if not ie.valid_session(sid):
        raise ValueError(f"not a session id: {sid!r}")
    return ledger_dir(home) / f"{sid}.json"


def logs_dir(home: Path) -> Path:
    return wake_dir(home) / "logs"


def lib_dir(home: Path) -> Path:
    return wake_dir(home) / "lib"


def runner_path(home: Path) -> Path:
    """The stable runner launchd (and a notification's click) runs."""
    return lib_dir(home) / "cardinal-wake"


def resume_dir(home: Path) -> Path:
    return wake_dir(home) / "resume"


def poll_state_path(home: Path) -> Path:
    return wake_dir(home) / "poll.json"


def plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return path


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


@contextlib.contextmanager
def _locked(path: Path, wait: float) -> Iterator[bool]:
    """flock on `path` (created 0600); yields whether it was acquired within
    `wait` seconds (investigation_events.locked's pattern)."""
    if fcntl is None:  # pragma: no cover
        yield True
        return
    _private_dir(path.parent)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
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
                time.sleep(0.02)
        yield got
    finally:
        if got:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def session_lock(home: Path, sid: str, wait: float = 5.0):
    ledger_path(home, sid)  # validates sid
    return _locked(ledger_dir(home) / f"{sid}.lock", wait)


def global_lock(home: Path, wait: float = 10.0):
    return _locked(ledger_dir(home) / ".global.lock", wait)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def read_config(home: Path) -> dict:
    """The opt-in and the vetted interpreter. Keys an earlier version wrote
    (launch, roots, relay_notes, <runtime>_path...) are ignored."""
    c = _read_json(config_path(home))
    python, path_env = c.get("python"), c.get("path_env")
    binaries = c.get("binaries") if isinstance(c.get("binaries"), dict) else {}
    return {"enabled": c.get("enabled") is True,
            "enabled_at": c.get("enabled_at"),
            "python": python if isinstance(python, str) and os.path.isabs(python) else None,
            "path_env": path_env if isinstance(path_env, str) and path_env else None,
            "binaries": {k: v for k, v in binaries.items() if isinstance(k, str) and os.path.isabs(k)
                         and isinstance(v, list)},
            "versions": c.get("versions") if isinstance(c.get("versions"), dict) else {}}


def write_config(home: Path, config: dict) -> None:
    _private_dir(wake_dir(home))
    ie.write_json(config_path(home), config)


# ---------------------------------------------------------------------------
# The daemon's interpreter (B1: never from an agent-writable place)
# ---------------------------------------------------------------------------

def temp_dirs() -> Tuple[str, ...]:
    """Directories any sandboxed agent may write (temp): the daemon's
    interpreter may not live there."""
    import tempfile
    return tuple(dict.fromkeys(list(UNSAFE_TEMP_DIRS) + [os.path.realpath(tempfile.gettempdir())]))


def _git_root(cwd: str) -> Optional[Path]:
    """The git work tree's root containing `cwd` (the nearest directory with
    a .git entry, cwd included), or None."""
    p = Path(cwd)
    for d in (p, *p.parents):
        if os.path.lexists(str(d / ".git")):
            return d
    return None


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def unsafe_location(path: str, home: Path, roots: Optional[List[str]] = None, *,
                    allow_home: bool = True) -> Optional[str]:
    """Why the daemon must not run `path` (resolved), or None: inside a git
    work tree under $HOME (where agents work), one of `roots`, a temp
    directory, or (allow_home False) anywhere under $HOME. A git tree
    outside $HOME is not refused: Homebrew's own prefix (/opt/homebrew) is
    one."""
    real = os.path.realpath(path)
    home_real = os.path.realpath(str(home))
    git = _git_root(os.path.dirname(real))
    if git is not None and _within(os.path.realpath(str(git)), home_real):
        return f"{real} is inside a git work tree"
    for root in roots or ():
        if _within(real, os.path.realpath(root)):
            return f"{real} is inside {root}"
    for t in temp_dirs():
        if _within(real, t):
            return f"{real} is inside a temporary directory"
    if not allow_home and _within(real, os.path.realpath(str(home))):
        return f"{real} is inside your home directory"
    return None


def fingerprint(path: str) -> Optional[list]:
    """[size, mtime_ns, inode, ctime_ns, sha256] of the file `path` resolves
    to, or None (N5: size / mtime / inode alone survive an in-place rewrite
    with the mtime set back; ctime and the content hash do not)."""
    import hashlib
    try:
        st = os.stat(path)
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns, h.hexdigest()]


def is_native_executable(path: str) -> bool:
    """`path` is a native executable (Mach-O, thin or universal; ELF where
    tests run), never a script that picks or spawns another binary."""
    try:
        with open(path, "rb") as f:
            return f.read(4) in _NATIVE_MAGICS
    except OSError:
        return False


def shim_problem(path: str, home: Path) -> Optional[str]:
    """Why `path` (as found, or resolved) is a version-manager shim, or None."""
    home_real = os.path.realpath(str(home))
    for p in (os.path.abspath(path), os.path.realpath(path)):
        for d in SHIM_DIRS:
            if _within(p, os.path.join(home_real, d)) or _within(p, os.path.join(str(home), d)):
                return f"{p} is a version-manager shim (~/{d}), which picks its target from files in the cwd"
    if os.path.basename(os.path.realpath(path)) in SHIM_BINARIES:
        return f"{os.path.realpath(path)} is a version-manager shim"
    return None


def system_python() -> Optional[str]:
    """The real interpreter behind the /usr/bin/python3 xcrun shim (`xcrun
    -f python3` with a clean environment: no DEVELOPER_DIR or SDKROOT),
    or None. The shim itself picks its target from the environment, so the
    daemon never runs it (review 3 #7)."""
    try:
        r = subprocess.run(["/usr/bin/xcrun", "-f", "python3"], capture_output=True, text=True, timeout=10,
                           check=False, env={"PATH": "/usr/bin:/bin", "HOME": "/var/empty"})
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and os.path.isabs(out) else None


def pick_python(home: Path, roots: Optional[List[str]] = None) -> Tuple[Optional[str], str]:
    """(the interpreter the daemon runs, with -I -S) or (None, why none is
    safe): this Python if it resolves outside $HOME, git work trees, temp
    dirs and shims, else the real interpreter behind /usr/bin/python3
    (system_python). Never a binary in the system PATH directories
    (/usr/bin/python3 is an xcrun shim)."""
    reasons = []
    for candidate in (sys.executable, system_python()):
        if not candidate or not os.access(candidate, os.X_OK):
            continue
        real = os.path.realpath(candidate)
        why = (unsafe_location(candidate, home, roots, allow_home=False) or shim_problem(candidate, home)
               or (f"{real} is a system shim, not the interpreter" if os.path.dirname(real) in SYSTEM_PATH_DIRS
                   else None)
               or (None if is_native_executable(real)
                   else f"{candidate} is not a native (Mach-O) executable"))
        if why is None:
            return os.path.realpath(candidate), ""
        reasons.append(why)
    return None, "; ".join(reasons) or "no python3 found"


def daemon_path(*dirs: Optional[str]) -> str:
    """PATH for the daemon: the system directories plus the given (vetted)
    directories, nothing from the user's shell."""
    return ":".join(dict.fromkeys([*SYSTEM_PATH_DIRS, *[d for d in dirs if d]]))


def binaries_problem(config: dict, home: Path) -> Optional[str]:
    """Why the vetted interpreter cannot be trusted now, or None: it must
    still be the file enable vetted (same path, size, mtime, inode, ctime,
    sha256), outside every unsafe location and version-manager shim, a
    native executable, and not the system shim."""
    python = config.get("python")
    fp = (config.get("binaries") or {}).get(python) if python else None
    if not python or fp is None or not config.get("path_env"):
        return "the daemon's interpreter was not vetted (run cardinal-wake enable again)"
    if fingerprint(python) != fp or os.path.realpath(python) != python:
        return f"{python} changed since cardinal-wake enable (run it again)"
    why = unsafe_location(python, home, allow_home=False) or shim_problem(python, home)
    if why:
        return why
    if not is_native_executable(python):
        return f"{python} is not a native (Mach-O) executable"
    if os.path.dirname(python) in SYSTEM_PATH_DIRS:
        return f"{python} is a system shim, not the interpreter (run cardinal-wake enable again)"
    return None


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def read_ledger(home: Path, sid: str) -> dict:
    data = _read_json(ledger_path(home, sid))
    entries = data.get("entries")
    data["session_id"] = sid
    data["entries"] = {k: v for k, v in entries.items() if isinstance(v, dict)} if isinstance(entries, dict) else {}
    hwm = data.get("hwm")
    data["hwm"] = {k: v for k, v in hwm.items() if ie.valid_investigation(k) and isinstance(v, int)
                   and not isinstance(v, bool)} if isinstance(hwm, dict) else {}
    data.pop("probe_retries", None)   # an earlier version's launch retries
    return data


def entry_key(investigation_id: str, seq: int) -> str:
    """A ledger entry's key: seqs are per investigation, so the key is both."""
    return f"{investigation_id}:{seq}"


def decision_seen(ledger: dict, investigation_id: str, seq: int) -> bool:
    """#seq of `investigation_id` was handled for this session: it has an
    entry, or it is at or below the session's high-water mark for that
    investigation (never pruned: a pruned entry cannot let a decision be
    notified again)."""
    return entry_key(investigation_id, seq) in ledger["entries"] or seq <= ledger["hwm"].get(investigation_id, 0)


def add_entry(ledger: dict, entry: dict) -> None:
    """Record a new entry and raise the investigation's high-water mark."""
    inv, seq = entry["investigation_id"], entry["seq"]
    ledger["entries"][entry_key(inv, seq)] = entry
    ledger["hwm"][inv] = max(ledger["hwm"].get(inv, 0), seq)


def write_ledger(home: Path, sid: str, ledger: dict, now: float) -> None:
    """Persist the ledger; terminal entries older than LEDGER_KEEP (or past
    LEDGER_MAX_ENTRIES) go, the high-water marks never do. Any entry an
    earlier version left in a launch state is terminal."""
    entries = ledger["entries"]
    for k in [k for k, e in entries.items()
              if e.get("state") in TERMINAL and now - (_num(e.get("updated_at")) or 0.0) > LEDGER_KEEP]:
        del entries[k]
    if len(entries) > LEDGER_MAX_ENTRIES:
        terminal = sorted((k for k, e in entries.items() if e.get("state") in TERMINAL),
                          key=lambda k: _num(entries[k].get("updated_at")) or 0.0)
        for k in terminal[:len(entries) - LEDGER_MAX_ENTRIES]:
            del entries[k]
    _private_dir(ledger_dir(home))
    ie.write_json(ledger_path(home, sid), ledger)


def transition(entry: dict, state: str, now: float, note: str = "") -> dict:
    """Move `entry` to `state`, appending one history line (never decision
    text: `note` is the waker's own words)."""
    entry["state"] = state
    entry["updated_at"] = now
    line = {"state": state, "at": _iso(now)}
    if note:
        line["note"] = note
        entry["reason"] = note
    entry.setdefault("history", []).append(line)
    if state in TERMINAL:
        entry["finished_at"] = now
    return entry


def ledger_sids(home: Path) -> List[str]:
    try:
        names = os.listdir(str(ledger_dir(home)))
    except OSError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json") and ie.valid_session(n[:-5]))


def _counted(ledger: dict, now: float) -> int:
    """Notifications (and an earlier version's claims) in the last DAY."""
    n = 0
    for e in ledger["entries"].values():
        t = _num(e.get("notified_at"))
        t = t if t is not None else _num(e.get("claimed_at"))
        if t is not None and 0 <= now - t < DAY:
            n += 1
    return n


def global_count(home: Path, now: float) -> int:
    return sum(_counted(read_ledger(home, sid), now) for sid in ledger_sids(home))


# ---------------------------------------------------------------------------
# Processes, notifications (the defaults System uses)
# ---------------------------------------------------------------------------

def process_start(pid: int) -> Optional[float]:
    """When process `pid` started (epoch seconds, 1 s resolution), or None."""
    if not isinstance(pid, int) or pid <= 1:
        return None
    try:
        out = subprocess.run([PS, "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=3,
                             check=False, env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"}).stdout.strip()
        return time.mktime(time.strptime(" ".join(out.split()), "%a %b %d %H:%M:%S %Y"))
    except (OSError, ValueError, OverflowError, subprocess.SubprocessError):
        return None


def process_table() -> Optional[List[Tuple[int, str]]]:
    """(pid, argv as one string) for every process, or None when ps fails."""
    try:
        r = subprocess.run([PS, "-A", "-ww", "-o", "pid=,args="], capture_output=True, text=True, timeout=5,
                           check=False, env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"})
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    rows = []
    for line in r.stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        with contextlib.suppress(ValueError):
            rows.append((int(pid), args))
    return rows


def terminal_notifier(exists: Callable[[str], bool] = lambda p: os.access(p, os.X_OK)) -> Optional[str]:
    """terminal-notifier where Homebrew installs it, or None."""
    return next((p for p in TERMINAL_NOTIFIERS if exists(p)), None)


def notify_argv(title: str, message: str, open_file: Optional[str] = None,
                notifier: Optional[str] = None) -> List[str]:
    """The notifier's argv. With terminal-notifier and a resume file, a click
    runs `/usr/bin/open -a Terminal <file>` (terminal-notifier hands -execute
    to a shell, so the path is shell-quoted). Otherwise osascript, with the
    text as arguments, never in the script, so nothing in it is evaluated."""
    if notifier:
        argv = [notifier, "-title", title, "-message", message, "-group", "cardinal-wake"]
        if open_file:
            argv[-1] = "cardinal-wake-" + Path(open_file).stem
            argv += ["-execute", f"{OPEN} -a Terminal {shlex.quote(open_file)}"]
        return argv
    return [OSASCRIPT, "-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
            "-e", "end run", title, message]


def notify_macos(title: str, message: str, open_file: Optional[str] = None) -> None:
    """A macOS notification (terminal-notifier when installed, so a click can
    resume the session; else osascript)."""
    if sys.platform != "darwin":
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(notify_argv(title, message, open_file, terminal_notifier()), capture_output=True,
                       timeout=10, check=False)


def exec_attended(argv: List[str], cwd: Optional[str]) -> None:
    """Replace this process with `argv` in `cwd` (the owner's terminal)."""
    if cwd:
        os.chdir(cwd)
    os.execvp(argv[0], argv)


def claude_connection(home: Path) -> dict:
    """The Claude plugin's connection: CARDINAL_MCP_URL / CARDINAL_MCP_API_KEY
    in ~/.claude/settings.json `env` (where /cardinal:connect writes them);
    nothing after /cardinal:disconnect (~/.claude/cardinal-disconnected)."""
    from .evidence_promote import connection_for
    if (home / ".claude" / "cardinal-disconnected").exists():
        return {}
    env = _read_json(home / ".claude" / "settings.json").get("env")
    if not isinstance(env, dict):
        return {}
    try:
        return connection_for(env.get("CARDINAL_MCP_URL"), env.get("CARDINAL_MCP_API_KEY")) or {}
    except Exception:
        return {}


def agent_connection(runtime: str, home: Path) -> dict:
    """A plugin's connection from its managed files under ~/.<runtime>
    (cardinal.json mcp_url + cardinal-secrets.json mcp_api_key; Codex,
    Cursor and Gemini CLI), never the environment."""
    from .evidence_promote import agent_paths_connection
    from .paths import AgentPaths
    try:
        return agent_paths_connection(AgentPaths(home=home / f".{runtime}"))(home, {}) or {}
    except Exception:
        return {}


def runtime_connection(runtime: str, home: Path) -> dict:
    if runtime == CLAUDE:
        return claude_connection(home)
    return agent_connection(runtime, home) if runtime in RUNTIMES else {}


def fetch_events(conn: dict, investigation_id: str, sid: str, after: int, client: str,
                 opener=None) -> Tuple[list, bool]:
    """(events after `after` addressed to `sid` or to everyone, whether the
    read reached the head). Raises on a failed read."""
    events, cursor, head = ie.read_all(conn, investigation_id, after=after, to_session_id=sid, client=client,
                                       opener=opener, deadline=time.monotonic() + READ_BUDGET,
                                       timeout=READ_TIMEOUT, limit=ie.PAGE_LIMIT)
    return events, cursor >= head


WAKE_REPORT_TIMEOUT = 10.0


def post_wake_event(conn: dict, investigation_id: str, sid: str, payload: dict, key: str, client: str,
                    opener=None) -> dict:
    """append-investigation-event of one session.wake report (conductor#2110):
    session_id = the notified session, never to_session_id; a bounded timeout."""
    return ie.append_event(conn, investigation_id, ie.SESSION_WAKE, payload, idempotency_key=key, client=client,
                           session_id=sid, producer_client=client, opener=opener, timeout=WAKE_REPORT_TIMEOUT)


class System:
    """Every effect the waker has on the world, injectable for tests.

    now() -> epoch; alive(pid); process_start(pid) -> epoch | None;
    process_table() -> [(pid, argv)] | None; notifier(title, message,
    open_file) (open_file: the resume .command a click opens, or None);
    connection(runtime, home) -> {origin, org, key}; fetch(conn, inv, sid,
    after, client) -> (events, complete); before_claim(sid, seq) (a test
    hook, called right before the notification's lock); environ (`resume`
    looks its binary up on this PATH); out(line) (the report); post_wake(conn,
    inv, sid, payload, idempotency_key, client) (a session.wake report;
    raises); exec(argv, cwd) (`resume`: replaces the process);
    click_notifier() -> the notifier that can run a click action
    (terminal-notifier) or None: a resume file is written only with one.
    """

    def __init__(self, *, now: Optional[Callable[[], float]] = None, alive: Optional[Callable[[int], bool]] = None,
                 process_start: Optional[Callable[[int], Optional[float]]] = None,
                 process_table: Optional[Callable[[], Optional[list]]] = None,
                 notifier: Optional[Callable[..., None]] = None,
                 connection: Optional[Callable[[str, Path], dict]] = None,
                 fetch: Optional[Callable[..., Tuple[list, bool]]] = None,
                 before_claim: Optional[Callable[[str, int], None]] = None,
                 environ: Optional[Mapping[str, str]] = None, opener=None,
                 out: Optional[Callable[[str], None]] = None,
                 post_wake: Optional[Callable[..., dict]] = None,
                 exec: Optional[Callable[[List[str], Optional[str]], None]] = None,
                 click_notifier: Optional[Callable[[], Optional[str]]] = None):
        self.post_wake = post_wake or (lambda conn, inv, sid, payload, key, client: post_wake_event(
            conn, inv, sid, payload, key, client, opener=opener))
        self.now = now or time.time
        self.alive = alive or poller.alive
        self.process_start = process_start or globals()["process_start"]
        self.process_table = process_table or globals()["process_table"]
        self.notifier = notifier or notify_macos
        self.connection = connection or runtime_connection
        self.fetch = fetch or (lambda conn, inv, sid, after, client: fetch_events(conn, inv, sid, after, client,
                                                                                 opener=opener))
        self.before_claim = before_claim
        self.environ = os.environ if environ is None else environ
        self.out = out or (lambda line: print(line, flush=True))
        self.exec = exec or exec_attended
        self.click_notifier = click_notifier or terminal_notifier


# ---------------------------------------------------------------------------
# Runtime resolution (each runtime's own records)
# ---------------------------------------------------------------------------

class Runtime:
    """What runs session `sid` here: kind (codex | claude | cursor | gemini |
    unknown), files (its records; their mtimes are a liveness signal) and,
    for unknown, why."""

    def __init__(self, kind: str, files: List[Path], reason: str = ""):
        self.kind, self.files, self.reason = kind, files, reason


def codex_rollouts(home: Path, sid: str) -> List[Path]:
    if not ie.valid_session(sid):
        return []
    root = home / ".codex" / "sessions"
    return sorted(Path(p) for p in glob.glob(str(root / "**" / f"rollout-*-{glob.escape(sid)}.jsonl"),
                                             recursive=True))


def claude_transcripts(home: Path, sid: str) -> List[Path]:
    if not ie.valid_session(sid):
        return []
    return sorted(Path(p) for p in glob.glob(str(home / ".claude" / "projects" / "*" / f"{glob.escape(sid)}.jsonl")))


GEMINI_HEAD = 64 * 1024   # bytes of a Gemini CLI chat file read to find its sessionId


def gemini_chats(home: Path, sid: str) -> List[Path]:
    """Gemini CLI's chat records for `sid`:
    ~/.gemini/tmp/<project>/chats/session-<time>-<sid[:8]>.json[l] whose
    head names sessionId == sid (only GEMINI_HEAD bytes are read)."""
    if not ie.valid_session(sid) or len(sid) < 8:
        return []
    pattern = str(home / ".gemini" / "tmp" / "*" / "chats" / f"session-*-{glob.escape(sid[:8])}.json*")
    want = re.compile(rb'"sessionId"\s*:\s*"' + re.escape(sid.encode()) + rb'"')
    out = []
    for p in sorted(glob.glob(pattern)):
        if not p.endswith((".json", ".jsonl")):
            continue
        with contextlib.suppress(OSError):
            with open(p, "rb") as f:
                if want.search(f.read(GEMINI_HEAD)):
                    out.append(Path(p))
    return out


def cursor_records(home: Path, sid: str) -> List[Path]:
    """Cursor's records for `sid` (best effort: Cursor documents no stable
    store): ~/.cursor/projects/*/agent-transcripts/<sid>.* and the files of
    ~/.cursor/chats/*/<sid>/. Not found: the runtime is unknown (still a
    notification)."""
    if not ie.valid_session(sid):
        return []
    s = glob.escape(sid)
    found = glob.glob(str(home / ".cursor" / "projects" / "*" / "agent-transcripts" / f"{s}.*"))
    found += glob.glob(str(home / ".cursor" / "chats" / "*" / s / "*"))
    return sorted(Path(p) for p in found if os.path.isfile(p))


# Each runtime's own records of a session (cheap: a glob; Gemini reads a
# file head). The records name the runtime, give `resume` its working
# directory, and their mtimes are a liveness signal.
RECORDS = {CODEX: codex_rollouts, CLAUDE: claude_transcripts, CURSOR: cursor_records, GEMINI: gemini_chats}


def runtime_kind(home: Path, sid: str) -> Runtime:
    """The runtime by its own records. None found, or records of more than
    one runtime: unknown."""
    found = {r: files for r, files in ((r, find(home, sid)) for r, find in RECORDS.items()) if files}
    if len(found) > 1:
        return Runtime(UNKNOWN, [p for files in found.values() for p in files],
                       reason=f"records of {' and '.join(LABELS[r] for r in found)} carry this session id")
    if not found:
        return Runtime(UNKNOWN, [], reason="no Codex, Claude Code, Cursor or Gemini CLI records for this session "
                                           "on this machine")
    (kind, files), = found.items()
    return Runtime(kind, files)


MAX_CONFIG_READ = 256 * 1024


def _read_small(path: Path) -> Optional[bytes]:
    """A regular, non-symlink file's bytes (at most MAX_CONFIG_READ), or None
    when it is anything else or cannot be read."""
    try:
        st = os.lstat(str(path))
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_CONFIG_READ:
            return None
        with open(str(path), "rb") as f:
            return f.read(MAX_CONFIG_READ + 1)
    except OSError:
        return None


def _json_records(path: Path, markers: Tuple[bytes, ...]) -> Iterator[dict]:
    """The JSON objects of a JSONL record (a regular file, never a symlink),
    line by line, only lines holding one of `markers`; overlong
    (MAX_LINE) or malformed lines are skipped; at most MAX_RECORD_SCAN bytes."""
    try:
        st = os.lstat(str(path))
    except OSError:
        return
    if not stat.S_ISREG(st.st_mode):
        return
    total = 0
    try:
        with open(str(path), "rb") as f:
            while total < MAX_RECORD_SCAN:
                line = f.readline(MAX_LINE + 1)
                if not line:
                    return
                total += len(line)
                if len(line) > MAX_LINE and not line.endswith(b"\n"):
                    while line and not line.endswith(b"\n"):   # skip the rest of an overlong line
                        line = f.readline(MAX_LINE)
                        total += len(line)
                    continue
                if not any(m in line for m in markers):
                    continue
                with contextlib.suppress(ValueError):
                    rec = json.loads(line)
                    if isinstance(rec, dict):
                        yield rec
    except OSError:
        return


def codex_session_meta(files: List[Path], sid: str) -> Optional[dict]:
    """The payload of the rollout's session_meta whose id is `sid`, or None."""
    for path in files:
        for rec in _json_records(path, (b"session_meta",)):
            p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            if rec.get("type") == "session_meta" and p.get("id") == sid:
                return p
    return None


_CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f]")


def _abs_dir(v: Any) -> Optional[str]:
    """An absolute path from a runtime's record, or None: never one with a
    control character (C0, DEL, C1: nothing raw reaches the terminal)."""
    return v if isinstance(v, str) and os.path.isabs(v) and not _CONTROL_RE.search(v) else None


def claude_project_dir_name(cwd: str) -> str:
    """How Claude Code names a project's directory under ~/.claude/projects."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def claude_cwd(files: List[Path], sid: str) -> Optional[str]:
    """The directory Claude Code resumes `sid` from: a cwd its transcript
    records for the session, the one that names the transcript's project
    directory when several do; None when none (or several, none naming it)."""
    for path in files:
        found: List[str] = []
        for rec in _json_records(path, (b'"cwd"',)):
            cwd = _abs_dir(rec.get("cwd"))
            if cwd and rec.get("sessionId") in (None, sid):
                if claude_project_dir_name(cwd) == path.parent.name:
                    return cwd
                if cwd not in found:
                    found.append(cwd)
            if len(found) > 32:
                break
        if len(found) == 1:
            return found[0]
    return None


def gemini_project_root(chat: Path) -> Optional[str]:
    """The project directory Gemini recorded for a chat file
    (~/.gemini/tmp/<project>/.project_root)."""
    data = _read_small(chat.parent.parent / ".project_root")
    if data is None:
        return None
    return _abs_dir(data.decode("utf-8", "replace").strip())


def gemini_cwd(files: List[Path]) -> Optional[str]:
    projects = {gemini_project_root(p) for p in files}
    return projects.pop() if len(projects) == 1 and None not in projects else None


def resolve_gemini_resume_ident(sid: str, cwd: str) -> Optional[str]:
    """What `gemini --resume` is given for session `sid` started in `cwd`.

    gemini-cli takes the full session UUID ("Resume by ID", its bundled
    docs/cli/session-management.md, 0.63.0), so this is the UUID. Whatever
    this returns is never an option (never starts with "-")."""
    return sid if UUID_RE.fullmatch(sid or "") else None


def cursor_cwd(files: List[Path]) -> Optional[str]:
    """A working directory Cursor's records carry for the session (best
    effort: a `cwd`, or a single `workspace_roots` / `workspaceRoots` entry),
    or None."""
    found = set()
    for path in files:
        for rec in _json_records(path, (b'"cwd"', b"orkspace_roots", b"orkspaceRoots")):
            roots = rec.get("workspace_roots", rec.get("workspaceRoots"))
            cwd = _abs_dir(rec.get("cwd")) or (_abs_dir(roots[0]) if isinstance(roots, list) and len(roots) == 1
                                               else None)
            if cwd:
                found.add(cwd)
            if len(found) > 1:
                return None
    return found.pop() if len(found) == 1 else None


# ---------------------------------------------------------------------------
# resume: the owner resumes a session, attended, in their own terminal
# ---------------------------------------------------------------------------

class ResumeError(Exception):
    """Why `resume` cannot run (said to the owner; exit 1)."""


class ResumePlan:
    """What `resume` runs: argv in cwd (None: no directory needed)."""

    def __init__(self, sid: str, kind: str, argv: List[str], cwd: Optional[str], prompt: Optional[str],
                 note: str = ""):
        self.sid, self.kind, self.argv, self.cwd, self.prompt, self.note = sid, kind, argv, cwd, prompt, note

    def command(self, *, short: bool = False) -> str:
        """The shell-quoted command (short: the prompt as a placeholder)."""
        return " ".join("<wake prompt>" if short and a == self.prompt else shlex.quote(a) for a in self.argv)


def bound_sessions(home: Path, state: Optional[Path] = None) -> List[str]:
    """Session ids bound under `state` (a state home), or under either
    (state_homes)."""
    out = set()
    for h in ([state] if state is not None else state_homes(home)):
        try:
            names = os.listdir(str(ie.sessions_dir(h)))
        except OSError:
            continue
        out.update(n[:-5] for n in names if n.endswith(".json") and ie.valid_session(n[:-5]))
    return sorted(out)


def resolve_session(home: Path, arg: str) -> str:
    """The session `arg` names: a full UUID, or a unique prefix of at least
    PREFIX_MIN hex characters among the sessions bound under ~/.cardinal;
    only when none matches there, among the relocated (agent-writable)
    bindings, so a sandboxed agent cannot make a real prefix ambiguous or
    shadow it."""
    arg = (arg or "").strip()
    if UUID_RE.fullmatch(arg):
        return arg
    hexes = arg.replace("-", "")
    if not re.fullmatch(r"[0-9a-fA-F][0-9a-fA-F-]*", arg) or len(hexes) < PREFIX_MIN:
        raise ResumeError(f"give the session id, or at least its first {PREFIX_MIN} hex characters")
    matches = []
    for state in state_homes(home):
        matches = [s for s in bound_sessions(home, state) if UUID_RE.fullmatch(s)
                   and s.lower().startswith(arg.lower())]
        if matches:
            break
    if not matches:
        raise ResumeError(f"no bound session id starts with {arg}")
    if len(matches) > 1:
        raise ResumeError(f"{arg} is ambiguous: " + ", ".join(matches) + " (give more of the id)")
    return matches[0]


def session_binding(home: Path, sid: str) -> Optional[dict]:
    """The session's binding: under ~/.cardinal, else the relocated one."""
    for state in state_homes(home):
        b = ie.read_binding(state, sid)
        if b is not None:
            return b
    return None


def pending_prompt(home: Path, sid: str, now: float, binding: Optional[dict] = None) -> Optional[str]:
    """The WAKE_PROMPT for a decision this session has not received yet, or
    None (resume then gives no first prompt). From the waker's own ledger:
    NOTIFIED entries notified while the session was stopped (never live,
    never an earlier version's), within DECISION_MAX_AGE of now; the lowest
    seq in the investigation of the newest one. With a binding, only its
    investigation and only seqs past read_from(binding) count: a decision
    the session already consumed reaches it through its hooks anyway. The
    binding is agent-writable: a forged cursor can leave the prompt out, or
    bring back a decision already consumed, but only a verified owner
    decision for this session, notified while it was stopped, within
    DECISION_MAX_AGE."""
    try:
        entries = read_ledger(home, sid)["entries"].values()
    except ValueError:
        return None
    floor, inv_bound = 0, None
    if binding is not None:
        try:
            floor, inv_bound = read_from(binding), binding.get("investigation_id")
        except (KeyError, TypeError, ValueError):
            return None
    ok = []
    for e in entries:
        seq, t = e.get("seq"), _num(e.get("notified_at"))
        if e.get("state") != NOTIFIED or e.get("live") is not False or t is None or not 0 <= now - t <= DECISION_MAX_AGE:
            continue
        if not ie.valid_investigation(e.get("investigation_id")) or not isinstance(seq, int) or isinstance(seq, bool) \
                or seq < 1:
            continue
        if binding is not None and (e["investigation_id"] != inv_bound or seq <= floor):
            continue
        ok.append(e)
    if not ok:
        return None
    newest = max(ok, key=lambda e: (_num(e.get("notified_at")) or 0.0, e["seq"]))
    seq = min(e["seq"] for e in ok if e["investigation_id"] == newest["investigation_id"])
    try:
        return wake_prompt(seq, newest["investigation_id"])
    except ValueError:
        return None


def _which(name: str, environ: Mapping[str, str]) -> Optional[str]:
    """`name` on the owner's PATH, absolute: relative entries ("", ".") are
    skipped, so nothing is resolved against the session's directory after
    the chdir."""
    dirs = [d for d in (environ.get("PATH") or os.defpath).split(os.pathsep) if d and os.path.isabs(d)]
    found = shutil.which(name, path=os.pathsep.join(dirs)) if dirs else None
    return os.path.abspath(found) if found else None


def resume_plan(home: Path, sid: str, environ: Mapping[str, str], now: Optional[float] = None) -> ResumePlan:
    """How to resume `sid` here (raises ResumeError when it cannot be): the
    runtime and the working directory from the runtime's own records only,
    the runtime's binary from the owner's PATH, the pending decision's
    WAKE_PROMPT as the first prompt."""
    if not UUID_RE.fullmatch(sid or ""):
        raise ResumeError("not a session id (a UUID)")
    rt = runtime_kind(home, sid)
    if rt.kind == UNKNOWN:
        raise ResumeError(f"cannot tell which runtime runs session {sid[:8]}: {rt.reason}")
    prompt = pending_prompt(home, sid, time.time() if now is None else now, session_binding(home, sid))
    note = "" if prompt else "no undelivered owner decision was notified for it: resuming without a first prompt"
    if rt.kind == CODEX:
        meta = codex_session_meta(rt.files, sid)
        if meta is None:
            raise ResumeError("its Codex rollout has no session record for it")
        if meta.get("originator") not in CODEX_ORIGINATORS:
            # The Codex desktop app (or an unrecognized client): open the thread there.
            return ResumePlan(sid, CODEX, [OPEN, f"codex://threads/{sid}"], None, None,
                              "a Codex desktop session: the thread opens in the app; continue there")
        cwd = _abs_dir(meta.get("cwd"))
    elif rt.kind == CLAUDE:
        cwd = claude_cwd(rt.files, sid)
    elif rt.kind == GEMINI:
        cwd = gemini_cwd(rt.files)
    else:
        cwd = cursor_cwd(rt.files)
    binary = RESUME_BINARIES[rt.kind]
    if rt.kind == CURSOR and (not cwd or not _which(binary, environ)):
        raise ResumeError(("cursor-agent was not found on PATH" if cwd else
                           "Cursor's records do not say where this chat ran")
                          + f". Reopen it in Cursor: open its project and pick the chat (id {sid}) from the Agent "
                            "panel's history.")
    if not cwd:
        raise ResumeError(f"{LABELS[rt.kind]}'s records do not say where session {sid[:8]} ran")
    if not os.path.isdir(cwd):
        raise ResumeError(f"its working directory {cwd} no longer exists")
    exe = _which(binary, environ)
    if not exe:
        raise ResumeError(f"{binary} was not found on PATH")
    ident = sid
    if rt.kind == GEMINI:
        ident = resolve_gemini_resume_ident(sid, cwd) or ""
        if not ident or ident.startswith("-"):
            raise ResumeError("Gemini CLI cannot resume this session by id")
    argv = [exe, "resume", sid] if rt.kind == CODEX else [exe, "--resume", ident]
    return ResumePlan(sid, rt.kind, argv + ([prompt] if prompt else []), cwd, prompt, note)


def resume(home: Path, arg: str, system: System, *, print_only: bool = False) -> int:
    """`cardinal-wake resume <session> [--print]`."""
    out = system.out
    try:
        sid = resolve_session(home, arg)
        plan = resume_plan(home, sid, system.environ, system.now())
    except ResumeError as e:
        out(f"cardinal-wake: {e}")
        return 1
    live = live_signals(home, sid, runtime_kind(home, sid), system, system.now())
    if live:
        out(f"cardinal-wake: warning: session {sid[:8]} looks live ({'; '.join(live)}); if its window is open, "
            "switch to it instead")
    label = LABELS.get(plan.kind, plan.kind) + (" (experimental)" if plan.kind == CURSOR else "")
    if print_only:
        out(f"{label} session {sid}")
        out(f"cwd: {plan.cwd if plan.cwd else '(none needed)'}")
        out(f"command: {plan.command()}")
        if plan.note:
            out(plan.note)
        return 0
    out(f"Resuming {label} session {sid[:8]}" + (f" in {plan.cwd}" if plan.cwd else "")
        + (f" ({plan.note})" if plan.note else ""))
    try:
        system.exec(plan.argv, plan.cwd)
    except OSError as e:
        out(f"cardinal-wake: could not run {plan.argv[0]} ({type(e).__name__})")
        return 1
    return 0


def resume_hint(sid: str) -> str:
    """The command a notification names (the stable runner `enable` installs)."""
    return f"~/.cardinal/wake/lib/cardinal-wake resume {sid[:8]}"


def lib_problem(home: Path) -> Optional[str]:
    """Why the stable copy in ~/.cardinal/wake/lib cannot run `resume` as
    this version does, or None: not installed, or its runner, session_wake
    or owner_input differ from this copy (a plugin updated since `enable`;
    inside the daemon this module IS that copy)."""
    lib = lib_dir(home)
    here = Path(__file__).resolve().parent
    try:
        if runner_path(home).read_text(encoding="utf-8") != RUNNER:
            return "the installed runner is out of date: run cardinal-wake enable again"
        for name in ("session_wake.py", "owner_input.py"):
            if (lib / "cardinal_core" / name).read_bytes() != (here / name).read_bytes():
                return "the installed runner is out of date: run cardinal-wake enable again"
    except OSError:
        return "the stable runner is not installed: run cardinal-wake enable"
    return None


def _owned_dir(path: Path) -> bool:
    """`path` is a real directory (never a symlink) owned by this user."""
    try:
        st = os.lstat(str(path))
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()


def safe_resume_dir(home: Path, *, create: bool = False) -> Optional[Path]:
    """~/.cardinal/wake/resume when it and ~/.cardinal/wake are real
    directories this user owns (created 0700 with create), else None."""
    wake, d = wake_dir(home), resume_dir(home)
    if create and not os.path.lexists(str(d)) and _owned_dir(wake):
        with contextlib.suppress(OSError):
            os.mkdir(str(d), 0o700)
    if not (_owned_dir(wake) and _owned_dir(d)):
        return None
    with contextlib.suppress(OSError):
        os.chmod(str(d), 0o700)
    return d


def write_resume_file(home: Path, sid: str, config: dict) -> Optional[str]:
    """~/.cardinal/wake/resume/<sid>.command (0700): execs the stable runner's
    `resume <sid>` with the vetted interpreter, so a click opens Terminal
    on it and never points into a plugin directory. None (no click action)
    when sid is not a UUID, the interpreter fails its vetting
    (binaries_problem), the stable copy is missing or out of date
    (lib_problem), or the resume directory is not a real one of this user's."""
    if not UUID_RE.fullmatch(sid or "") or binaries_problem(config, home) or lib_problem(home):
        return None
    d = safe_resume_dir(home, create=True)
    if d is None:
        return None
    text = ("#!/bin/sh\n# cardinal-wake: resume this session in this terminal (written by the waker)\n"
            f"exec {shlex.quote(config['python'])} -I -S {shlex.quote(str(runner_path(home)))} resume "
            f"{shlex.quote(sid)}\n")
    path = d / f"{sid}.command"
    tmp = d / f".{sid}.{os.getpid()}.tmp"
    try:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(str(tmp), 0o700)
        os.replace(str(tmp), str(path))
    except OSError:
        return None
    return str(path)


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------

def live_signals(home: Path, sid: str, rt: Runtime, system: System, now: float) -> List[str]:
    """Why the session may still be running (empty: it is stopped). The
    poller status and .active of both state homes count (any home saying
    live is enough)."""
    why = []
    homes = state_homes(home)
    for h in homes:
        st = poller.read_status(h, sid)
        anchor = st.get("anchor")
        if isinstance(anchor, int) and not isinstance(anchor, bool) and anchor > 1 and system.alive(anchor):
            started = system.process_start(anchor)
            try:
                written = poller.status_path(h, sid).stat().st_mtime
            except OSError:
                written = None
            if started is None or written is None or started < written:
                why.append("its agent process is running")
                break
    table = system.process_table()
    if table is None:
        why.append("the process table could not be read")
    elif any(sid in args for pid, args in table if pid not in (os.getpid(), os.getppid())):
        why.append("a running process names the session")
    for p in rt.files:
        with contextlib.suppress(OSError):
            if now - p.stat().st_mtime <= LIVE_WINDOW:
                why.append("its transcript changed in the last 2 minutes")
                break
    if any(now - ie.last_activity(h, sid) <= LIVE_WINDOW for h in homes):
        why.append("it used a tool in the last 2 minutes")
    return why


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

def state_homes(home: Path) -> Tuple[Path, Path]:
    """Where hooks keep per-session Cardinal state: the user's home
    (~/.cardinal) and the relocated home of sessions run inside a seatbelt
    sandbox (paths.sandboxed_home). Both hold agent-writable hints."""
    home = paths.unsandboxed_home(Path(home))
    return home, paths.sandboxed_home(home)


def candidate_sessions(home: Path, now: float) -> List[Tuple[str, dict, float, Path]]:
    """(sid, binding, binding mtime, state home) of sessions that may be
    notified: touched within CANDIDATE_WINDOW, the investigation's author,
    owner_decision on. A session bound under ~/.cardinal is decided there
    alone, stale or not (the relocated binding, which a sandboxed agent can
    write, never shadows it). Relocated bindings count only for sessions
    with no Codex or Claude Code records (whose hooks never run in a
    seatbelt sandbox), and are as untrusted as any binding."""
    out, seen = [], set()
    plain, boxed = state_homes(home)
    for state in (plain, boxed):
        d = ie.sessions_dir(state)
        try:
            names = os.listdir(str(d))
        except OSError:
            continue
        for name in sorted(names):
            sid = name[:-5] if name.endswith(".json") else ""
            if not ie.valid_session(sid) or sid in seen:
                continue
            seen.add(sid)   # before any other check: a stale real binding is never replaced by a relocated one
            if state == boxed and (codex_rollouts(home, sid) or claude_transcripts(home, sid)):
                continue
            try:
                mtime = (d / name).stat().st_mtime
            except OSError:
                continue
            if now - mtime > CANDIDATE_WINDOW:
                continue
            b = ie.read_binding(state, sid)
            if b is None or b.get("is_author") is not True or not ie.capability(b, "owner_decision"):
                continue
            out.append((sid, b, mtime, state))
    return out


def read_from(binding: dict) -> int:
    """Where the session's unconsumed events start: its cursor, or before
    the oldest decision it was shown and has not acknowledged."""
    seqs = [p["seq"] for p in binding.get("pending_decisions") or []
            if isinstance(p, dict) and isinstance(p.get("seq"), int) and not isinstance(p.get("seq"), bool)]
    return max(0, min([int(binding["cursor"])] + [s - 1 for s in seqs]))


WAKE_NOT_REQUESTED = "decision posted without resume requested"
DECISION_OLD = "the decision is older than 7 days (or undated)"


def decision_requests_wake(e: Any) -> bool:
    """The owner asked for the session to be resumed (conductor#2110: the
    Composer's "Resume this session if it has stopped" sets payload.wake,
    which the server allows only with to_session_id). Exactly the JSON true."""
    p = e.get("payload") if isinstance(e, dict) else None
    return isinstance(p, dict) and p.get("wake") is True


def wake_decisions(events: list, sid: str, binding: dict) -> List[dict]:
    """The decisions in `events` that may be notified for `sid` (§5.2,
    before the ledger and caps): verified, addressed to it, and still
    deliverable (unacknowledged by it, owner_decision on)."""
    inv = binding["investigation_id"]
    out = []
    for e in ie.deliverable(events, sid, inv, binding):
        seq = e.get("seq")
        if ie.verified_owner_decision(e) and e.get("to_session_id") == sid and isinstance(seq, int) \
                and not isinstance(seq, bool) and seq > 0:
            out.append(e)
    return sorted(out, key=lambda e: e["seq"])


def created_epoch(e: dict) -> Optional[float]:
    """The server's created_at of an event, as epoch seconds, or None."""
    import calendar
    v = e.get("created_at")
    if not isinstance(v, str):
        return None
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?Z", v)
    if not m:
        return None
    try:
        return float(calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


def _connection_for(home: Path, binding: dict, prefer: str, system: System) -> Tuple[dict, str]:
    """The plugin connection whose org is the binding's: the session's own
    runtime first. ({}, "") when none matches."""
    order = [prefer] + [r for r in RUNTIMES if r != prefer] if prefer in RUNTIMES else list(RUNTIMES)
    for runtime in order:
        conn = system.connection(runtime, home) or {}
        if not (conn.get("origin") and conn.get("org") and conn.get("key")):
            continue
        if binding.get("org") in (None, conn.get("org")):
            return conn, runtime
    return {}, ""


def _client(runtime: str, config: dict) -> str:
    """The X-Cardinal-Client of the plugin whose key reads: <name>/<version
    of that plugin when it ran enable>, the name alone otherwise."""
    version = (config.get("versions") or {}).get(runtime)
    from .evidence import client_string
    try:
        return client_string("claude-plugin" if runtime == CLAUDE else runtime, version)
    except ValueError:
        return "cardinal-wake"


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------

def _where(binding: Optional[dict], origin: Optional[str]) -> str:
    """'inv_… on <host>': built only from the investigation id and the
    plugin connection's origin, never from a link the (agent-writable)
    binding carries."""
    import urllib.parse
    inv = (binding or {}).get("investigation_id")
    if not ie.valid_investigation(inv):
        return ""
    host = ""
    if isinstance(origin, str) and origin.startswith("https://"):
        h = (urllib.parse.urlsplit(origin).hostname or "").lower()
        host = h if re.fullmatch(r"[a-z0-9.-]{1,253}", h) else ""
    return f"{inv} on {host}" if host else inv


def notification_text(binding: Optional[dict], sid: str, runtime: str, origin: Optional[str], *,
                      live: bool, click: bool = False, stale: Optional[str] = None) -> str:
    """The runtime, the session's short id, the investigation, and what to
    do; never decision text. A resume command only for a stopped session of
    a known runtime with an up-to-date stable runner (stale: why not)."""
    where = _where(binding, origin)
    label = LABELS.get(runtime, LABELS[UNKNOWN])
    head = f"Owner decision for {label} session {sid[:8]}" + (f" ({where})." if where else ".")
    if live:
        return f"{head} It is still open: switch to that window."
    if runtime not in RUNTIMES:
        return f"{head} Its runtime is not recognized on this Mac: open the session yourself to see it."
    if stale:
        return f"{head} To resume it from here, {stale.split(': ', 1)[-1]} ({stale.split(':', 1)[0]})."
    return f"{head} Resume: {resume_hint(sid)}" + (" (or click to resume in Terminal)" if click else "")


def _notify(system: System, message: str, open_file: Optional[str] = None) -> None:
    with contextlib.suppress(Exception):
        system.notifier(TITLE, message[:400], open_file)


def _report(system: System, report: list, sid: str, seq: Optional[int], action: str, why: str = "") -> None:
    report.append({"session_id": sid, "seq": seq, "action": action, "reason": why})
    system.out(f"{sid[:8]} #{seq if seq is not None else '-'} {action}" + (f": {why}" if why else ""))


def run_once(home: Path, system: Optional[System] = None, *, dry_run: bool = False) -> List[dict]:
    """One pass of the waker (see the module docstring). dry_run: reads
    Cardinal and reports what it would do; writes and notifies nothing.
    Returns the report (one dict per action)."""
    system = system or System()
    report: list = []
    config = read_config(home)
    if not dry_run:
        with contextlib.suppress(OSError):
            prune_files(home, system.now())
    if not config["enabled"]:
        if not dry_run:
            report_all_wake_states(home, system, config)
        _report(system, report, "-", None, "skip", "wake is not enabled (cardinal-wake enable)")
        return report
    now = system.now()
    polls = _read_json(poll_state_path(home))
    for sid, binding, mtime, state in candidate_sessions(home, now):
        if now - mtime > RECENT_WINDOW and 0 <= now - (_num(polls.get(sid)) or 0.0) < OLD_CHECK_EVERY:
            continue
        polls[sid] = now
        try:
            consider(home, sid, binding, system, config, report, dry_run=dry_run, state=state)
        except Exception as e:
            _report(system, report, sid, None, "error", f"{type(e).__name__}")
    if not dry_run:
        with contextlib.suppress(OSError, ValueError):
            _private_dir(wake_dir(home))
            ie.write_json(poll_state_path(home), {k: v for k, v in polls.items()
                                                   if isinstance(v, (int, float)) and now - v < CANDIDATE_WINDOW})
        report_all_wake_states(home, system, config)
    return report


def consider(home: Path, sid: str, binding: dict, system: System, config: dict, report: list, *,
             dry_run: bool = False, state: Optional[Path] = None) -> None:
    """One candidate session (§5.2). state: the home its binding was found
    under (state_homes); the waker's own files always live under `home`."""
    state = state or home
    now = system.now()
    led = read_ledger(home, sid)
    rt = runtime_kind(home, sid)
    conn, conn_runtime = _connection_for(home, binding, rt.kind, system)
    if not conn:
        return
    inv = binding["investigation_id"]
    try:
        events, complete = system.fetch(conn, inv, sid, read_from(binding), _client(conn_runtime, config))
    except Exception as e:
        _report(system, report, sid, None, "skip", f"could not read the investigation ({type(e).__name__})")
        return
    if not complete:
        _report(system, report, sid, None, "skip", "the investigation's events could not all be read this pass")
        return
    pending = [e for e in wake_decisions(events, sid, binding) if not decision_seen(led, inv, e["seq"])]
    if not pending:
        return
    live = live_signals(home, sid, rt, system, now)
    # Review 7 F2: ascending seq, stopping at the first decision that cannot be recorded (the caps,
    # a busy lock). A notification records an entry, which raises the high-water mark, so a higher
    # decision is never recorded ahead of a lower one that has no entry yet.
    for e in pending:
        created = created_epoch(e)
        if created is None or now - created > DECISION_MAX_AGE:
            why = DECISION_OLD
        elif not decision_requests_wake(e):
            why = WAKE_NOT_REQUESTED
        elif live:
            why = "the session is live (" + "; ".join(live) + "): it will see the decision itself"
        else:
            why = "the session is stopped: notified with its resume command"
        done = notify_decision(home, sid, binding, e["seq"], rt.kind, why, system, report, config, live=bool(live),
                               dry_run=dry_run, origin=conn.get("origin"), state=state)
        if done not in ("notified", "seen", "dry_run"):
            break


def notify_decision(home: Path, sid: str, binding: dict, seq: int, kind: str, why: str, system: System,
                    report: list, config: dict, *, live: bool = False, dry_run: bool = False,
                    origin: Optional[str] = None, state: Optional[Path] = None) -> str:
    """Record #seq as NOTIFIED for `sid` and notify once (§5.4), under the
    session's lock (then the global lock, for the caps). Returns notified |
    seen | cap | busy | dry_run."""
    if dry_run:
        _report(system, report, sid, seq, "would notify", why)
        return "dry_run"
    inv = binding["investigation_id"]
    if system.before_claim is not None:
        system.before_claim(sid, seq)
    cap_notice = False
    with session_lock(home, sid) as got:
        if not got:
            return "busy"
        now = system.now()
        led = read_ledger(home, sid)
        if decision_seen(led, inv, seq):
            return "seen"
        with global_lock(home) as gotg:
            if not gotg:
                return "busy"
            capped = _counted(led, now) >= SESSION_DAILY_CAP or global_count(home, now) >= GLOBAL_DAILY_CAP
            if capped:   # no entry: the decision stays pending (and blocks higher ones) until the window passes
                last = _num(led.get("cap_notified_at"))
                if last is None or now - last >= DAY:
                    led["cap_notified_at"] = now
                    write_ledger(home, sid, led, now)
                    cap_notice = True
            else:
                entry = {"seq": seq, "investigation_id": inv, "runtime": kind, "reports": [], "notified_at": now,
                         "live": live, "sandboxed_state": paths.is_sandboxed_home(state or home)}
                transition(entry, DECISION_SEEN, now)
                transition(entry, NOTIFIED, now, why)
                add_entry(led, entry)
                write_ledger(home, sid, led, now)
    if capped:
        if cap_notice:   # once a day per session
            _notify(system, f"Owner decisions for {LABELS.get(kind, LABELS[UNKNOWN])} session {sid[:8]}: the daily "
                            "notification cap is reached; the rest follow later.")
        _report(system, report, sid, seq, "capped", "the daily notification cap is reached; reconsidered later")
        return "cap"
    offer = not live and kind in RUNTIMES   # resume_plan refuses an unknown runtime
    stale = lib_problem(home) if offer else None
    open_file = write_resume_file(home, sid, config) if offer and not stale and system.click_notifier() else None
    _notify(system, notification_text(binding, sid, kind, origin, live=live, click=bool(open_file),
                                      stale=stale), open_file)
    _report(system, report, sid, seq, "notified", why)
    return "notified"


# ---------------------------------------------------------------------------
# session.wake reports (conductor#2110)
# ---------------------------------------------------------------------------

WAKE_REPORT_STATES = {NOTIFIED: "notified"}   # the launch states of earlier versions are never reported again
WAKE_REPORT_TRIES = 3
WAKE_RUNTIMES = ("codex", "claude", "gemini", "cursor")
# The only reasons a report may carry: readable by every org member and read
# grantee, so a fixed vocabulary, never the waker's own words, decision text
# or a key. (substring of the history note, reason), first match wins.
_WAKE_REASON_RULES = (
    (WAKE_NOT_REQUESTED, WAKE_NOT_REQUESTED),
    ("older than 7 days", "decision older than 7 days"),
    ("the session is live", "session is live"),
)
# A stopped session is notified with its resume command: it is never resumed unattended.
WAKE_REASON_DEFAULT = "session cannot be resumed unattended"
WAKE_REASONS = frozenset(r for _, r in _WAKE_REASON_RULES) | {WAKE_REASON_DEFAULT}


def wake_reason(state: str, note: Any) -> Optional[str]:
    """The fixed-vocabulary reason for a report of `state`, from the waker's
    history note (never the note itself), or None."""
    text = note if isinstance(note, str) else ""
    for needle, reason in _WAKE_REASON_RULES:
        if needle in text:
            return reason
    return WAKE_REASON_DEFAULT if state == NOTIFIED else None


def wake_report_key(investigation_id: str, sid: str, seq: int, state: str) -> str:
    """wake:<sha256(investigation:sid:seq)[:40]>:<state>: one report per
    decision, session and state (A-Za-z0-9._:-, at most 128, never a
    server-reserved prefix)."""
    import hashlib
    h = hashlib.sha256(f"{investigation_id}:{sid}:{seq}".encode()).hexdigest()[:40]
    return f"wake:{h}:{state}"


def report_all_wake_states(home: Path, system: System, config: dict) -> None:
    for sid in ledger_sids(home):
        with contextlib.suppress(Exception):   # reporting never stops or changes a pass
            report_wake_states(home, sid, system, config)


def report_wake_states(home: Path, sid: str, system: System, config: dict) -> None:
    """Post the session.wake reports this session's entries still owe (best
    effort): "notified" once per entry, only for entries created with a
    reports list, only when the binding advertises session_wake, with the
    plugin connection whose org is the binding's. An older server (400
    unknown_event_type), a missing route (404), a refusal (403) or the
    limit (409 event_limit_reached) stops reports for that investigation
    (recorded in the ledger); a decision the server cannot match (404
    wake_decision_not_found, 422, 400 invalid_event) ends that entry's
    reports; 409 idempotency_conflict counts as reported; anything else is
    retried on later passes, WAKE_REPORT_TRIES times per entry."""
    with session_lock(home, sid, wait=0.5) as got:
        if not got:
            return
        led = read_ledger(home, sid)
        off = led.get("wake_reports_off") if isinstance(led.get("wake_reports_off"), dict) else {}
        plain_home, boxed_home = state_homes(home)
        changed = False
        for entry in led["entries"].values():
            reports = entry.get("reports")
            inv, seq = entry.get("investigation_id"), entry.get("seq")
            if not isinstance(reports, list) or entry.get("reports_ended") or inv in off \
                    or not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
                continue
            states = []
            for line in entry.get("history") or []:
                st = WAKE_REPORT_STATES.get(line.get("state")) if isinstance(line, dict) else None
                if st and st not in reports and st not in [s for s, _ in states]:
                    states.append((st, wake_reason(line.get("state"), line.get("note"))))
            if not states:
                continue
            binding = ie.read_binding(boxed_home if entry.get("sandboxed_state") else plain_home, sid)
            if not binding or binding.get("investigation_id") != inv or not ie.capability(binding, "session_wake"):
                continue
            runtime = entry.get("runtime") if entry.get("runtime") in WAKE_RUNTIMES else "unknown"
            conn, conn_runtime = _connection_for(home, binding, runtime, system)
            if not conn:
                continue
            client = _client(conn_runtime, config)
            for st, reason in states:
                payload = {"decision_seq": seq, "state": st, "runtime": runtime}
                if reason in WAKE_REASONS:
                    payload["reason"] = reason
                changed = True
                try:
                    system.post_wake(conn, inv, sid, payload, wake_report_key(inv, sid, seq, st), client)
                except sync.ServerError as e:
                    body = e.body if isinstance(getattr(e, "body", None), dict) else {}
                    err, status = body.get("error"), getattr(e, "status", None)
                    if err == "idempotency_conflict":
                        reports.append(st)
                        continue
                    if err in ("unknown_event_type", "event_limit_reached") or status == 403 \
                            or (status == 404 and err != "wake_decision_not_found"):
                        off[inv] = str(err or status)[:60]
                        break
                    if status in (400, 404, 422):
                        entry["reports_ended"] = str(err or status)[:60]
                        break
                    entry["report_tries"] = int(entry.get("report_tries") or 0) + 1
                    break
                except Exception:
                    entry["report_tries"] = int(entry.get("report_tries") or 0) + 1
                    break
                reports.append(st)
            if int(entry.get("report_tries") or 0) >= WAKE_REPORT_TRIES and not entry.get("reports_ended"):
                entry["reports_ended"] = "gave up after retries"
        if changed:
            led["wake_reports_off"] = off
            write_ledger(home, sid, led, system.now())


def prune_files(home: Path, now: float) -> None:
    """Keep the newest WAKE_LOGS_KEEP logs (earlier versions' per-wake logs;
    never launchd.log) and delete resume files untouched for RESUME_FILE_KEEP."""
    d = logs_dir(home)
    with contextlib.suppress(OSError):
        names = [n for n in os.listdir(str(d)) if n.endswith(".log") and n != "launchd.log"]
        if len(names) > WAKE_LOGS_KEEP:
            files = sorted(names, key=lambda n: (d / n).stat().st_mtime if (d / n).exists() else 0.0)
            for n in files[:len(files) - WAKE_LOGS_KEEP]:
                with contextlib.suppress(OSError):
                    (d / n).unlink()
    r = safe_resume_dir(home)
    if r is None:
        return
    with contextlib.suppress(OSError):
        for n in os.listdir(str(r)):
            p = r / n
            with contextlib.suppress(OSError):
                st = p.lstat()
                if n.endswith(".command") and stat.S_ISREG(st.st_mode) and now - st.st_mtime > RESUME_FILE_KEEP:
                    p.unlink()


# ---------------------------------------------------------------------------
# Install (launchd) and the CLI
# ---------------------------------------------------------------------------

RUNNER = '''#!/usr/bin/env python3
"""cardinal-wake, the stable copy launchd (run --once) and a notification's
click (resume <session>) run; written by `cardinal-wake enable` with
cardinal_core next to it, re-written by every enable."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cardinal_core import session_wake  # noqa: E402

if __name__ == "__main__":
    sys.exit(session_wake.cli_main(sys.argv[1:]))
'''


def install_lib(home: Path, core_dir: Optional[Path] = None) -> Path:
    """Copy cardinal_core (this package) and the runner into
    ~/.cardinal/wake/lib, replacing what was there. Returns the runner."""
    core_dir = core_dir or Path(__file__).resolve().parent
    lib = _private_dir(lib_dir(home))
    tmp = lib / f".cardinal_core.{os.getpid()}.tmp"
    if tmp.exists():
        shutil.rmtree(str(tmp))
    shutil.copytree(str(core_dir), str(tmp), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    dest = lib / "cardinal_core"
    old = lib / f".cardinal_core.{os.getpid()}.old"
    if dest.exists():
        os.replace(str(dest), str(old))
    os.replace(str(tmp), str(dest))
    if old.exists():
        shutil.rmtree(str(old), ignore_errors=True)
    runner = runner_path(home)
    tmp_runner = lib / f".cardinal-wake.{os.getpid()}.tmp"
    tmp_runner.write_text(RUNNER, encoding="utf-8")
    os.chmod(str(tmp_runner), 0o700)
    os.replace(str(tmp_runner), str(runner))
    return runner


def plist_bytes(home: Path, python: str, runner: Path, path_env: str) -> bytes:
    log = logs_dir(home) / "launchd.log"
    return plistlib.dumps({
        "Label": PLIST_LABEL,
        "ProgramArguments": [python, "-I", "-S", str(runner), "run", "--once"],
        "StartInterval": RUN_INTERVAL,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {"PATH": path_env},
    })


def launchctl_default(args: List[str]) -> Tuple[int, str]:
    try:
        r = subprocess.run([LAUNCHCTL] + args, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


def _domain() -> str:
    return f"gui/{os.getuid()}"


LAUNCH_REMOVED = ("cardinal-wake: unattended launch was removed; the waker only notifies, and "
                  "`cardinal-wake resume <session>` resumes a session in your own terminal (run enable without "
                  "--launch)")


def enable(home: Path, *, environ: Mapping[str, str], launchctl: Callable[[List[str]], Tuple[int, str]],
           out: Callable[[str], None], now: float, version: Optional[str] = None,
           runtime: Optional[str] = None, core_dir: Optional[Path] = None) -> int:
    """Install or update the launchd agent and the config."""
    old = read_config(home)
    # B1: the daemon never runs anything an agent can write. The interpreter
    # is this Python or the real one behind /usr/bin/python3, outside $HOME,
    # git work trees and temp dirs, run with -I -S; PATH is the system dirs
    # plus its directory only.
    python, why = pick_python(home)
    if python is None:
        out(f"cardinal-wake: no Python the daemon may run: {why}. Install python3 outside your home directory "
            "and your repositories (or the Xcode command line tools for /usr/bin/python3).")
        return 2
    path_env = daemon_path(os.path.dirname(python))
    runner = install_lib(home, core_dir)
    _private_dir(logs_dir(home))
    versions = dict(old.get("versions") or {})
    if runtime in RUNTIMES and version:
        versions[runtime] = version
    write_config(home, {"enabled": True, "python": python, "path_env": path_env,
                        "binaries": {python: fingerprint(python)},
                        "enabled_at": old.get("enabled_at") if old["enabled"] and old.get("enabled_at") else _iso(now),
                        "versions": versions})
    plist = plist_path(home)
    plist.parent.mkdir(parents=True, exist_ok=True)
    tmp = plist.with_name(f".{plist.name}.tmp")
    tmp.write_bytes(plist_bytes(home, python, runner, path_env))
    os.chmod(str(tmp), 0o644)
    os.replace(str(tmp), str(plist))
    launchctl(["bootout", f"{_domain()}/{PLIST_LABEL}"])
    rc, said = launchctl(["bootstrap", _domain(), str(plist)])
    if rc != 0:
        out(f"cardinal-wake: launchctl bootstrap failed ({said or rc}); the agent is written to {plist} but not "
            "loaded")
        return 1
    out(f"Wake is on: launchd runs {runner} every {RUN_INTERVAL} s ({plist}).")
    out("It notifies you of an owner decision you post on an Investigation page addressed to one of your "
        "sessions. Agents cannot post one.")
    out(NOTIFY_TEXT)
    notifier = terminal_notifier()
    out(f"Notifications: {notifier}; a click resumes the session in Terminal." if notifier else
        "Notifications: osascript. Install terminal-notifier (brew install terminal-notifier) to resume a session "
        "with one click.")
    if runtime == CLAUDE:
        out("To answer a Claude Code session that is still running from another device, start it with "
            "`claude --remote-control`.")
    return 0


def disable(home: Path, *, launchctl: Callable[[List[str]], Tuple[int, str]], out: Callable[[str], None]) -> int:
    launchctl(["bootout", f"{_domain()}/{PLIST_LABEL}"])
    with contextlib.suppress(FileNotFoundError):
        plist_path(home).unlink()
    if config_path(home).exists():
        config = _read_json(config_path(home))
        config["enabled"] = False
        write_config(home, config)
    out("Wake is off: the launchd agent is removed.")
    return 0


def status(home: Path, system: System, out: Callable[[str], None]) -> int:
    """The opt-in, recent notifications and candidate sessions with their
    resume commands; local files only, never decision text."""
    now = system.now()
    c = read_config(home)
    out(f"wake: {'on' if c['enabled'] else 'off'}"
        f"{'; enabled ' + str(c['enabled_at']) if c['enabled'] and c['enabled_at'] else ''}")
    out(f"launchd agent: {'installed' if plist_path(home).exists() else 'not installed'} ({plist_path(home)})")
    out(f"python: {c.get('python') or 'not vetted'} (-I -S); PATH: {c.get('path_env') or 'not vetted'}")
    if c["enabled"]:
        problem = binaries_problem(c, home)
        out("interpreter: " + (problem if problem else "unchanged since enable"))
    out("stable runner: " + (lib_problem(home) or f"{runner_path(home)} (up to date)"))
    notifier = system.click_notifier()
    out("notifier: " + (f"{notifier} (click to resume)" if notifier else "osascript (no click action)"))
    rows = []
    for sid in ledger_sids(home):
        for e in read_ledger(home, sid)["entries"].values():
            rows.append((_num(e.get("updated_at")) or 0.0, sid, e))
    rows.sort(key=lambda r: r[0], reverse=True)
    out("recent notifications:" if rows else "recent notifications: none")
    for t, sid, e in rows[:20]:
        out(f"  {_iso(t)} {sid[:8]} #{e.get('seq')} {e.get('state')}"
            + (f": {e.get('reason')}" if e.get("reason") else ""))
    cands = candidate_sessions(home, now)
    out("candidate sessions:" if cands else "candidate sessions: none")
    for sid, b, _, _ in cands:
        try:
            plan = resume_plan(home, sid, system.environ, now)
        except ResumeError as e:
            out(f"  {sid[:8]} {LABELS.get(runtime_kind(home, sid).kind, UNKNOWN)}: cannot resume here: {e}")
            continue
        out(f"  {sid[:8]} {LABELS.get(plan.kind, plan.kind)}: {resume_hint(sid)} -> {plan.command(short=True)}"
            + (f" (in {plan.cwd})" if plan.cwd else ""))
    return 0


def _plugin_version(plugin_root: Optional[Path]) -> Optional[str]:
    if plugin_root is None:
        return None
    for manifest in glob.glob(str(plugin_root / ".*plugin" / "plugin.json")):
        v = _read_json(Path(manifest)).get("version")
        if isinstance(v, str):
            return v
    return None


def cli_main(argv: Optional[List[str]] = None, *, runtime: Optional[str] = None, plugin_root: Optional[Path] = None,
             home: Optional[Path] = None, system: Optional[System] = None,
             launchctl: Optional[Callable[[List[str]], Tuple[int, str]]] = None,
             platform: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> int:
    """cardinal-wake: enable | disable | status | run [--once] [--dry-run] |
    resume <session> [--print]. Run it in your own terminal."""
    parser = argparse.ArgumentParser(
        prog="cardinal-wake",
        description="Owner-woken sessions: when you post an owner decision on an Investigation page addressed "
                    "to one of your sessions, notify you on this Mac; `resume` resumes that session in your own "
                    "terminal. Nothing is launched unattended. Run it in your own terminal, not through an agent.")
    sub = parser.add_subparsers(dest="command")
    p_enable = sub.add_parser("enable", help="install the per-user launchd agent (macOS)")
    p_enable.add_argument("--launch", action="store_true", help=argparse.SUPPRESS)   # removed in v4: refused
    sub.add_parser("disable", help="remove the launchd agent")
    sub.add_parser("status", help="the opt-in, recent notifications and candidate sessions with their resume commands")
    p_run = sub.add_parser("run", help="look for addressed owner decisions (launchd runs run --once)")
    p_run.add_argument("--once", action="store_true", help="one pass, then exit")
    p_run.add_argument("--dry-run", action="store_true", help="print what would happen; notify nothing")
    p_resume = sub.add_parser(
        "resume", help="resume a session in this terminal with its owner decision as the first prompt (Claude "
                       "Code, Codex CLI, Gemini CLI; Cursor's cursor-agent: experimental; a Codex desktop "
                       "session opens in the app)")
    p_resume.add_argument("session", help=f"the session id, or a unique prefix of at least {PREFIX_MIN} hex "
                                          "characters among bound sessions")
    p_resume.add_argument("--print", action="store_true", dest="print_only",
                          help="print the command and its directory; run nothing")
    args = parser.parse_args(argv)
    home = home or ie.home_dir()
    environ = os.environ if environ is None else environ
    system = system or System(environ=environ)
    launchctl = launchctl or launchctl_default
    platform = platform or sys.platform
    out = system.out
    if args.command == "enable" and args.launch:
        out(LAUNCH_REMOVED)
        return 2
    if args.command in ("enable", "disable") and platform != "darwin":
        out("cardinal-wake: wake runs as a launchd agent, on macOS only")
        return 2
    if args.command == "enable":
        return enable(home, environ=environ, launchctl=launchctl, out=out, now=system.now(),
                      version=_plugin_version(plugin_root), runtime=runtime)
    if args.command == "disable":
        return disable(home, launchctl=launchctl, out=out)
    if args.command == "status":
        return status(home, system, out)
    if args.command == "resume":
        return resume(home, args.session, system, print_only=args.print_only)
    if args.command == "run":
        _trim_launchd_log(home)
        if args.once or args.dry_run:
            report = run_once(home, system, dry_run=args.dry_run)
            if args.dry_run and not report:
                out("nothing to do: no addressed owner decision is waiting")
            return 0
        while True:  # pragma: no cover - a foreground loop for debugging
            run_once(home, system)
            time.sleep(RUN_INTERVAL)
    parser.print_help()
    return 2


def _trim_launchd_log(home: Path) -> None:
    log = logs_dir(home) / "launchd.log"
    with contextlib.suppress(OSError):
        if log.stat().st_size > MAX_LAUNCHD_LOG:
            with open(str(log), "w"):
                pass
