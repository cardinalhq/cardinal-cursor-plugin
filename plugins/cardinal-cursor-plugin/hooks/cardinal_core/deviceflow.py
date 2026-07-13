"""Cardinal device-code consent flow and endpoint reachability probes.

Shared verbatim by every adapter's cardinal-connect. `client_id` names the
requesting plugin (e.g. "cardinal-codex-plugin") so the server can show
which agent is asking for consent.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

DEFAULT_POLL_TIMEOUT_SECS = 600
DEFAULT_POLL_INTERVAL_SECS = 5
INGEST_PROBE_RETRY_SLEEPS = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0)


class DeviceFlowError(RuntimeError):
    """Raised for any unrecoverable device-flow failure; message is
    user-presentable. Adapters decide how to exit (sys.exit for CLIs)."""


def derive_deployment_env(host: str) -> str:
    try:
        hostname = urllib.parse.urlparse(host).hostname or ""
    except Exception:
        return "unknown"
    if hostname == "app.cardinalhq.io":
        return "prod"
    if "dogfood" in hostname:
        return "dogfood"
    if "cardinalhq.io" in hostname:
        return "cardinal"
    return "customer"


def _post_json(url: str, body: dict, timeout: int = 15) -> tuple[int, dict | None]:
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, None


def start_device_code(host: str, scopes: list[str], client_id: str) -> dict:
    status, body = _post_json(
        host.rstrip("/") + "/api/auth/device/code",
        {
            "client": client_id,
            "scopes": scopes,
            "hostname": socket.gethostname(),
        },
    )
    if status != 201 or not body:
        err = (body or {}).get("error", f"HTTP {status}")
        desc = (body or {}).get("error_description", "")
        raise DeviceFlowError(f"device-code init failed: {err}{(': ' + desc) if desc else ''}")
    return body


def poll_device_token(
    host: str,
    device_code: str,
    client_id: str,
    interval: int,
    timeout: int,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, body = _post_json(
            host.rstrip("/") + "/api/auth/device/token",
            {"device_code": device_code, "client": client_id},
            timeout=20,
        )
        body = body or {}
        if status == 200:
            return body
        err = body.get("error", f"http_{status}")
        if err == "authorization_pending":
            time.sleep(interval)
            continue
        if err == "slow_down":
            interval += 5
            time.sleep(interval)
            continue
        if err == "access_denied":
            raise DeviceFlowError("Request was denied in the browser.")
        if err == "expired_token":
            raise DeviceFlowError("Consent request expired before approval. Re-run cardinal-connect.")
        desc = body.get("error_description", "")
        raise DeviceFlowError(f"device-code failed: {err}{(': ' + desc) if desc else ''}")
    raise DeviceFlowError("Timed out waiting for browser approval. Re-run cardinal-connect.")


def verify_mcp_reachable(mcp_url: str | None, api_key: str | None) -> tuple[bool, str]:
    if not mcp_url:
        return False, "server returned no MCP URL"
    if not api_key:
        return False, "server returned no MCP API key"
    req = urllib.request.Request(
        mcp_url,
        method="GET",
        headers={"x-cardinalhq-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status not in (401, 403), f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, f"HTTP {exc.code} - MCP key invalid"
        return True, f"HTTP {exc.code} - auth OK"
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, f"network error: {exc}"


def _ingest_probe_once(endpoint: str, api_key: str, api_header: str) -> tuple[bool, str]:
    url = endpoint.rstrip("/") + "/v1/metrics"
    req = urllib.request.Request(
        url,
        data=b"",
        method="POST",
        headers={
            "content-type": "application/x-protobuf",
            api_header: api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status not in (401, 403), f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, f"HTTP {exc.code} - ingest key invalid"
        if exc.code < 500:
            return True, f"HTTP {exc.code} on empty body - auth OK"
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, f"network error: {exc}"


def verify_ingest_reachable(
    ingest: dict | None,
    log: Callable[[str], None] = print,
    sleeps: tuple[float, ...] = INGEST_PROBE_RETRY_SLEEPS,
) -> tuple[bool, str]:
    """Probe the ingest endpoint. Retries the 401 case on the retry
    ladder because a freshly minted key can take seconds to propagate;
    `sleeps` is injectable so callers (and tests) can shorten it
    (core 0.2.0 gap #7)."""
    if not ingest:
        return False, "server returned no ingest credential"
    endpoint = ingest.get("endpoint")
    api_key = ingest.get("api_key")
    api_header = ingest.get("api_header") or "x-cardinalhq-api-key"
    if not endpoint:
        return False, "server returned no ingest endpoint"
    if not api_key:
        return False, "server returned no ingest API key"

    last_msg = ""
    for attempt in range(len(sleeps) + 1):
        ok, msg = _ingest_probe_once(str(endpoint), str(api_key), str(api_header))
        if ok:
            return True, msg
        last_msg = msg
        if "HTTP 401" not in msg:
            return False, msg
        if attempt < len(sleeps):
            sleep_s = sleeps[attempt]
            log(
                f"ingest key returned 401; retrying in {sleep_s:.0f}s "
                f"(attempt {attempt + 2}/{len(sleeps) + 1})..."
            )
            time.sleep(sleep_s)
    total = sum(sleeps)
    return False, f"{last_msg} after ~{total:.0f}s; ingest key did not propagate"
