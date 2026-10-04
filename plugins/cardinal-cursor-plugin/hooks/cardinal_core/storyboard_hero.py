"""Upload the local render of a storyboard act's cover (or hero) scene after publish.

maestro never renders a Canvas. When storyboard__publish answers, its result
carries (conductor storyboard/card/hero.ts publishCardWire, rich unfurls
design §4.2):

    revision: R          the PRE-publish revision: the one storyboard__preview
                         reported, i.e. the r<R>/ directory the plugin's local
                         preview rendered into
    card: {hero_scene_id, designated_cover,
           hero_upload: {method: "PUT", path: "/api/orgs/<org>/storyboards/
                         <sb>/acts/<n>/hero?revision=R&scene=<id>",
                         content_type: "image/png", max_bytes: 2097152} | null}

The plugin then PUTs its own render of that scene to hero_upload.path,
unchanged, and link previews show it (the server validates, re-encodes and
decides which audience may see it).

  hero_png()   picks the file: r<R>/<scene>-cover.png (the 1200x630 cover
               render) when the scene is the designated cover and the file
               exists, else the last reveal step r<R>/<scene>-<N>.png (highest
               N; -dark and -cover files never match). Over the upload cap it
               is downscaled with Pillow when Pillow is importable, else it is
               reported as over the cap.
  upload_hero() PUTs it with the MCP key, X-Cardinal-Client and
               Content-Type: image/png, never across a redirect, and only to
               this connection's org's hero route.

Harness-neutral: the adapter supplies the storyboards directory, the
connection {origin, org, key} and its client string. Standard library only
(Pillow optional). Never raises from upload_hero for HTTP or network errors:
the caller reports them.
"""

from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NamedTuple, Optional

from .evidence_promote import no_redirect_opener

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
# Pillow downscale target: twice the card (1200x630), so a retina render
# keeps its detail and the server's 1200x630 re-encode still has pixels.
DOWNSCALE_BOX = (2400, 1260)

STORYBOARD_ID_RE = re.compile(r"^sb_[0-9a-f]{24}$")
SCENE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HERO_PATH_RE = re.compile(r"^/api/orgs/([^/?#]+)/storyboards/sb_[0-9a-f]{24}/acts/\d+/hero\?[^#\s]*$")
MAX_RESPONSE_BYTES = 64 * 1024


class HeroPick(NamedTuple):
    status: str            # "ok" | "missing" | "over_cap"
    path: Optional[Path]   # the render picked (also for over_cap)
    png: Optional[bytes]   # the bytes to upload ("ok" only)
    size: int              # the file's size in bytes (0 when missing)
    cover: bool            # the file is the 1200x630 cover render
    downscaled: bool = False


class UploadResult(NamedTuple):
    status: Optional[int]  # the HTTP status; None: no answer (network, timeout)
    body: dict             # the JSON answer ({} when none)
    error: Optional[str]   # an exception class name when there was no answer


def _step_pngs(rev_dir: Path, scene_id: str) -> list:
    pat = re.compile(r"^" + re.escape(scene_id) + r"-(\d+)\.png$")
    out = []
    try:
        entries = list(rev_dir.iterdir())
    except OSError:
        return []
    for p in entries:
        m = pat.match(p.name)
        if m and p.is_file():
            out.append((int(m.group(1)), p))
    return [p for _, p in sorted(out)]


def _downscale(data: bytes, cap: int) -> Optional[bytes]:
    """The PNG fitted into DOWNSCALE_BOX, when Pillow is importable and the
    result fits under `cap`; else None."""
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGBA") if im.mode not in ("RGB", "RGBA") else im
            im.thumbnail(DOWNSCALE_BOX)
            buf = io.BytesIO()
            im.save(buf, format="PNG", optimize=True)
    except Exception:
        return None
    out = buf.getvalue()
    return out if len(out) <= cap else None


def hero_png(storyboards_dir: Path, storyboard_id: str, revision: int, scene_id: str, designated_cover: bool,
             cap: int = MAX_UPLOAD_BYTES) -> HeroPick:
    """The local render to upload for `scene_id` at `revision`, from
    <storyboards_dir>/<storyboard_id>/r<revision>/."""
    none = HeroPick("missing", None, None, 0, False)
    if not (isinstance(storyboard_id, str) and STORYBOARD_ID_RE.match(storyboard_id)):
        return none
    if not (isinstance(scene_id, str) and SCENE_ID_RE.match(scene_id)):
        return none
    if not (isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0):
        return none
    rev_dir = Path(storyboards_dir) / storyboard_id / f"r{revision}"
    candidates = []
    cover = rev_dir / f"{scene_id}-cover.png"
    if designated_cover and cover.is_file():
        candidates.append((cover, True))
    steps = _step_pngs(rev_dir, scene_id)
    if steps:
        candidates.append((steps[-1], False))
    if not candidates:
        return none
    path, is_cover = candidates[0]
    try:
        data = path.read_bytes()
    except OSError:
        return none
    if not data.startswith(b"\x89PNG"):
        return none
    if len(data) <= cap:
        return HeroPick("ok", path, data, len(data), is_cover)
    smaller = _downscale(data, cap)
    if smaller is not None:
        return HeroPick("ok", path, smaller, len(data), is_cover, True)
    return HeroPick("over_cap", path, None, len(data), is_cover)


def valid_upload_path(conn: dict, path) -> bool:
    """The hero route of the connection's own org, as publish answered it."""
    if not isinstance(path, str):
        return False
    m = HERO_PATH_RE.match(path)
    return bool(m) and urllib.parse.unquote(m.group(1)) == conn.get("org")


def upload_hero(conn: dict, path: str, png: bytes, timeout: float, client: str, opener=None) -> UploadResult:
    """PUT `png` to conn.origin + path. Refuses (ValueError) a path that is
    not this org's hero route. HTTP errors and network failures come back in
    the result, never raised."""
    if not (conn.get("origin") and conn.get("key") and conn.get("org")):
        raise ValueError("not connected")
    if not valid_upload_path(conn, path):
        raise ValueError("not a hero upload path of the connected org")
    req = urllib.request.Request(conn["origin"] + path, data=png, method="PUT", headers={
        "X-CardinalHQ-API-Key": conn["key"],
        "X-Cardinal-Client": client,
        "Content-Type": "image/png",
        "Accept": "application/json",
    })
    opener = opener or no_redirect_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as err:
        try:
            raw = err.read(MAX_RESPONSE_BYTES)
        except OSError:
            raw = b""
        finally:
            err.close()
        status = err.code
    except Exception as e:  # URLError, timeout, connection reset
        return UploadResult(None, {}, type(e).__name__)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {}
    return UploadResult(status, body if isinstance(body, dict) else {}, None)
