#!/usr/bin/env python3
"""Render Cardinal storyboard preview bundles to PNGs with your local Chromium.

storyboard__preview does not render anything on the server. For each scene it
returns a reference to one self-contained HTML page (the "preview bundle":
preview_bundle {url?, path, bytes, sha256, revision} or {unavailable}). This
script downloads each page with your Cardinal MCP key, loads it in a local,
sandboxed, network-locked headless Chromium, steps through the scene's reveal
steps and writes one PNG per step for Claude to Read and critique.

Rendering is authoring feedback, not a publish check: publish trust comes only
from maestro's deterministic validation. If no usable Chromium is found, this
script says so and exits 3. Skipping the preview is a quality problem, not an
error. Keep authoring.

Contract: conductor docs/specs/storyboard-preview-bundle.md. The driver follows
conductor packages/maestro/src/storyboard/harness/render.ts (renderBundlePage)
over the Chrome DevTools Protocol on --remote-debugging-pipe (standard library
only; no Playwright, no websocket, no debugging port).

Usage:
  render_preview.py --from-json preview.json [--scene ID]... [--theme light|dark]
  render_preview.py < preview.json
  render_preview.py --html page.html [--html other.html]      # a local bundle file
  render_preview.py --from-json preview.json --cover <scene>  # link-preview cover
  render_preview.py --static-html mock.html --png mock.png [--viewport 560x640] [--dpr 2]

--cover renders one scene's LAST reveal step at card.cover_render's size
(1200x630, DPR 1) to <scene>-cover.png, the image a link preview shows for a
cover_scene; its JSON line has step "cover". --static-html screenshots a local
static page (the plugin's link-preview mock) with scripts disabled, in the same
sandboxed, network-locked Chromium; nothing is fetched from Cardinal.

Input: the storyboard__preview result (the JSON object with storyboard_id,
scenes[].id, scenes[].preview_bundle, local_preview). The MCP wrapper
{structuredContent: {...}} or {content: [{type: "text", text: "<json>"}]} is
accepted too. Only storyboard_id, revision and scenes[].{id, preview_bundle}
are needed, so a trimmed copy works.

Output (stdout): one JSON object per line.
  per rendered step:  {scene_id, step, steps, png, state, height, error: null,
                       frame_errors, protocol_errors}
  per scene problem:  {scene_id, step: null, ..., png: null, error: "<why>"}
  last line:          {"summary": {...}}
PNGs go to ~/.claude/cardinal/storyboards/<storyboard_id>/r<revision>/
<scene>-<step>.png (-dark before .png for --theme dark), mode 0600, overwritten
per revision. --out DIR overrides the directory.

Exit codes:
  0  ran (per-scene errors, {unavailable} scenes and skipped scenes are in the
     output; exit 0 does not mean every scene rendered)
  2  nothing rendered because of input, connection, auth or fetch problems
     (not connected, wrong org, stale revision, upgrade Cardinal, ...)
  3  no usable local Chromium, unsupported platform (Windows) or Chromium could
     not start with its sandbox on: skip the preview, tell the user, keep
     authoring

Chromium: $CARDINAL_CHROMIUM, $PUPPETEER_EXECUTABLE_PATH or $CHROME_PATH, then
Google Chrome / Chromium / Chrome Canary / Brave / Edge in the usual places for
this OS, then the Playwright and Puppeteer download caches. Chrome 112 or newer
(--headless=new) is required. Chromium's OS sandbox always stays on.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

EXIT_OK = 0
EXIT_FETCH = 2
EXIT_NO_CHROMIUM = 3

MIN_CHROME_MAJOR = 112  # --headless=new

# conductor packages/maestro/src/report/browser.ts CANVAS_CHROMIUM_NETWORK_ARGS,
# verbatim (contract "Renderer requirements"): no DNS, and every connection
# (loopback included) through a dead proxy. The page cannot enforce this
# itself: parser-inserted resource hints are not requests and no CSP covers
# them.
CANVAS_CHROMIUM_NETWORK_ARGS = (
    "--host-resolver-rules=MAP * ~NOTFOUND",
    "--proxy-server=127.0.0.1:9",
    "--proxy-bypass-list=<-loopback>",
)
BASE_CHROMIUM_ARGS = (
    "--headless=new",
    "--remote-debugging-pipe",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--hide-scrollbars",
    "--mute-audio",
    "--use-mock-keychain",
    "--password-store=basic",
    "--force-color-profile=srgb",
)
# Never passed: Canvas code is hostile, so the OS sandbox and same-origin
# rules stay on, and there is no TCP debugging endpoint.
FORBIDDEN_CHROMIUM_ARGS = (
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--no-zygote",
    "--single-process",
    "--disable-web-security",
    "--allow-file-access-from-files",
    "--remote-debugging-port",
    "--remote-debugging-address",
)

# preview-bundle.ts constants (PREVIEW_VIEWPORT etc.); local_preview in the
# tool result overrides them.
DEFAULT_VIEWPORT = (1280, 800)
# The link-preview card (conductor storyboard/card/svg.ts CARD_WIDTH x
# CARD_HEIGHT); card.cover_render in the tool result overrides it.
COVER_VIEWPORT = (1200, 630)
DEFAULT_READY_MS = 10_000
DEFAULT_SETTLE_MS = 5_000
DEFAULT_MAX_BUNDLE_BYTES = 32 * 1024 * 1024
# One CDP message. Screenshots are the largest legitimate ones; a child
# frame's name can cross CDP at up to ~256 MB, so anything bigger than this is
# dropped and closes the Canvas.
MAX_CDP_MESSAGE_BYTES = 64 * 1024 * 1024
# A screenshot is one CDP message: at DPR 2 a tall, noisy Canvas already
# nears MAX_CDP_MESSAGE_BYTES, so --dpr is clamped.
MIN_DPR, MAX_DPR = 0.5, 2.0
DEFAULT_RUN_TIMEOUT_S = 540  # under the Bash tool's 10-minute ceiling
MAX_429_RETRIES = 4

STORYBOARD_ID_RE = re.compile(r"^sb_[0-9a-f]{24}$")
SCENE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# The only route the plugin key may call besides the MCP aggregator
# (conductor middleware/auth.ts STORYBOARD_PREVIEW_BUNDLE_PATH, GET only).
BUNDLE_PATH_RE = re.compile(
    r"^/api/orgs/([^/?#]+)/storyboards/(sb_[0-9a-f]{24})/scenes/([A-Za-z0-9_-]{1,64})"
    r"/preview\.html\?revision=(\d+)$"
)
MCP_URL_PATH_RE = re.compile(r"^/api/orgs/([^/?#]+)/mcp(?:/|$)")

UPGRADE_MESSAGE = (
    "This Cardinal returned no preview_bundle for any scene, so it predates local previews "
    "(maestro v1.97.10). Upgrade Cardinal, reconnect the MCP server (/mcp), and preview again. "
    "Publishing does not depend on the preview."
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def note(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    sys.stderr.flush()


def _write_private(path: Path, data: bytes) -> None:
    """Write `data` to `path` with mode 0600, replacing any existing file."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
    finally:
        os.close(fd)
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


def _mkdir_private(path: Path, own_leaf: bool = False) -> None:
    """Create `path` and any missing parents with mode 0700. Directories that
    already existed keep their mode (an explicit --out may be any directory),
    except the leaf when `own_leaf` says it is ours (the default PNG dir)."""
    missing = []
    p = path
    while not p.exists() and p != p.parent:
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            continue
        try:
            os.chmod(str(d), 0o700)  # mkdir's mode is masked by the umask
        except OSError:
            pass
    if own_leaf:
        try:
            os.chmod(str(path), 0o700)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Chromium discovery
# ---------------------------------------------------------------------------

