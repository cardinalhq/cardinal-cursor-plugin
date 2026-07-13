"""OTLP/HTTP log-record building and emission.

Connection and resource facts are ARGUMENTS — no module state — so one
process can hold N connections (omnigent server) while CLI hook scripts
build theirs from AgentPaths per invocation. Failures are best-effort and
silent: telemetry must not break the agent loop.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import CORE_VERSION
from .paths import AgentPaths

DEFAULT_TIMEOUT_SEC = 2.0
DEFAULT_API_HEADER = "x-cardinalhq-api-key"


@dataclass(frozen=True)
class IngestConnection:
    endpoint: str  # base URL; /v1/logs is appended
    api_key: str
    api_header: str = DEFAULT_API_HEADER
    # Additional headers forwarded verbatim on every POST (core 0.2.0
    # gap #3 — Claude forwards every pair parsed from
    # OTEL_EXPORTER_OTLP_HEADERS, which may carry more than the auth key).
    extra_headers: tuple[tuple[str, str], ...] = ()


def connection_from_paths(paths: AgentPaths) -> IngestConnection | None:
    """The CLI-plugin default: connection facts from cardinal.json +
    cardinal-secrets.json. None when not connected (emit becomes a no-op)."""
    state = paths.read_state()
    secrets = paths.read_secrets()
    endpoint = state.get("ingest_endpoint")
    api_key = secrets.get("ingest_api_key")
    if not endpoint or not api_key:
        return None
    return IngestConnection(
        endpoint=str(endpoint).rstrip("/"),
        api_key=str(api_key),
        api_header=str(secrets.get("ingest_api_header") or DEFAULT_API_HEADER),
    )


def kv(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def log_record(event_name: str, attrs: dict[str, Any], ts_ns: int) -> dict[str, Any]:
    all_attrs = {"event_name": event_name, **attrs}
    return {
        "timeUnixNano": str(ts_ns),
        "observedTimeUnixNano": str(ts_ns),
        "severityNumber": 9,
        "severityText": "INFO",
        "body": {"stringValue": event_name},
        "attributes": [kv(k, v) for k, v in all_attrs.items() if v is not None and v != ""],
    }


def resource_attrs(
    *,
    service_name: str,
    agent_runtime: str,
    deployment_environment: str | None,
    user_email: str | None,
    org: str | None,
    plugin_version: str,
    include_core_version: bool = True,
) -> dict[str, str]:
    """Standard Cardinal resource attributes. Identity is an argument —
    CLI adapters read it from state, omnigent supplies actor.run_as."""
    attrs = {
        "service.name": service_name,
        "agent.runtime": agent_runtime,
        "deployment.environment": str(deployment_environment or "unknown"),
        "user.email": str(user_email or "unknown"),
        "cardinal.org": str(org or "unknown"),
        "cardinal.plugin_version": plugin_version,
    }
    if include_core_version:
        attrs["cardinal.core_version"] = CORE_VERSION
    return attrs


def passthrough_resource_attrs(
    pairs: dict[str, str],
    *,
    service_name: str,
    agent_runtime: str,
    plugin_version: str,
    include_core_version: bool = True,
) -> dict[str, str]:
    """Resource attributes from an externally-owned pair set, order
    preserved (core 0.2.0 gap #4 — Claude passes through whatever CSV
    cardinal-connect baked into OTEL_RESOURCE_ATTRIBUTES).
    service.name / agent.runtime are setdefaults; cardinal.plugin_version
    is an emit-time overwrite (the stamp must reflect the installed
    plugin, not what connect wrote at install time)."""
    attrs = dict(pairs)
    attrs.setdefault("service.name", service_name)
    attrs.setdefault("agent.runtime", agent_runtime)
    attrs["cardinal.plugin_version"] = plugin_version
    if include_core_version:
        attrs["cardinal.core_version"] = CORE_VERSION
    return attrs


def emit_records(
    records: list[dict[str, Any]],
    connection: IngestConnection | None,
    resource: dict[str, str],
    *,
    scope_name: str,
    scope_version: str,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> None:
    if not records or connection is None:
        return
    body = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [kv(k, v) for k, v in resource.items()],
                },
                "scopeLogs": [
                    {
                        "scope": {
                            "name": scope_name,
                            "version": scope_version,
                        },
                        "logRecords": records,
                    }
                ],
            }
        ]
    }
    headers = {
        "content-type": "application/json",
        **dict(connection.extra_headers),
        connection.api_header: connection.api_key,
    }
    req = urllib.request.Request(
        connection.endpoint + "/v1/logs",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except (urllib.error.URLError, OSError, TimeoutError):
        pass


def parse_ts_ns(raw: Any, fallback_ns: int) -> int:
    """ISO string, epoch millis, or epoch nanos → epoch nanos."""
    if isinstance(raw, str) and raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1_000_000_000)
        except ValueError:
            return fallback_ns
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw * 1_000_000) if raw < 1e13 else int(raw)
    return fallback_ns