MAC_APPS = (
    "Google Chrome.app/Contents/MacOS/Google Chrome",
    "Chromium.app/Contents/MacOS/Chromium",
    "Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "Brave Browser.app/Contents/MacOS/Brave Browser",
    "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
LINUX_COMMANDS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
LINUX_PATHS = ("/snap/bin/chromium",)


def _cache_candidates(home: Path, system: str) -> list:
    """Playwright / Puppeteer browser downloads, newest first."""
    if system == "Darwin":
        patterns = [
            (home / "Library/Caches/ms-playwright", "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"),
            (home / "Library/Caches/ms-playwright",
             "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"),
            (home / ".cache/puppeteer",
             "chrome/*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"),
        ]
    else:
        patterns = [
            (home / ".cache/ms-playwright", "chromium-*/chrome-linux*/chrome"),
            (home / ".cache/puppeteer", "chrome/*/chrome-linux*/chrome"),
        ]
    out = []
    for root, pattern in patterns:
        if root.is_dir():
            out.extend(sorted((str(p) for p in root.glob(pattern)), key=_version_sort_key, reverse=True))
    return out


def _version_sort_key(path: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", path))


def chromium_candidates(env: dict, system: str, home: Path, which=shutil.which, mac_roots=None) -> list:
    """Every place to look, in order: [(source, path)]. $CARDINAL_CHROMIUM is
    authoritative: when set, it is the only candidate."""
    if env.get("CARDINAL_CHROMIUM"):
        return [("$CARDINAL_CHROMIUM", env["CARDINAL_CHROMIUM"])]
    out = []
    for var in ("PUPPETEER_EXECUTABLE_PATH", "CHROME_PATH"):
        if env.get(var):
            out.append(("$" + var, env[var]))
    if system == "Darwin":
        for base in (mac_roots if mac_roots is not None else (Path("/Applications"), home / "Applications")):
            for app in MAC_APPS:
                out.append(("app", str(base / app)))
    elif system == "Linux":
        for cmd in LINUX_COMMANDS:
            found = which(cmd)
            out.append(("PATH", found or cmd))
        for p in LINUX_PATHS:
            out.append(("path", p))
    for p in _cache_candidates(home, system):
        out.append(("cache", p))
    return out


def chromium_version(path: str) -> str | None:
    try:
        res = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (res.stdout or res.stderr or "").strip()
    return text.splitlines()[0].strip() if text else None


def chrome_major(version: str | None) -> int | None:
    if not version:
        return None
    m = re.search(r"(\d+)\.\d+\.\d+", version)
    return int(m.group(1)) if m else None


def find_chromium(env: dict, system: str, home: Path, which=shutil.which, version_of=chromium_version,
                  mac_roots=None):
    """-> ({path, version, source} or None, [what was searched, with why not])."""
    searched = []
    for source, path in chromium_candidates(env, system, home, which, mac_roots):
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            searched.append(f"{path} ({source}: not found)")
            continue
        version = version_of(path)
        major = chrome_major(version)
        if major is not None and major < MIN_CHROME_MAJOR:
            searched.append(f"{path} ({version}: too old, need Chrome {MIN_CHROME_MAJOR}+)")
            continue
        return {"path": path, "version": version or "unknown", "source": source}, searched
    return None, searched


def launch_args(profile_dir: str) -> list:
    args = list(BASE_CHROMIUM_ARGS) + list(CANVAS_CHROMIUM_NETWORK_ARGS)
    args.append("--user-data-dir=" + profile_dir)
    args.append("about:blank")
    for a in args:
        if a.split("=", 1)[0] in FORBIDDEN_CHROMIUM_ARGS:
            raise AssertionError("forbidden Chromium flag: " + a)
    return args


# ---------------------------------------------------------------------------
# Connection + bundle fetch
# ---------------------------------------------------------------------------

class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


CONNECTION_ENV = "CARDINAL_CONNECTION"  # "env": the environment only, never settings.json
DISCONNECTED_MARKER = Path(".claude") / "cardinal-disconnected"


def _disconnected(home: Path) -> bool:
    try:
        return (home / DISCONNECTED_MARKER).is_file()
    except OSError:
        return False


def connect_info(home: Path, environ: dict, runtime: str = "claude") -> dict:
    """The MCP URL + key, resolved as the plugin's hooks do
    (hooks/_storyboard_discovery.connection; this script is stdlib-only and
    runs isolated, so the rule is repeated here):
      - CARDINAL_CONNECTION=env: CARDINAL_MCP_URL + CARDINAL_MCP_API_KEY from
        the environment only, never ~/.claude/settings.json (a session
        pointed at another Maestro, e.g. a local stack, on a machine whose
        settings hold the production key);
      - otherwise the /cardinal:connect values in ~/.claude/settings.json
        `env` first (Claude Code does not reliably export them to Bash),
        then the environment;
      - the /cardinal:disconnect marker means not connected, in both modes
        (except when settings.json still holds a connection)."""
    if runtime != "claude":
        # Trusted plugin-relative core, never the working directory. Isolated
        # Python (-I) prevents repository imports from intercepting credentials.
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "hooks"))
        from cardinal_core.evidence_promote import agent_paths_connection
        from cardinal_core.paths import AgentPaths
        return agent_paths_connection(AgentPaths(home=home / ("." + runtime)))(home, environ) or {}
    url = key = None
    if environ.get(CONNECTION_ENV) == "env":
        if _disconnected(home):
            return {}
        url, key = environ.get("CARDINAL_MCP_URL"), environ.get("CARDINAL_MCP_API_KEY")
    else:
        try:
            env = json.loads((home / ".claude" / "settings.json").read_text()).get("env", {})
            url, key = env.get("CARDINAL_MCP_URL"), env.get("CARDINAL_MCP_API_KEY")
        except (OSError, ValueError, AttributeError):
            pass
        if not (url and key):
            if _disconnected(home):
                return {}  # the running session's environment still holds the old key
            url, key = environ.get("CARDINAL_MCP_URL"), environ.get("CARDINAL_MCP_API_KEY")
    if not (url and key):
        return {}
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return {}
    m = MCP_URL_PATH_RE.match(parts.path)
    return {
        "origin": _origin(url),
        "org": urllib.parse.unquote(m.group(1)) if m else None,
        "key": key,
    }


def _origin(url: str) -> str | None:
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    default = 443 if parts.scheme == "https" else 80
    host = parts.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib re-sends custom headers on a redirect: the key must never follow one."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _opener():
    return urllib.request.build_opener(_NoRedirect())


def _error_body(err: urllib.error.HTTPError) -> dict:
    try:
        body = json.loads(err.read(64 * 1024) or b"{}")
        return body if isinstance(body, dict) else {}
    except (ValueError, OSError):
        return {}


def explain_status(status: int, body: dict) -> str:
    code = str(body.get("error") or "")
    msg = str(body.get("message") or "")
    if status == 400:
        return "maestro rejected the bundle request (no revision): the plugin and this Cardinal disagree on the preview contract; upgrade both"
    if status == 401:
        return "the Cardinal MCP key was rejected: reconnect with /cardinal:connect --rotate"
    if status == 403:
        if code == "org_scope_mismatch" or "org mismatch" in (code + " " + msg).lower():
            return ("this storyboard belongs to a different Cardinal org than the one Claude Code is connected to: "
                    "reconnect to that org (/cardinal:connect --rotate) or preview a storyboard in the connected org")
        if code == "insufficient_scope":
            return ("this Cardinal does not let the plugin key fetch preview bundles: upgrade Cardinal "
                    "(maestro v1.97.10 or newer)")
        return ("forbidden: authoring storyboards needs the Member role in this org (viewers cannot preview); "
                "ask an org owner for Member")
    if status == 404:
        return "storyboard or scene not found (deleted, or not in the connected org)"
    if status == 409 and code == "revision_mismatch":
        return "the storyboard changed since this preview: call storyboard__preview again and render its new result"
    if status == 409 and code == "dataset_not_materialized":
        return "a dataset this scene binds is not materialized yet: call storyboard__preview first, then render its result"
    if status == 409:
        return f"conflict ({code or 'unknown'}): call storyboard__preview again"
    if status == 413:
        return "bundle too large: " + (msg or "bind a reduce or a narrower query")
    if status == 422:
        return "scene not renderable: " + (msg or "the scene has errors; fix them and preview again")
    if status == 429:
        return "Cardinal is busy with other previews for this org: wait a minute and try again"
    if status == 501 or status == 503:
        return "this Cardinal cannot build preview bundles (" + (msg or code or f"HTTP {status}") + ")"
    return f"maestro answered HTTP {status}" + (f" ({code})" if code else "") + (f": {msg}" if msg else "")


def bundle_revision(ref: dict):
    """preview_bundle.revision when it is a plain int, else None. It names an
    output directory, so nothing else is accepted."""
    rev = ref.get("revision")
    return rev if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 0 else None


def fetch_bundle(conn: dict, ref: dict, max_bytes: int, opener=None, sleep=time.sleep,
                 deadline: float | None = None, clock=time.monotonic) -> bytes:
    """GET one scene's bundle page and check it against the tool result.
    Only ever sends the key to the connected maestro's bundle route. Never
    runs past `deadline` (a time.monotonic() value): socket timeouts, reads
    and 429 waits are all cut to what is left of it."""
    path = ref.get("path")
    if not isinstance(path, str) or not BUNDLE_PATH_RE.match(path):
        raise FetchError(f"preview_bundle.path is not a preview-bundle route: {str(path)[:200]!r}")
    route = BUNDLE_PATH_RE.match(path)
    org = urllib.parse.unquote(route.group(1))
    if bundle_revision(ref) != int(route.group(4)):
        raise FetchError("preview_bundle.revision does not match its path: call storyboard__preview again")
    if conn.get("org") and org != conn["org"]:
        raise FetchError(
            f"this preview is for org {org}, but Claude Code is connected to org {conn['org']}: "
            "reconnect to that org (/cardinal:connect --rotate) to render it",
            status=403, code="org_scope_mismatch")
    expect_bytes, expect_sha = ref.get("bytes"), ref.get("sha256")
    if not isinstance(expect_bytes, int) or not isinstance(expect_sha, str):
        raise FetchError("preview_bundle has no bytes/sha256: upgrade Cardinal")
    if expect_bytes > max_bytes:
        raise FetchError(f"bundle is {expect_bytes} bytes, over the {max_bytes}-byte cap")
    url = conn["origin"] + path
    opener = opener or _opener()
    attempt = 0

    def left(cap: float) -> float:
        if deadline is None:
            return cap
        remaining = deadline - clock()
        if remaining < 1:
            raise FetchError("not fetched: this run's time budget is spent; render the rest with --scene")
        return min(cap, remaining)

    while True:
        req = urllib.request.Request(url, method="GET", headers={
            "X-CardinalHQ-API-Key": conn["key"],
            "Accept": "text/html",
        })
        try:
            with opener.open(req, timeout=left(120)) as resp:
                # Chunked, so a server trickling bytes cannot outlast the run.
                data = b""
                while len(data) <= expect_bytes:
                    left(120)
                    chunk = resp.read(min(1 << 20, expect_bytes + 1 - len(data)))
                    if not chunk:
                        break
                    data += chunk
            break
        except urllib.error.HTTPError as err:
            body = _error_body(err)
            err.close()
            if err.code == 429 and attempt < MAX_429_RETRIES:
                attempt += 1
                try:
                    wait = int(err.headers.get("Retry-After") or 10)
                except (TypeError, ValueError):
                    wait = 10
                wait = max(1, min(wait, 30))
                if deadline is not None and clock() + wait > deadline - 1:
                    raise FetchError(explain_status(429, body), status=429)
                sleep(wait)
                continue
            if 300 <= err.code < 400:
                raise FetchError(f"maestro redirected the bundle request (HTTP {err.code}); not following it with your key",
                                 status=err.code)
            raise FetchError(explain_status(err.code, body), status=err.code, code=str(body.get("error") or "") or None)
        except (urllib.error.URLError, OSError) as err:
            reason = getattr(err, "reason", err)
            raise FetchError(f"could not reach {conn['origin']}: {reason}")
        except http.client.HTTPException as err:
            # A truncated chunked body (IncompleteRead), a bad status line:
            # not OSError, and still a fetch problem (exit 2), not a crash.
            raise FetchError(f"could not read the bundle from {conn['origin']}: {type(err).__name__}: "
                             f"{str(err)[:200]}; call storyboard__preview again")
    if len(data) != expect_bytes:
        raise FetchError(f"bundle size mismatch: preview said {expect_bytes} bytes, maestro sent "
                         f"{'more than that' if len(data) > expect_bytes else len(data)}; call storyboard__preview again")
    got = hashlib.sha256(data).hexdigest()
    if got != expect_sha:
        raise FetchError("bundle sha256 mismatch with the preview result; call storyboard__preview again")
    return data


# ---------------------------------------------------------------------------
# CDP over --remote-debugging-pipe (fd 3 in, fd 4 out, NUL-terminated JSON)
# ---------------------------------------------------------------------------

class CDPError(Exception):
    pass


class CDPClosed(CDPError):
    pass


class CanvasClosed(CDPError):
    pass


class PipeCDP:
    """Minimal CDP client. Writes to `write_fd`, reads `read_fd` on a thread.
    Event handlers run on the reader thread and may only post() (fire and
    forget), never send() (which waits)."""

    def __init__(self, write_fd: int, read_fd: int, max_message_bytes: int = MAX_CDP_MESSAGE_BYTES):
        self._w = write_fd
        self._r = read_fd
        self._max = max_message_bytes
        self._wlock = threading.Lock()
        self._cv = threading.Condition()
        self._next_id = 0
        self._responses: dict = {}
        self._waiting: set = set()
        self._callbacks: dict = {}
        self._handlers: list = []
        self._fatal: dict = {}
        self._sessions: set = set()
        self.closed: str | None = None
        self.oversized = 0
        self._thread = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._thread.start()

    # -- wire ---------------------------------------------------------------
    def _write(self, msg: dict) -> None:
        data = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\0"
        with self._wlock:
            view = memoryview(data)
            try:
                while view:
                    n = os.write(self._w, view)
                    view = view[n:]
            except OSError as err:
                raise CDPClosed(f"Chromium closed the DevTools pipe ({err})")

    def _read_loop(self) -> None:
        buf = bytearray()
        discarding = False
        try:
            while True:
                chunk = os.read(self._r, 1 << 16)
                if not chunk:
                    break
                start = 0
                while True:
                    nul = chunk.find(b"\0", start)
                    part = chunk[start:] if nul < 0 else chunk[start:nul]
                    if not discarding:
                        if len(buf) + len(part) > self._max:
                            buf = bytearray()
                            discarding = True
                        else:
                            buf += part
                    if nul < 0:
                        break
                    if discarding:
                        discarding = False
                        self._on_oversized()
                    else:
                        raw, buf = buf, bytearray()
                        self._dispatch(raw)
                    start = nul + 1
        except OSError:
            pass
        finally:
            with self._cv:
                self.closed = self.closed or "Chromium closed the DevTools pipe"
                self._cv.notify_all()

    def _on_oversized(self) -> None:
        self.oversized += 1
        reason = (f"a DevTools message exceeded {self._max // (1024 * 1024)} MiB (a Canvas pushed bulk data "
                  "through a frame event); the Canvas is closed")
        sessions = self._sessions_seen()
        with self._cv:
            for sess in sessions:
                self._fatal.setdefault(sess, reason)
            # Whatever command was waiting may have been answered by the
            # message we dropped: fail every waiter.
            for mid in list(self._waiting):
                self._responses.setdefault(mid, {"id": mid, "error": {"message": reason}})
            self._cv.notify_all()
        for sess in sessions:
            self._post_abort(sess, reason)

    def _sessions_seen(self) -> set:
        with self._cv:
            return set(self._sessions)

    def track_session(self, session: str) -> None:
        with self._cv:
            self._sessions.add(session)

    def forget_session(self, session: str) -> None:
        with self._cv:
            self._sessions.discard(session)

    def _dispatch(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        if "id" in msg:
            with self._cv:
                if msg["id"] in self._waiting:
                    self._responses[msg["id"]] = msg
                    self._cv.notify_all()
                callback = self._callbacks.pop(msg["id"], None)
            if callback is not None:
                try:
                    callback(msg)
                except Exception:  # a callback bug must not kill the reader
                    pass
            return
        for handler in list(self._handlers):
            try:
                handler(msg)
            except Exception:  # a handler bug must not kill the reader
                pass

    # -- API ----------------------------------------------------------------
    def on(self, handler) -> None:
        self._handlers.append(handler)

    def off(self, handler) -> None:
        try:
            self._handlers.remove(handler)
        except ValueError:
            pass

    def _new_id(self) -> int:
        with self._cv:
            self._next_id += 1
            return self._next_id

    def post(self, method: str, params: dict | None = None, session: str | None = None,
             on_reply=None) -> None:
        """Send without waiting. `on_reply(msg)`, if given, runs on the reader
        thread with the raw response (check msg.get("error")); like event
        handlers it may only post(), never send()."""
        mid = self._new_id()
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        if on_reply is not None:
            with self._cv:
                self._callbacks[mid] = on_reply
        try:
            self._write(msg)
        except CDPClosed:
            with self._cv:
                self._callbacks.pop(mid, None)

    def send(self, method: str, params: dict | None = None, session: str | None = None,
             timeout: float = 30.0) -> dict:
        mid = self._new_id()
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        with self._cv:
            self._waiting.add(mid)
        try:
            self._write(msg)
            deadline = time.monotonic() + timeout
            with self._cv:
                while True:
                    if mid in self._responses:
                        resp = self._responses.pop(mid)
                        break
                    if session and session in self._fatal:
                        raise CanvasClosed(self._fatal[session])
                    if self.closed:
                        raise CDPClosed(self.closed)
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError(f"{method} did not answer within {timeout:.0f}s")
                    self._cv.wait(min(left, 0.5))
        finally:
            with self._cv:
                self._waiting.discard(mid)
                self._responses.pop(mid, None)
        if "error" in resp:
            err = resp["error"] or {}
            if session and session in self._fatal:
                raise CanvasClosed(self._fatal[session])
            raise CDPError(f"{method}: {err.get('message', err)}")
        return resp.get("result") or {}

    def trip(self, session: str, reason: str) -> None:
        """Close the Canvas in `session` and stop waiting on it."""
        with self._cv:
            if session in self._fatal:
                return
            self._fatal[session] = reason
            self._cv.notify_all()
        self._post_abort(session, reason)

    def _post_abort(self, session: str, reason: str) -> None:
        # No context id: runs in the host page's main world, which the
        # sandboxed, opaque-origin frame cannot reach.
        self.post("Runtime.evaluate", {
            "expression": "window.cardinalPreview && window.cardinalPreview.abort(%s)" % json.dumps(reason),
        }, session)

    def fatal(self, session: str) -> str | None:
        with self._cv:
            return self._fatal.get(session)


# ---------------------------------------------------------------------------
# Chromium process
# ---------------------------------------------------------------------------

class LaunchError(Exception):
    pass


class Browser:
    def __init__(self, binary: str, workdir: Path):
        self.binary = binary
        self.profile = workdir / "profile"
        _mkdir_private(self.profile)
        self.stderr_path = workdir / "chromium.log"
        self.proc = None
        self.cdp = None

    def start(self, timeout: float = 30.0) -> PipeCDP:
        if os.name != "posix":
            raise LaunchError("local preview is not supported on Windows yet")
        cmd_r, cmd_w = os.pipe()   # we write commands, Chromium reads fd 3
        res_r, res_w = os.pipe()   # Chromium writes fd 4, we read
        # Keep both child ends clear of 3/4 so the dup2s below cannot collide.
        import fcntl
        hi_cmd_r = fcntl.fcntl(cmd_r, fcntl.F_DUPFD, 10)
        hi_res_w = fcntl.fcntl(res_w, fcntl.F_DUPFD, 10)
        os.close(cmd_r)
        os.close(res_w)

        def child_fds():
            os.dup2(hi_cmd_r, 3)
            os.dup2(hi_res_w, 4)

        env = dict(os.environ)
        env["TZ"] = "UTC"
        err = open(str(self.stderr_path), "wb")
        try:
            self.proc = subprocess.Popen(
                [self.binary] + launch_args(str(self.profile)),
                stdin=subprocess.DEVNULL, stdout=err, stderr=err,
                env=env, close_fds=False, preexec_fn=child_fds, start_new_session=True,
            )
        except OSError as e:
            raise LaunchError(f"could not start {self.binary}: {e}")
        finally:
            err.close()
            os.close(hi_cmd_r)
            os.close(hi_res_w)
        self.cdp = PipeCDP(cmd_w, res_r)
        try:
            self.cdp.send("Browser.getVersion", timeout=timeout)
        except (CDPError, TimeoutError) as e:
            tail = self.stderr_tail()
            self.kill()
            if re.search(r"sandbox|namespace|setuid", tail, re.I):
                raise LaunchError(
                    "Chromium could not start with its OS sandbox on (on Linux this needs unprivileged user "
                    "namespaces, which this machine or container does not allow). The preview renderer never "
                    "disables the sandbox, so local preview is unavailable here. Chromium said: " + tail[-600:])
            raise LaunchError(f"Chromium did not start ({e}). Chromium said: {tail[-600:] or '(nothing)'}")
        return self.cdp

    def stderr_tail(self) -> str:
        try:
            with open(str(self.stderr_path), "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 4096))
                return f.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""

    def close(self) -> None:
        if self.cdp and not self.cdp.closed:
            try:
                self.cdp.send("Browser.close", timeout=5)
            except (CDPError, TimeoutError):
                pass
        if self.proc:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self.kill()

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                try:
                    self.proc.kill()
                except OSError:
                    pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        elif self.proc:
            # The browser exited; its helpers share the process group.
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass


# ---------------------------------------------------------------------------
# Driving one bundle page (render.ts renderBundlePage)
# ---------------------------------------------------------------------------

REVEAL_JS = """(async () => {
  const api = window.cardinalPreview;
  if (!api) return {ok: false, error: "the page has no cardinalPreview host"};
  try { await api.reveal(%d); return {ok: true, steps: api.steps}; }
  catch (e) { return {ok: false, error: (e && e.message) ? e.message : String(e)}; }
})()"""

SNAP_JS = """(() => {
  const api = window.cardinalPreview;
  const el = document.getElementById("cv");
  const r = el ? el.getBoundingClientRect() : null;
  const rep = api ? api.report() : null;
  return {
    rect: r ? {x: r.left + window.scrollX, y: r.top + window.scrollY, width: r.width, height: r.height} : null,
    state: rep ? rep.state : null, step: rep ? rep.step : null, steps: rep ? rep.steps : null,
    height: rep ? rep.height : null,
    frameErrors: rep ? rep.frameErrors : [], protocolErrors: rep ? rep.protocolErrors : []
  };
})()"""

REPORT_JS = "window.cardinalPreview ? window.cardinalPreview.report() : null"

# The Canvas iframe (sandbox="allow-scripts", srcdoc) is an out-of-process
# iframe in current Chrome (IsolateSandboxedIframes): its own renderer, its
# own DevTools target. The page session's Fetch interception and Page events
# do not cover it, so every frame target is auto-attached and gets the same
# request filter and frame locks on its own session. Chrome does not hold a
# srcdoc OOPIF for the debugger (waitingForDebugger is false), so the frame's
# initial parse can run before its session is set up: Page.getFrameTree
# catches a child frame or navigation made in that window, and the network
# flags plus the frame's hash CSP stay the barrier for its requests.
# (--disable-features=IsolateSandboxedIframes would close that window but
# would put hostile Canvas code in the file:// host page's process.)
AUTO_ATTACH = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
REDUCED_MOTION = {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]}
FETCH_ALL = {"patterns": [{"urlPattern": "*"}]}
CANVAS_FRAME_URL = "about:srcdoc"
MSG_CHILD_FRAME = ("the Canvas created a child frame (<iframe>/<frame>/<object>); a Canvas cannot "
                   "embed frames — the Canvas is closed")
MSG_WORKER = "the Canvas started a worker; a Canvas cannot run workers — the Canvas is closed"
MSG_HASH_NAV = ("a frame navigated within its document (location.hash / history); a Canvas cannot "
                "navigate — the Canvas is closed")
MSG_NAVIGATED = "a frame navigated; a Canvas cannot navigate — the Canvas is closed"


def _clip_list(xs, n=20, width=500) -> list:
    out = []
    for x in (xs or [])[:n]:
        s = str(x)
        out.append(s if len(s) <= width else s[:width] + "…")
    if xs and len(xs) > n:
        out.append(f"… {len(xs) - n} more")
    return out


def png_name(scene_id: str, step: int, theme: str) -> str:
    return f"{scene_id}-{step}" + ("-dark" if theme == "dark" else "") + ".png"


def cover_png_name(scene_id: str, theme: str) -> str:
    # Never matches png_name's <scene>-<N>.png: the hero upload picks the
    # cover and the last step apart by name.
    return f"{scene_id}-cover" + ("-dark" if theme == "dark" else "") + ".png"


def clear_scene_pngs(out_dir: Path, scene_id: str, theme: str) -> None:
    pat = re.compile(r"^" + re.escape(scene_id) + r"-\d+" + ("-dark" if theme == "dark" else "") + r"\.png$")
    if out_dir.is_dir():
        for p in out_dir.iterdir():
            if pat.match(p.name):
                try:
                    p.unlink()
                except OSError:
                    pass


def render_page(cdp: PipeCDP, page_path: Path, scene_id: str, out_dir: Path, opts: dict) -> dict:
    """Load one bundle page and screenshot every reveal step.
    -> {ok, steps, pngs, records, error, frame_errors, protocol_errors, blocked}"""
    file_url = page_path.resolve().as_uri()
    fragment = "#theme=" + opts["theme"]
    width, height = opts["viewport"]
    ready_s, settle_s = opts["ready_ms"] / 1000.0, opts["settle_ms"] / 1000.0
    # children: DevTools sessions of the Canvas frame targets (OOPIFs) and
    # anything they spawn, auto-attached below. Only the reader thread
    # touches it.
    state = {"blocked": 0, "main": None, "loaded": threading.Event(), "children": {}}
    result = {"ok": False, "steps": None, "pngs": [], "records": [], "error": None,
              "frame_errors": [], "protocol_errors": [], "blocked": 0}
    ctx = target = sess = None

    def allowed(url: str) -> bool:
        return url.startswith(("data:", "blob:", "about:")) or url.split("#", 1)[0] == file_url

    def trip(reason: str) -> None:
        # Always the page session: abort() runs in the host page's main
        # world, never in the (hostile) Canvas frame's.
        cdp.trip(sess, reason)

    def filter_request(session: str, p: dict) -> None:
        url = str((p.get("request") or {}).get("url") or "")
        if allowed(url):
            cdp.post("Fetch.continueRequest", {"requestId": p.get("requestId")}, session)
        else:
            state["blocked"] += 1
            cdp.post("Fetch.failRequest", {"requestId": p.get("requestId"), "errorReason": "BlockedByClient"},
                     session)

    def lock_failed(what: str):
        def on_reply(reply: dict) -> None:
            if reply.get("error"):
                err = (reply.get("error") or {}).get("message") or reply.get("error")
                trip(f"the preview could not put its {what} on the Canvas frame ({err}); the Canvas is closed")
        return on_reply

    def check_frame_tree(reply: dict) -> None:
        # What the Canvas did before its session was attached.
        if reply.get("error"):
            lock_failed("frame locks")(reply)
            return
        tree = (reply.get("result") or {}).get("frameTree") or {}
        frame = tree.get("frame") or {}
        if tree.get("childFrames"):
            trip(MSG_CHILD_FRAME)
        elif frame.get("urlFragment"):  # Chrome reports "#…" apart from url
            trip(MSG_HASH_NAV)
        elif frame.get("url") != CANVAS_FRAME_URL:
            trip(MSG_NAVIGATED)

    def adopt(child: str, info: dict, resume: bool) -> None:
        kind = info.get("type")
        state["children"][child] = kind
        if kind == "iframe":
            # Posted in order on one session, so the filter and the locks are
            # in place before the target resumes (when it waited at all).
            cdp.post("Fetch.enable", FETCH_ALL, child, on_reply=lock_failed("request filter"))
            cdp.post("Page.enable", None, child, on_reply=lock_failed("frame locks"))
            cdp.post("Target.setAutoAttach", AUTO_ATTACH, child, on_reply=lock_failed("frame locks"))
            cdp.post("Emulation.setEmulatedMedia", REDUCED_MOTION, child)
            cdp.post("Page.getFrameTree", None, child, on_reply=check_frame_tree)
        else:
            # A worker: its requests are filtered where Chrome allows it.
            cdp.post("Fetch.enable", FETCH_ALL, child)
        if resume:
            cdp.post("Runtime.runIfWaitingForDebugger", None, child)

    def check_navigation(p: dict, in_canvas: bool) -> None:
        frame = p.get("frame") or {}
        url = frame.get("url")
        if in_canvas:
            # The Canvas frame's own session attaches after its first commit
            # (about:srcdoc), so any other commit there is the Canvas
            # navigating itself, about:blank included.
            if url not in (CANVAS_FRAME_URL, "", None):
                trip(MSG_NAVIGATED)
        elif frame.get("parentId") and url not in ("about:srcdoc", "about:blank", "", None):
            trip(MSG_NAVIGATED)

    def on_event(msg: dict) -> None:
        sid = msg.get("sessionId")
        if sid is None:
            return
        in_canvas = sid in state["children"]
        if sid != sess and not in_canvas:
            return
        method = msg.get("method")
        p = msg.get("params") or {}
        if method == "Fetch.requestPaused":
            filter_request(sid, p)
        elif method == "Target.attachedToTarget":
            child = p.get("sessionId")
            if not child:
                return
            if in_canvas:
                # Something the Canvas itself spawned in another process
                # (a grandchild frame): filter it, keep it paused, close.
                info = p.get("targetInfo") or {}
                adopt(child, info, resume=False)
                trip(MSG_CHILD_FRAME if info.get("type") == "iframe" else MSG_WORKER)
            else:
                adopt(child, p.get("targetInfo") or {}, resume=True)
        elif method == "Target.detachedFromTarget":
            state["children"].pop(p.get("sessionId"), None)
        elif method == "Page.frameAttached":
            parent = p.get("parentFrameId")
            if in_canvas or (parent and state["main"] and parent != state["main"]):
                trip(MSG_CHILD_FRAME)
        elif method == "Page.navigatedWithinDocument":
            if in_canvas or (p.get("frameId") and p.get("frameId") != state["main"]):
                trip(MSG_HASH_NAV)
        elif method == "Page.frameNavigated":
            check_navigation(p, in_canvas)
        elif method in ("Page.domContentEventFired", "Page.loadEventFired") and not in_canvas:
            state["loaded"].set()

    def evaluate(expr: str, timeout: float):
        r = cdp.send("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True},
                     sess, timeout=timeout)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise CDPError(str((d.get("exception") or {}).get("description") or d.get("text") or "evaluate failed"))
        return (r.get("result") or {}).get("value")

    cdp.on(on_event)
    step = None
    try:
        ctx = cdp.send("Target.createBrowserContext", {"disposeOnDetach": True})["browserContextId"]
        target = cdp.send("Target.createTarget", {"url": "about:blank", "browserContextId": ctx})["targetId"]
        sess = cdp.send("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        cdp.track_session(sess)
        cdp.send("Page.enable", session=sess)
        tree = cdp.send("Page.getFrameTree", session=sess)
        state["main"] = ((tree.get("frameTree") or {}).get("frame") or {}).get("id") or target
        cdp.send("Emulation.setDeviceMetricsOverride",
                 {"width": width, "height": height, "deviceScaleFactor": opts["dpr"], "mobile": False}, sess)
        cdp.send("Emulation.setTimezoneOverride", {"timezoneId": "UTC"}, sess)
        cdp.send("Emulation.setEmulatedMedia", REDUCED_MOTION, sess)
        cdp.send("Target.setAutoAttach", AUTO_ATTACH, sess)
        cdp.send("Fetch.enable", FETCH_ALL, sess)
        nav = cdp.send("Page.navigate", {"url": file_url + fragment}, sess, timeout=ready_s + 10)
        if nav.get("errorText"):
            raise CDPError("the page did not load: " + str(nav["errorText"]))
        deadline = time.monotonic() + ready_s + 10
        while not state["loaded"].wait(0.25):
            if cdp.fatal(sess):
                raise CanvasClosed(cdp.fatal(sess))
            if time.monotonic() > deadline:
                raise TimeoutError("the page did not finish loading")
        step = 0
        first = evaluate(REVEAL_JS % 0, ready_s + settle_s + 5)
        if not isinstance(first, dict) or not first.get("ok"):
            raise CDPError((first or {}).get("error") if isinstance(first, dict) else "reveal(0) failed")
        steps = first.get("steps")
        result["steps"] = steps if isinstance(steps, int) else None
        cover = opts.get("cover")
        last = (result["steps"] or 0) - 1
        for step in range(result["steps"] or 0):
            if step > 0:
                r = evaluate(REVEAL_JS % step, settle_s + 5)
                if not isinstance(r, dict) or not r.get("ok"):
                    raise CDPError((r or {}).get("error") if isinstance(r, dict) else f"reveal({step}) failed")
            if cover and step != last:
                # The cover is the scene's final picture: step through the
                # reveals (each builds on the last) and shoot only the end.
                continue
            snap = evaluate(SNAP_JS, 10) or {}
            rect = snap.get("rect")
            if not rect or rect.get("width", 0) <= 0 or rect.get("height", 0) <= 0:
                raise CDPError("the Canvas frame has no layout box")
            # A cover is exactly the card's size (viewport = cover_render,
            # DPR 1) from the Canvas frame's corner; a step is the whole frame.
            clip = ({"x": rect["x"], "y": rect["y"], "width": width, "height": height, "scale": 1} if cover else
                    {"x": rect["x"], "y": rect["y"], "width": rect["width"], "height": rect["height"], "scale": 1})
            shot = cdp.send("Page.captureScreenshot", {
                "format": "png", "clip": clip, "captureBeyondViewport": True,
            }, sess, timeout=60)
            png = base64.b64decode(shot.get("data") or "")
            if not png.startswith(b"\x89PNG"):
                raise CDPError("Chromium returned no PNG")
            dest = out_dir / (cover_png_name(scene_id, opts["theme"]) if cover else png_name(scene_id, step, opts["theme"]))
            _write_private(dest, png)
            result["pngs"].append(str(dest))
            result["records"].append({
                "scene_id": scene_id, "step": "cover" if cover else step, "steps": result["steps"], "png": str(dest),
                "state": snap.get("state"), "height": snap.get("height"), "error": None,
                "frame_errors": _clip_list(snap.get("frameErrors")),
                "protocol_errors": _clip_list(snap.get("protocolErrors")),
            })
        result["ok"] = True
    except Exception as e:  # one scene's failure must not end the run
        result["ok"] = False
        msg = str(e) if isinstance(e, (CDPError, TimeoutError)) else f"{type(e).__name__}: {e}"
        result["error"] = msg + (f" (at step {step})" if step is not None and not isinstance(e, CanvasClosed) else "")
    finally:
        closed_by = cdp.fatal(sess) if sess else None
        report = None
        if sess and closed_by is None and not cdp.closed:
            try:
                report = evaluate(REPORT_JS, 5)
            except (CDPError, TimeoutError):
                report = None
        if isinstance(report, dict):
            result["frame_errors"] = _clip_list(report.get("frameErrors"))
            result["protocol_errors"] = _clip_list(report.get("protocolErrors"))
            if isinstance(report.get("steps"), int):
                result["steps"] = report["steps"]
            result["state"] = report.get("state")
            result["height"] = report.get("height")
            if report.get("error") and not result["error"]:
                result["error"] = str(report["error"])[:1000]
        if closed_by and closed_by not in result["protocol_errors"]:
            result["protocol_errors"].append(closed_by)
        if result["frame_errors"]:
            result["ok"] = False
        result["blocked"] = state["blocked"]
        cdp.off(on_event)
        if sess:
            cdp.forget_session(sess)
        if target and not cdp.closed:
            try:
                cdp.send("Target.closeTarget", {"targetId": target}, timeout=10)
            except (CDPError, TimeoutError):
                pass
        if ctx and not cdp.closed:
            try:
                cdp.send("Target.disposeBrowserContext", {"browserContextId": ctx}, timeout=10)
            except (CDPError, TimeoutError):
                pass
    return result


def render_static(cdp: PipeCDP, page_path: Path, out_png: Path, viewport: tuple, dpr: float,
                  load_s: float = 15.0) -> dict:
    """Screenshot one local static page (the link-preview mock) at `viewport`.
    Scripts are off (Emulation.setScriptExecutionDisabled before the page
    loads), every request but the page itself and data:/about: URLs fails,
    and Chromium runs with the same network lock as a Canvas render.
    -> {ok, png, error, blocked}"""
    file_url = page_path.resolve().as_uri()
    width, height = viewport
    state = {"blocked": 0, "dcl": None, "load": threading.Event()}
    result = {"ok": False, "png": None, "error": None, "blocked": 0}
    ctx = target = sess = None

    def on_event(msg: dict) -> None:
        if sess is None or msg.get("sessionId") != sess:
            return
        method = msg.get("method")
        p = msg.get("params") or {}
        if method == "Fetch.requestPaused":
            url = str((p.get("request") or {}).get("url") or "")
            if url.startswith(("data:", "about:")) or url.split("#", 1)[0] == file_url:
                cdp.post("Fetch.continueRequest", {"requestId": p.get("requestId")}, sess)
            else:
                state["blocked"] += 1
                cdp.post("Fetch.failRequest", {"requestId": p.get("requestId"), "errorReason": "BlockedByClient"},
                         sess)
        elif method == "Page.domContentEventFired":
            state["dcl"] = state["dcl"] or time.monotonic()
        elif method == "Page.loadEventFired":
            state["load"].set()

    cdp.on(on_event)
    try:
        ctx = cdp.send("Target.createBrowserContext", {"disposeOnDetach": True})["browserContextId"]
        target = cdp.send("Target.createTarget", {"url": "about:blank", "browserContextId": ctx})["targetId"]
        sess = cdp.send("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        cdp.track_session(sess)
        cdp.send("Page.enable", session=sess)
        cdp.send("Emulation.setScriptExecutionDisabled", {"value": True}, sess)
        cdp.send("Emulation.setDeviceMetricsOverride",
                 {"width": width, "height": height, "deviceScaleFactor": dpr, "mobile": False}, sess)
        cdp.send("Emulation.setEmulatedMedia", REDUCED_MOTION, sess)
        cdp.send("Fetch.enable", FETCH_ALL, sess)
        nav = cdp.send("Page.navigate", {"url": file_url}, sess, timeout=load_s + 5)
        if nav.get("errorText"):
            raise CDPError("the page did not load: " + str(nav["errorText"]))
        deadline = time.monotonic() + load_s
        # The load event (images decoded); DOMContentLoaded plus a grace
        # period when load never comes.
        while not state["load"].wait(0.1):
            dcl = state["dcl"]
            if dcl is not None and time.monotonic() - dcl > 2.0:
                break
            if cdp.closed or time.monotonic() > deadline:
                raise TimeoutError("the page did not finish loading")
        shot = cdp.send("Page.captureScreenshot", {
            "format": "png", "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": 1},
        }, sess, timeout=60)
        png = base64.b64decode(shot.get("data") or "")
        if not png.startswith(b"\x89PNG"):
            raise CDPError("Chromium returned no PNG")
        _mkdir_private(out_png.parent)
        _write_private(out_png, png)
        result.update(ok=True, png=str(out_png))
    except Exception as e:
        result["error"] = str(e) if isinstance(e, (CDPError, TimeoutError)) else f"{type(e).__name__}: {e}"
    finally:
        result["blocked"] = state["blocked"]
        cdp.off(on_event)
        if sess:
            cdp.forget_session(sess)
        if target and not cdp.closed:
            try:
                cdp.send("Target.closeTarget", {"targetId": target}, timeout=10)
            except (CDPError, TimeoutError):
                pass
        if ctx and not cdp.closed:
            try:
                cdp.send("Target.disposeBrowserContext", {"browserContextId": ctx}, timeout=10)
            except (CDPError, TimeoutError):
                pass
    return result


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

def parse_preview_result(text: str) -> dict:
    """The storyboard__preview result, from its JSON or an MCP wrapper."""
    try:
        obj = json.loads(text)
    except ValueError as e:
        raise ValueError(f"input is not JSON ({e})")
    return _unwrap(obj)


def _unwrap(obj, depth: int = 0) -> dict:
    if depth <= 3 and isinstance(obj, list):
        # A bare MCP content-block list (how Claude Code saves a large result).
        return _unwrap({"content": obj}, depth + 1)
    if depth > 3 or not isinstance(obj, dict):
        raise ValueError("input is not a storyboard__preview result (no scenes)")
    if isinstance(obj.get("scenes"), list):
        return obj
    if isinstance(obj.get("structuredContent"), dict):
        return _unwrap(obj["structuredContent"], depth + 1)
    if isinstance(obj.get("content"), str):
        # {content: "<result text>"}: how Claude Code's tool_use_result keeps an MCP result.
        try:
            return _unwrap(json.loads(obj["content"]), depth + 1)
        except ValueError:
            raise ValueError("input is not a storyboard__preview result (no scenes)") from None
    for block in obj.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                return _unwrap(json.loads(block.get("text") or ""), depth + 1)
            except ValueError:
                continue
    raise ValueError("input is not a storyboard__preview result (no scenes)")


def plan_scenes(result: dict, only: list | None) -> tuple:
    """-> (to_fetch [(scene_id, ref)], problems [(scene_id, reason)], upgrade_needed)"""
    scenes = [s for s in result.get("scenes") or [] if isinstance(s, dict)]
    if scenes and not any("preview_bundle" in s for s in scenes):
        return [], [], True
    wanted = set(only or [])
    fetch, problems = [], []
    for s in scenes:
        sid = str(s.get("id") or "")
        if wanted and sid not in wanted:
            continue
        ref = s.get("preview_bundle")
        if not SCENE_ID_RE.match(sid):
            problems.append((sid or "?", "scene id is not a valid scene id"))
        elif not isinstance(ref, dict):
            problems.append((sid, "no preview_bundle for this scene"))
        elif "unavailable" in ref:
            problems.append((sid, "unavailable: " + str(ref.get("unavailable"))))
        else:
            fetch.append((sid, ref))
    for sid in sorted(wanted - {str(s.get("id")) for s in scenes}):
        problems.append((sid, "no such scene in this preview result"))
    return fetch, problems, False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _problem_record(scene_id: str, reason: str) -> dict:
    return {"scene_id": scene_id, "step": None, "steps": None, "png": None, "state": None, "height": None,
            "error": reason, "frame_errors": [], "protocol_errors": []}


def no_chromium_message(searched: list, reason: str | None = None) -> str:
    head = reason or "No usable local Chromium or Google Chrome (version 112+) was found, so the scenes were not rendered."
    return (head + " Local preview is authoring feedback, not a publish requirement: tell the user, keep authoring, "
            "and judge the scenes from their statements and the preview warnings. To enable it, install Google Chrome "
            "or set CARDINAL_CHROMIUM to a Chrome/Chromium binary. Searched: " + "; ".join(searched[:40]))


VIEWPORT_ARG_RE = re.compile(r"^(\d{2,4})x(\d{2,4})$")


def static_main(args, home: Path, deadline: float, finish, summary: dict) -> int:
    """--static-html FILE --png OUT: one local page to one PNG, in the same
    sandboxed, network-locked Chromium as a Canvas render. A local file only:
    nothing is fetched from Cardinal."""
    page = Path(args.static_html)
    if not page.is_file():
        return finish(EXIT_FETCH, f"no such file: {args.static_html}")
    if not args.png:
        return finish(EXIT_FETCH, "--static-html needs --png <out.png>")
    m = VIEWPORT_ARG_RE.match(args.viewport or "")
    if not m:
        return finish(EXIT_FETCH, "--viewport takes WxH, e.g. 560x640")
    viewport = (max(64, min(int(m.group(1)), 4096)), max(64, min(int(m.group(2)), 4096)))
    if os.name != "posix" or sys.platform.startswith("win"):
        return finish(EXIT_NO_CHROMIUM, no_chromium_message([], "Local preview is not supported on Windows yet."))
    env = dict(os.environ)
    if args.chromium:
        env["CARDINAL_CHROMIUM"] = args.chromium
    found, searched = find_chromium(env, "Darwin" if sys.platform == "darwin" else "Linux", home)
    if not found:
        return finish(EXIT_NO_CHROMIUM, no_chromium_message(searched))
    summary["chromium"] = found
    workdir = Path(tempfile.mkdtemp(prefix="cardinal-preview-"))
    os.chmod(str(workdir), 0o700)
    browser = Browser(found["path"], workdir)
    watchdog = threading.Timer(max(5.0, deadline - time.monotonic()), browser.kill)
    watchdog.daemon = True
    try:
        try:
            cdp = browser.start()
        except LaunchError as e:
            return finish(EXIT_NO_CHROMIUM, no_chromium_message([found["path"]], str(e)))
        watchdog.start()
        r = render_static(cdp, page, Path(args.png), viewport, max(MIN_DPR, min(args.dpr, MAX_DPR)))
        emit({"static_html": str(page), "png": r["png"], "error": r["error"]})
        if r["blocked"]:
            note(f"blocked {r['blocked']} request(s) from the page")
        if r["png"]:
            summary["rendered"] = 1
            summary["pngs"].append(r["png"])
            summary["out_dir"] = str(Path(r["png"]).parent)
        return finish(EXIT_OK, None if r["ok"] else "the page was not rendered: " + str(r["error"]))
    finally:
        watchdog.cancel()
        browser.close()
        shutil.rmtree(str(workdir), ignore_errors=True)


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Render Cardinal storyboard preview bundles to PNGs with local, sandboxed, offline Chromium.",
        epilog="Exit codes: 0 ran (see per-scene errors), 2 input/connection/fetch problem, "
               "3 no usable local Chromium (skip preview, keep authoring).")
    ap.add_argument("--from-json", metavar="FILE", help="storyboard__preview result JSON ('-' or omitted: stdin)")
    ap.add_argument("--html", metavar="FILE", action="append", help="render a local bundle page instead (repeatable)")
    ap.add_argument("--scene", action="append", metavar="ID", help="only this scene (repeatable)")
    ap.add_argument("--theme", choices=("light", "dark"), default="light")
    ap.add_argument("--dpr", type=float, default=1.0,
                    help=f"device pixel ratio, {MIN_DPR:g}–{MAX_DPR:g} (default 1, fine for critique)")
    ap.add_argument("--out", metavar="DIR", help="PNG directory (default ~/.claude/cardinal/storyboards/<id>/r<rev>)")
    ap.add_argument("--chromium", metavar="PATH", help="Chrome/Chromium binary (else discovered)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_RUN_TIMEOUT_S, help="whole-run wall clock, seconds")
    ap.add_argument("--keep", action="store_true", help="keep the temp dir (bundle pages + profile)")
    ap.add_argument("--cover", metavar="ID",
                    help="render scene ID's last reveal step as the link-preview cover: viewport card.cover_render "
                         "(1200x630), DPR 1, <scene>-cover.png")
    ap.add_argument("--static-html", metavar="FILE",
                    help="screenshot a local static page instead (scripts off, no network): needs --png")
    ap.add_argument("--png", metavar="OUT", help="--static-html: the PNG to write")
    ap.add_argument("--viewport", metavar="WxH", default="560x640", help="--static-html: viewport (default 560x640)")
    ap.add_argument("--runtime", choices=("claude", "codex", "cursor", "gemini"), required=True, help="Use this agent's Cardinal connection")
    args = ap.parse_args(argv)

    home = Path(os.environ.get("HOME") or str(Path.home()))
    deadline = time.monotonic() + max(10, args.timeout)
    summary: dict = {"rendered": 0, "pngs": [], "scenes": {}, "chromium": None, "out_dir": None}

    def finish(code: int, message: str | None = None) -> int:
        if message:
            summary["message"] = message
        summary["exit"] = code
        emit({"summary": summary})
        return code

    if args.static_html:
        return static_main(args, home, deadline, finish, summary)
    if args.cover is not None:
        if not SCENE_ID_RE.match(args.cover):
            return finish(EXIT_FETCH, "--cover takes a scene id")
        args.scene = [args.cover]

    # -- input ----------------------------------------------------------------
    pages: list = []   # (scene_id, local path or None, ref or None)
    cover_render: dict = {}
    problems: list = []
    sb_id, revision, max_bytes = "local", None, DEFAULT_MAX_BUNDLE_BYTES
    viewport, ready_ms, settle_ms = DEFAULT_VIEWPORT, DEFAULT_READY_MS, DEFAULT_SETTLE_MS
    if args.html:
        for f in args.html:
            p = Path(f)
            if not p.is_file():
                return finish(EXIT_FETCH, f"no such file: {f}")
            sid = re.sub(r"[^A-Za-z0-9_-]", "-", p.stem.replace(".preview", ""))[:64] or "scene"
            pages.append((sid, p, None))
    else:
        try:
            if args.from_json and args.from_json != "-":
                text = Path(args.from_json).read_text(encoding="utf-8")
            else:
                if sys.stdin.isatty():
                    ap.print_usage(sys.stderr)
                    return finish(EXIT_FETCH, "pass --from-json <file> (the storyboard__preview result) or pipe it on stdin")
                text = sys.stdin.read()
            result = parse_preview_result(text)
        except (OSError, ValueError) as e:
            return finish(EXIT_FETCH, str(e))
        sb = str(result.get("storyboard_id") or "")
        sb_id = sb if STORYBOARD_ID_RE.match(sb) else "unknown"
        summary["storyboard_id"] = sb_id
        if isinstance(result.get("view_url"), str):
            summary["view_url"] = result["view_url"]
        lp = result.get("local_preview") if isinstance(result.get("local_preview"), dict) else {}
        vp = lp.get("viewport") if isinstance(lp.get("viewport"), dict) else {}
        if isinstance(vp.get("width"), int) and isinstance(vp.get("height"), int):
            viewport = (max(320, min(vp["width"], 4096)), max(200, min(vp["height"], 4096)))
        card = result.get("card") if isinstance(result.get("card"), dict) else {}
        cover_render = card.get("cover_render") if isinstance(card.get("cover_render"), dict) else {}
        if isinstance(lp.get("ready_timeout_ms"), int):
            ready_ms = max(1000, min(lp["ready_timeout_ms"], 120_000))
        if isinstance(lp.get("settle_timeout_ms"), int):
            settle_ms = max(1000, min(lp["settle_timeout_ms"], 120_000))
        if isinstance(lp.get("max_bundle_bytes"), int):
            max_bytes = max(1, min(lp["max_bundle_bytes"], 256 * 1024 * 1024))
        fetch, problems, upgrade = plan_scenes(result, args.scene)
        if upgrade:
            return finish(EXIT_FETCH, UPGRADE_MESSAGE)
        revs = {bundle_revision(r) for _, r in fetch} - {None}
        top = bundle_revision(result)
        revision = revs.pop() if len(revs) == 1 else (top if not revs else None)
        pages = [(sid, None, ref) for sid, ref in fetch]
    summary["revision"] = revision
    if args.cover is not None:
        # The cover is the card's image: card.cover_render's size, clamped as
        # local_preview's viewport is (1200x630 without one, or with --html).
        cw, ch = cover_render.get("width"), cover_render.get("height")
        viewport = (max(320, min(cw, 4096)) if isinstance(cw, int) else COVER_VIEWPORT[0],
                    max(200, min(ch, 4096)) if isinstance(ch, int) else COVER_VIEWPORT[1])
    for sid, reason in problems:
        emit(_problem_record(sid, reason))
        summary["scenes"][sid] = reason
    if not pages:
        return finish(EXIT_OK, "nothing to render" + (": every selected scene is unavailable — fix the errors "
                                                       "storyboard__preview reported and preview again" if problems else ""))

    # -- Chromium ---------------------------------------------------------------
    if os.name != "posix" or sys.platform.startswith("win"):
        return finish(EXIT_NO_CHROMIUM, no_chromium_message([], "Local preview is not supported on Windows yet."))
    system = "Darwin" if sys.platform == "darwin" else "Linux"
    env = dict(os.environ)
    if args.chromium:
        env["CARDINAL_CHROMIUM"] = args.chromium
    found, searched = find_chromium(env, system, home)
    if not found:
        note(no_chromium_message(searched))
        return finish(EXIT_NO_CHROMIUM, no_chromium_message(searched))
    summary["chromium"] = found
    note(f"chromium: {found['path']} ({found['version']})")

    # -- fetch ----------------------------------------------------------------------
    workdir = Path(tempfile.mkdtemp(prefix="cardinal-preview-"))
    os.chmod(str(workdir), 0o700)
    fetched: list = []
    fetch_failures = 0
    first_failure = None
    try:
        if any(ref is not None for _, _, ref in pages):
            conn = connect_info(home, os.environ, args.runtime)
            if not conn:
                return finish(EXIT_FETCH, "Claude Code is not connected to Cardinal (no CARDINAL_MCP_URL / "
                                          "CARDINAL_MCP_API_KEY): run /cardinal:connect, then preview again")
            for i, (sid, _, ref) in enumerate(pages):
                if time.monotonic() > deadline - 15:
                    for rest, _, _ in pages[i:]:
                        emit(_problem_record(rest, "not fetched: this run's time budget is spent; "
                                                   "render the rest with --scene"))
                        summary["scenes"][rest] = "not fetched"
                    break
                try:
                    data = fetch_bundle(conn, ref, max_bytes, deadline=deadline - 15)
                except FetchError as e:
                    fetch_failures += 1
                    first_failure = first_failure or str(e)
                    emit(_problem_record(sid, str(e)))
                    summary["scenes"][sid] = str(e)
                    if e.status in (401, 403) or e.code == "revision_mismatch":
                        # The same answer for every scene: stop asking.
                        for rest, _, _ in pages[i + 1:]:
                            emit(_problem_record(rest, "not fetched: " + str(e)))
                            summary["scenes"][rest] = "not fetched"
                        break
                    continue
                dest = workdir / f"{sid}.html"
                _write_private(dest, data)
                fetched.append((sid, dest))
        else:
            fetched = [(sid, p) for sid, p, _ in pages]
        if not fetched:
            return finish(EXIT_FETCH if fetch_failures else EXIT_OK,
                          "no scene could be fetched" + (": " + first_failure if first_failure else ""))

        # -- render ---------------------------------------------------------------------
        out_dir = Path(args.out) if args.out else (
            home / ("." + args.runtime) / "cardinal" / "storyboards" / sb_id / (f"r{revision}" if revision is not None else "local"))
        _mkdir_private(out_dir, own_leaf=not args.out)
        summary["out_dir"] = str(out_dir)
        opts = {"theme": args.theme, "viewport": viewport, "dpr": max(MIN_DPR, min(args.dpr, MAX_DPR)), "ready_ms": ready_ms,
                "settle_ms": settle_ms, "cover": args.cover is not None}
        if opts["cover"]:
            opts["dpr"] = 1.0  # the card is 1200x630 pixels, not points
        browser = Browser(found["path"], workdir)
        watchdog = threading.Timer(max(5.0, deadline - time.monotonic()), browser.kill)
        watchdog.daemon = True
        try:
            try:
                cdp = browser.start()
            except LaunchError as e:
                note(str(e))
                return finish(EXIT_NO_CHROMIUM, no_chromium_message([found["path"]], str(e)))
            watchdog.start()
            for sid, path in fetched:
                if cdp.closed:
                    emit(_problem_record(sid, "not rendered: " + (cdp.closed or "Chromium exited")))
                    summary["scenes"][sid] = "not rendered"
                    continue
                if not opts["cover"]:  # a cover leaves the step PNGs alone
                    clear_scene_pngs(out_dir, sid, args.theme)
                r = render_page(cdp, path, sid, out_dir, opts)
                for rec in r["records"]:
                    emit(rec)
                summary["pngs"].extend(r["pngs"])
                summary["rendered"] += len(r["pngs"])
                if not r["ok"] or r["error"]:
                    rec = _problem_record(sid, r["error"] or "the Canvas reported frame errors")
                    rec.update(steps=r["steps"], state=r.get("state"), height=r.get("height"),
                               frame_errors=r["frame_errors"], protocol_errors=r["protocol_errors"])
                    emit(rec)
                    summary["scenes"][sid] = rec["error"]
                else:
                    summary["scenes"][sid] = f"rendered {len(r['pngs'])} step(s)"
                if r["blocked"]:
                    note(f"{sid}: blocked {r['blocked']} network request(s) from the page")
        finally:
            watchdog.cancel()
            browser.close()
        msg = (f"rendered {summary['rendered']} PNG(s); Read each one and judge whether the scene's point is obvious "
               "in five seconds without the transcript")
        if summary["rendered"] == 0 and fetch_failures and len(fetched) == 0:
            return finish(EXIT_FETCH, msg)
        return finish(EXIT_OK, msg)
    finally:
        if args.keep:
            note(f"kept {workdir}")
        else:
            shutil.rmtree(str(workdir), ignore_errors=True)


def _exit_on_sigterm(signum, frame):  # noqa: ARG001
    # SystemExit unwinds main()'s finally blocks: Chromium is closed and the
    # temp dir (bundle pages, profile) removed when the Bash tool times out.
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    sys.exit(main())
