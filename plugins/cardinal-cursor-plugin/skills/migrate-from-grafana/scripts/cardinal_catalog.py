#!/usr/bin/env python3
"""Read Cardinal's metric/label catalog and suggest a Grafana -> Cardinal name mapping.

Grafana (Mimir/Prometheus) and Cardinal (lakerunner) often store the same OTel
metric under different names: Prometheus conversion appends `_total` and unit
suffixes (`_seconds`, `_milliseconds`, `_bytes`, ...) and turns dots into
underscores. This script finds, for every metric/label referenced by the export,
the name Cardinal actually has, so queries keep returning data after migration.

Reads CARDINAL_URL, CARDINAL_ORG_ID and CARDINAL_API_KEY or CARDINAL_TOKEN (env or --env-file).
CARDINAL_URL / CARDINAL_ORG_ID default to the cardinal-connect org; --check without a
token uses the cardinal-connect MCP key.

Usage:
  cardinal_catalog.py --check [--instance <id-or-slug>] [--env-file .env.cardinal]
  cardinal_catalog.py --export ./export --out ./catalog [--instance <id-or-slug>] [--env-file .env.cardinal]

  cardinal_catalog.py --orgs

--orgs lists the user's Cardinal orgs (via the cardinal-connect token) so they can pick one.
--check only confirms Cardinal is receiving data and writes nothing. Without a token it
exits 3 when cardinal-connect hasn't been run, 4 when its key needs --rotate, and 6 when
the chosen org isn't the connected one (then it needs the login token). Any script exits
5 when the login token is expired or cut off.

Writes:
  catalog/instance.json          chosen lakerunner instance {id, slug, name}
  catalog/metrics.json           Cardinal metric names
  catalog/labels.json            Cardinal metric label names
  catalog/log_labels.json        Cardinal log label names (if the logs API answered)
  catalog/mapping.suggested.json mapping in convert.py's format + "_review" notes
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

UNIT_SUFFIXES = ["_seconds", "_milliseconds", "_microseconds", "_nanoseconds", "_bytes", "_bits",
                 "_ratio", "_percent", "_celsius", "_meters", "_hertz", "_volts", "_amperes",
                 "_joules", "_grams", "_minutes", "_hours", "_days"]
HIST_SUFFIXES = ["_bucket", "_sum", "_count"]


def load_env_file(path):
    # A missing file is fine: with cardinal-connect there is no .env.cardinal.
    if path and os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


# Where the agent's Cardinal connect wrote its state: CARDINAL_AGENT_HOME (each
# adapter's SKILL.md sets it, e.g. ~/.codex), else the first agent home that has one.
AGENT_HOMES = ["~/.claude", "~/.codex", "~/.cursor", "~/.gemini"]
# --check exit codes the skill branches on: connect, or reconnect with --rotate.
EXIT_NOT_CONNECTED, EXIT_CONNECT_REJECTED, EXIT_NEEDS_TOKEN = 3, 4, 6
EXIT_BAD_TOKEN = 5  # login token expired / truncated: ask for a fresh one, resume the same step
COPY_HINT = ("Copy a fresh one: reload Cardinal, dev tools > Network, right-click an /api/orgs/... "
             "request > Copy > Copy as cURL, and take everything after 'Bearer ' (the Headers pane "
             "cuts long tokens off).")


def agent_home():
    """The agent home dir holding cardinal.json, or None when not connected."""
    homes = [os.environ["CARDINAL_AGENT_HOME"]] if os.environ.get("CARDINAL_AGENT_HOME") else AGENT_HOMES
    for home in map(os.path.expanduser, homes):
        if os.path.exists(os.path.join(home, "cardinal.json")):
            return home
    return None


def _read_json(path):
    try:
        data = json.load(open(path))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def connect_info():
    """What the agent's Cardinal connect saved: {host, org_id, user_email, mcp_url,
    mcp_key, act_key, act_endpoint, act_scopes} (missing keys absent), or {} when not
    connected. The MCP key reads data and can enable/disable alert rules; the act
    key (maestro:act, from every agent's connect) can list the user's orgs, and — when connected
    with `dashboards:write` / `alerts:write` / `telemetry:query` — create and update
    dashboards / alert rules and run the catalog and validation queries."""
    home = agent_home()
    if not home:
        return {}
    state = _read_json(os.path.join(home, "cardinal.json"))
    sec = _read_json(os.path.join(home, "cardinal-secrets.json"))
    info = {k: state[k] for k in ("host", "org_id", "user_email", "act_scopes") if state.get(k)}
    # Claude Code keeps the MCP key in settings.json env; the other agents in cardinal-secrets.json.
    env = _read_json(os.path.join(home, "settings.json")).get("env") or {}
    if isinstance(env, dict) and env.get("CARDINAL_MCP_URL") and env.get("CARDINAL_MCP_API_KEY"):
        info.update(mcp_url=env["CARDINAL_MCP_URL"], mcp_key=env["CARDINAL_MCP_API_KEY"])
    elif state.get("mcp_url") and sec.get("mcp_api_key"):
        info.update(mcp_url=state["mcp_url"], mcp_key=sec["mcp_api_key"])
    if sec.get("act_api_key") and sec.get("act_endpoint"):
        info.update(act_key=sec["act_api_key"], act_endpoint=sec["act_endpoint"])
    return info


def connect_can(conn, scopes):
    """Does the cardinal-connect act token carry every scope in `scopes`?"""
    return bool(conn.get("act_key")) and set(scopes) <= set(conn.get("act_scopes") or [])


# Every step of the migration together needs these. Reconnect hints always ask for all
# of them: --rotate replaces the token, so asking for one step's scope would only move
# the failure to the next step.
MIGRATION_SCOPES = ["dashboards:write", "alerts:write", "telemetry:query"]


def missing_scopes_message(conn, connect_scopes):
    """Why there's no usable credential, and the one command that fixes it."""
    alt = "or put CARDINAL_TOKEN (or CARDINAL_API_KEY) in .env.cardinal"
    if not connect_scopes:
        return "no Cardinal login token: put CARDINAL_TOKEN (or CARDINAL_API_KEY) in .env.cardinal"
    scopes = " ".join(MIGRATION_SCOPES)
    if not conn:
        return f"not connected to Cardinal: run `cardinal-connect {scopes}`, {alt}"
    have = set(conn.get("act_scopes") or []) if conn.get("act_key") else set()
    missing = [s for s in connect_scopes if s not in have]
    return (f"the cardinal-connect token lacks {', '.join(missing)} (this step needs it): reconnect "
            f"with `cardinal-connect --rotate {scopes}` so every migration step is covered, {alt}")


def list_orgs(conn):
    """The user's Cardinal orgs [{id, name, slug, role}] via the connect act key, or None."""
    if not conn.get("act_key"):
        return None
    r = urllib.request.Request(conn["act_endpoint"].rstrip("/") + "/api/me",
                               headers={"X-CardinalHQ-API-Key": conn["act_key"]})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            me = json.loads(resp.read())
    except (urllib.error.URLError, ValueError):
        return None
    return [{"id": o.get("orgId") or o.get("id"), "name": o.get("name"), "slug": o.get("slug"),
             "role": o.get("role")} for o in me.get("orgs", [])]


def token_problem(token):
    """Why a login token (JWT) can't work, or None. Also reports time left on stderr."""
    import base64
    import time
    parts = token.split(".")
    if len(parts) != 3:
        return None  # not a JWT; let the server decide
    try:
        pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
        head = json.loads(base64.urlsafe_b64decode(pad(parts[0])))
        claims = json.loads(base64.urlsafe_b64decode(pad(parts[1])))
        sig = base64.urlsafe_b64decode(pad(parts[2]))
    except (ValueError, TypeError):
        return "the login token is garbled (copied incompletely?)"
    if head.get("alg", "").startswith(("RS", "PS")) and len(sig) not in (256, 384, 512):
        return "the login token is cut off (its signature is incomplete)"
    left = int(claims.get("exp", 0) - time.time()) if claims.get("exp") else None
    if left is not None and left <= 0:
        return f"the login token expired {-left}s ago (they only last ~5 minutes)"
    if left is not None:
        print(f"login token valid for {left // 60}m{left % 60:02d}s", file=sys.stderr)
    return None


def mcp_for(org_id):
    """A CardinalMCP client when cardinal-connect is connected to org_id, else None
    (its key is bound to the org approved at connect time)."""
    conn = connect_info()
    if conn.get("mcp_key") and conn.get("org_id") == org_id:
        return CardinalMCP(conn["mcp_url"], conn["mcp_key"])
    return None


class Cardinal:
    """Maestro client. Auth is an org API key (admin:all scope) or the
    `cardinal-connect` act token (dashboards:write / alerts:write /
    telemetry:query scopes), both
    sent as X-CardinalHQ-API-Key, or the user's own login token (sent as Bearer).
    The act token and login token carry the user's org role: Member (or Owner)
    can write dashboards and alert rules.
    CARDINAL_URL / CARDINAL_ORG_ID default to the `cardinal-connect` org."""

    def __init__(self, url, key, org, token=None):
        self.url, self.key, self.org, self.token = url.rstrip("/"), key, org, token

    @classmethod
    def from_env(cls, connect_scopes=None):
        """connect_scopes: the `cardinal-connect` act-token scopes that cover every
        request the caller will make (e.g. ["dashboards:write"]; lakerunner
        instance/metric/query routes need "telemetry:query"). When given and the
        connect token carries them all, it is used if no login token / API key
        is set."""
        url, key, token, org = (os.environ.get(k) for k in
                                ("CARDINAL_URL", "CARDINAL_API_KEY", "CARDINAL_TOKEN", "CARDINAL_ORG_ID"))
        conn = connect_info()
        url, org = url or conn.get("host"), org or conn.get("org_id")
        if not (key or token) and connect_scopes and connect_can(conn, connect_scopes):
            key, url = conn["act_key"], url or conn.get("act_endpoint")
            print(f"using the cardinal-connect token ({', '.join(connect_scopes)})", file=sys.stderr)
        if not (key or token):
            sys.exit(missing_scopes_message(conn, connect_scopes))
        if not url or not org:
            sys.exit("CARDINAL_URL and CARDINAL_ORG_ID must be set (or run cardinal-connect to fill them in)")
        if token and token.lower().startswith("bearer "):
            token = token[7:]
        if token and not key:
            problem = token_problem(token.strip())
            if problem:
                print(f"{problem}. {COPY_HINT}", file=sys.stderr)
                sys.exit(EXIT_BAD_TOKEN)
        return cls(url, key, org, token.strip() if token else token)

    def req(self, method, path, params=None, body=None):
        if params:
            path += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["X-CardinalHQ-API-Key"] = self.key
        else:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.org:
            headers["X-Org-Id"] = self.org
        r = urllib.request.Request(self.url + path, method=method, headers=headers,
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                raw = resp.read()
                try:
                    return resp.status, json.loads(raw or b"null")
                except json.JSONDecodeError:
                    return resp.status, raw.decode(errors="replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            if e.code == 401 and self.token and not self.key:
                print(f"Cardinal rejected the login token (401). {COPY_HINT}", file=sys.stderr)
                sys.exit(EXIT_BAD_TOKEN)
            return e.code, body
        except urllib.error.URLError as e:
            return 0, str(e.reason)


class NativeQuery:
    """lakerunner's native query API via Maestro: POST {q, s, e, step} to
    /api/lakerunner/<instance>/query/<signal>/<op>; results stream back as SSE."""

    def __init__(self, cardinal, instance_id, window_ms=3600_000):
        import time
        self.c, self.base = cardinal, f"/api/lakerunner/{instance_id}/query"
        now = int(time.time() * 1000)
        self.range = {"s": str(now - window_ms), "e": str(now)}

    @staticmethod
    def events(body):
        out = []
        for line in (body if isinstance(body, str) else "").split("\n"):
            if line.startswith("data:"):
                try:
                    out.append(json.loads(line[5:].strip()))
                except json.JSONDecodeError:
                    pass
        return out

    def tags(self, signal, metric=None):
        body = dict(self.range, **({"q": metric} if metric else {}))
        code, resp = self.c.req("POST", f"{self.base}/{signal}/tags", body=body)
        return resp.get("tags", []) if code == 200 and isinstance(resp, dict) else []

    def tag_values(self, signal, tag):
        code, resp = self.c.req("POST", f"{self.base}/{signal}/tagvalues",
                                params={"tagName": tag}, body=dict(self.range))
        if code != 200:
            return None
        vals = []
        for e in self.events(resp):
            d = e.get("data", {})
            if e.get("type") == "result" and isinstance(d, dict) and d.get("value") is not None:
                vals.append(str(d["value"]))
        return vals

    def query(self, signal, expr, step=60):
        """Return (number of result points, error message or None)."""
        body = dict(self.range, q=expr, step=step)
        if signal == "logs":
            body.update(limit=50, reverse=True)
        code, resp = self.c.req("POST", f"{self.base}/{signal}/query", body=body)
        if code != 200:
            return 0, f"HTTP {code}: {str(resp)[:200]}"
        ev = self.events(resp)
        errs = [e for e in ev if e.get("type") == "error"]
        if errs:
            return 0, json.dumps(errs[0].get("data", errs[0]))[:200]
        return sum(1 for e in ev if e.get("type") == "result"), None

    def series(self, signal, expr, step=60, rng=None):
        """Return ({series key: {unix seconds: value}}, error or None). The key is the
        series' labels without Cardinal's internal ones (see series_key)."""
        body = dict(rng or self.range, q=expr, step=step)
        code, resp = self.c.req("POST", f"{self.base}/{signal}/query", body=body)
        if code != 200:
            return {}, f"HTTP {code}: {str(resp)[:200]}"
        out = {}
        for e in self.events(resp):
            if e.get("type") == "error":
                return {}, json.dumps(e.get("data", e))[:200]
            d = e.get("data", {})
            if e.get("type") == "result" and isinstance(d, dict) and isinstance(d.get("value"), (int, float)):
                out.setdefault(series_key(d.get("tags") or {}), {})[int(d["timestamp"]) // 1000] = float(d["value"])
        return out, None

    def values(self, signal, expr, step=60):
        """All numeric values the query returns, across series (for the probes)."""
        s, err = self.series(signal, expr, step)
        return [v for pts in s.values() for v in pts.values()], err


# Labels Cardinal adds to raw series (not the user's): left out when comparing series.
INTERNAL_LABELS = re.compile(r"^(__name__|name|chq_.*|cardinalhq_.*|_cardinalhq.*)$")


def series_key(labels):
    """A stable key for a series: its user labels, sorted, without Cardinal's internal ones."""
    return tuple(sorted((k, str(v)) for k, v in labels.items() if not INTERNAL_LABELS.match(k)))


def median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def quantile_usable(q50, q99, err=None):
    """Does histogram_quantile give sane answers on this histogram? Lakerunner returns
    negative values (-1, -1.0005) for histograms of zero-valued observations, and an
    engine without the function errors; either way the converter falls back to the average."""
    if err or not q50 or not q99:
        return False
    if any(v != v or v < 0 or v in (float("inf"), float("-inf")) for v in q50 + q99):
        return False
    return median(q50) <= median(q99) * (1 + 1e-9)


# How Cardinal answers histogram_count(rate(h[w])) (the request rate):
#   per_second  Prometheus semantics: requests/s at any step
#   per_minute  the count in each 60s rollup bucket, whatever [w] says (lakerunner on
#               delta OTLP histograms): /60 gives requests/s, but only at <= 60s steps
#   unknown     not measurable (no data at one of the steps)
RATE_BUCKET_SECONDS = 60


def rate_mode(at60, at120, err=None):
    """Classify the engine from histogram_count(rate(h[5m])) at step 60 and step 120:
    a per-bucket count doubles when the step doubles, a per-second rate doesn't."""
    a, b = median(at60 or []), median(at120 or [])
    if err or not a or not b:
        return "unknown"
    r = b / a
    if 0.8 <= r <= 1.25:
        return "per_second"
    if 1.6 <= r <= 2.4:
        return "per_minute"
    return "unknown"


def probe_histograms(native, fams):
    """{family: cardinal name} -> how this Cardinal instance answers the histogram
    functions the converter uses: per-family histogram_quantile usability, and the
    engine's request-rate semantics (measured once, on the first family with data)."""
    out = {"histogram_quantile_ok": {}, "histogram_rate_mode": "unknown"}
    for fam, m in sorted(fams.items()):
        q50, e1 = native.values("metrics", f"histogram_quantile(0.5, sum(rate({m}[5m])))")
        q99, e2 = native.values("metrics", f"histogram_quantile(0.99, sum(rate({m}[5m])))")
        out["histogram_quantile_ok"][fam] = quantile_usable(q50, q99, e1 or e2)
        if out["histogram_rate_mode"] == "unknown":
            expr = f"sum(histogram_count(rate({m}[5m])))"
            a, ea = native.values("metrics", expr, step=60)
            b, eb = native.values("metrics", expr, step=120)
            out["histogram_rate_mode"] = rate_mode(a, b, ea or eb)
    return out


def probe_or_vector(native, metric):
    """Does `<empty> or vector(0)` answer 0? Lakerunner answers nothing when the left
    side has no series, so a Grafana panel that shows 0 shows no data in Cardinal."""
    vals, err = native.values("metrics", f'sum(rate({metric}{{migrate_probe="none"}}[5m])) or vector(0)')
    return bool(vals) and not err


def decide_label_drops(matchers, used, metric_labels, log_labels, values, renames=None):
    """Which label filters to remove, per signal.

    matchers: {"metrics"|"logs": {(label, value), ...}} literal `label="value"` filters
    used:     {"metrics"|"logs": {label, ...}} every label the queries reference
    values:   (signal, label) -> set of values Cardinal has for it (or None)
    renames:  {grafana label: cardinal label} applied before checking
    A filter is removed only from the signal that lacks it: a label that exists on
    logs but not on these metrics stays on the log queries.
    Returns ({"metrics": set, "logs": set}, review notes)."""
    have = {"metrics": metric_labels, "logs": log_labels}
    renames = renames or {}
    drops, review = {"metrics": set(), "logs": set()}, []
    for sig in ("metrics", "logs"):
        for label, value in sorted(matchers.get(sig, ())):
            if label in ("detected_level", "__name__") or label in drops[sig]:
                continue
            name = renames.get(label, label)
            if name not in have[sig]:
                drops[sig].add(label)
                review.append({"label": label, "value": value, "signal": sig,
                               "issue": f"label not on the {sig} in Cardinal: this filter is removed from {sig} "
                                        "queries, which widens them (edit drop_labels / drop_log_labels if wrong)",
                               "present_on_other_signal": label in have["logs" if sig == "metrics" else "metrics"]})
                continue
            seen = values(sig, name) or set()
            if seen and value not in seen:
                drops[sig].add(label)
                review.append({"label": label, "value": value, "signal": sig,
                               "issue": f"value not found on the {sig} in Cardinal: this filter is removed from "
                                        f"{sig} queries, which widens them", "values_in_cardinal": sorted(seen)[:10]})
        for label in sorted(used.get(sig, ())):
            if (label in ("detected_level", "__name__", "le") or label in drops[sig]
                    or renames.get(label, label) in have[sig]):
                continue
            drops[sig].add(label)
            review.append({"label": label, "signal": sig,
                           "issue": f"label not on the {sig} in Cardinal: filters on it are removed from {sig} queries"})
    return drops, review


class CardinalMCP:
    """Minimal client for the Cardinal MCP server `cardinal-connect` wires up
    (streamable HTTP, JSON-RPC). Used for the pre-flight data check, which then
    needs no login token."""

    def __init__(self, url, key):
        self.url, self.key, self.session, self.next_id = url, key, None, 1
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "migrate-from-grafana", "version": "1"}})
        self._rpc("notifications/initialized", {}, notify=True)

    def _rpc(self, method, params, notify=False):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "X-CardinalHQ-API-Key": self.key}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            msg["id"], self.next_id = self.next_id, self.next_id + 1
        r = urllib.request.Request(self.url, data=json.dumps(msg).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(r, timeout=120) as resp:
                self.session = resp.headers.get("Mcp-Session-Id") or self.session
                raw = resp.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                print(f"the cardinal-connect MCP key was rejected ({e.code}); reconnect with "
                      "cardinal-connect --rotate", file=sys.stderr)
                sys.exit(EXIT_CONNECT_REJECTED)
            sys.exit(f"Cardinal MCP call {method} failed ({e.code}): {e.read().decode(errors='replace')[:300]}")
        except urllib.error.URLError as e:
            sys.exit(f"cannot reach Cardinal MCP at {self.url}: {e.reason}")
        if notify:
            return None
        if not raw.lstrip().startswith("{"):  # SSE framing
            raw = "".join(line[5:] for line in raw.splitlines() if line.startswith("data:"))
        return json.loads(raw) if raw.strip() else {}

    def call(self, tool, args):
        """Return (text, is_error)."""
        resp = self._rpc("tools/call", {"name": tool, "arguments": args})
        if "error" in resp:
            return str(resp["error"].get("message", resp["error"])), True
        res = resp.get("result", {})
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        return text, bool(res.get("isError"))


def rule_states(c, instance_slug):
    """{rule id: True/False (enabled)} as Cardinal reports it now, or None if unknown.
    Read back through the MCP tool when connected to this org (it states enabled/
    disabled per rule), else from the REST listing's `enabled` field."""
    code, body = c.req("GET", f"/api/orgs/{c.org}/alert-rules")
    rules = body if isinstance(body, list) else (body or {}).get("rules", []) if isinstance(body, dict) else []
    rules = rules if code == 200 else []
    mcp = mcp_for(c.org)
    if mcp:
        text, err = mcp.call("lakerunner__manage_alert_rules", {"instance": instance_slug, "action": "list"})
        if not err:
            # the MCP lists lakerunner rule ids; key them by Maestro's rule id, as callers do
            by_lr = {m.group(1): m.group(2) == "enabled"
                     for m in re.finditer(r"id=([0-9a-f-]+),[^)]*\b(enabled|disabled)\)", text)}
            return {r.get("id"): by_lr[r["lakerunnerRuleId"]] for r in rules if r.get("lakerunnerRuleId") in by_lr}
    states = {r.get("id"): r["enabled"] for r in rules if isinstance(r.get("enabled"), bool)}
    return states or None


def lakerunner_rule_id(c, rule_id):
    """Maestro's alert-rule id -> the lakerunner rule id the MCP tool expects (None if not synced yet)."""
    code, body = c.req("GET", f"/api/orgs/{c.org}/alert-rules")
    rules = body if isinstance(body, list) else (body or {}).get("rules", []) if isinstance(body, dict) else []
    return next((r.get("lakerunnerRuleId") for r in rules if r.get("id") == rule_id), None) if code == 200 else None


def set_rule_enabled(c, instance_slug, rule_id, enabled):
    """Switch a rule on/off. Returns an error message, or None when Cardinal confirms it.
    Uses the MCP enable/disable action: a REST PUT of {"enabled": false} is accepted
    but leaves the rule enabled."""
    mcp = mcp_for(c.org)
    if not mcp:
        return ("can't switch it " + ("on" if enabled else "off") + " without cardinal-connect to this org; "
                "do it in Cardinal's Alerts page")
    lr_id = lakerunner_rule_id(c, rule_id)
    if not lr_id:
        return "Cardinal hasn't synced the rule to the data lake yet; re-run this step shortly"
    text, err = mcp.call("lakerunner__manage_alert_rules",
                         {"instance": instance_slug, "action": "enable" if enabled else "disable", "rule_id": lr_id})
    if err:
        return text[:200]
    state = (rule_states(c, instance_slug) or {}).get(rule_id)
    return None if state is enabled else f"Cardinal still reports it {'enabled' if state else 'disabled'}"


def check_via_mcp(conn, want_instance):
    """Pre-flight with the cardinal-connect MCP key: is the org receiving metrics?"""
    mcp = CardinalMCP(conn["mcp_url"], conn["mcp_key"])
    print(f"using cardinal-connect ({conn.get('user_email', 'unknown user')}, org {conn.get('org_id')})")
    text, err = mcp.call("lakerunner__list_instances", {})
    try:
        instances = json.loads(text).get("instances", []) if not err else []
    except ValueError:
        instances = []
    if not instances:
        sys.exit(f"this Cardinal org has no lakerunner (data lake) instance: {text[:300]}")
    inst = instances[0]
    if want_instance:
        match = [i for i in instances if want_instance in (i.get("slug"), i.get("name"))]
        if not match:
            sys.exit(f"instance '{want_instance}' not found; available: {[i.get('slug') for i in instances]}")
        inst = match[0]
    elif len(instances) > 1:
        print(f"note: {len(instances)} instances; checked the default '{inst['slug']}'. "
              f"Pass --instance to choose: {[i.get('slug') for i in instances]}")
    text, err = mcp.call("lakerunner__discover_metrics",
                         {"instance": inst["slug"], "question": "request rate, latency, errors, cpu, memory"})
    found = re.findall(r"^\s*\d+\.\s+\*\*([^*]+)\*\*", text, re.M)
    if err or not found:
        sys.exit(f"Cardinal isn't receiving metrics on '{inst.get('name') or inst['slug']}' yet"
                 + (f": {text[:300]}" if err else ""))
    print(f"Cardinal is receiving data: instance '{inst['slug']}' ({inst.get('name')}), "
          f"metrics found e.g. {', '.join(found[:5])}")


def norm(name):
    return re.sub(r"[.\-/]", "_", name).lower()


def metric_candidates(name):
    """Plausible Cardinal spellings of a Grafana metric name, most specific first."""
    out = [name]
    base = name
    if base.endswith("_total"):
        base = base[: -len("_total")]
        out.append(base)
    for sfx in UNIT_SUFFIXES:
        if base.endswith(sfx):
            out.append(base[: -len(sfx)])
            if name.endswith("_total"):
                out.append(base[: -len(sfx)] + "_total")
    return list(dict.fromkeys(out))


def all_exprs(export):
    exprs = []
    ddir = os.path.join(export, "dashboards")
    for fn in os.listdir(ddir):
        stack = list(json.load(open(os.path.join(ddir, fn))).get("panels", []))
        while stack:
            p = stack.pop()
            stack.extend(p.get("panels", []))
            exprs += [t["expr"] for t in p.get("targets", []) if t.get("expr")]
    apath = os.path.join(export, "alerts.json")
    if os.path.exists(apath):
        for g in json.load(open(apath)):
            for r in g["rules"]:
                exprs += [d["model"]["expr"] for d in r.get("grafana_alert", {}).get("data", [])
                          if d.get("model", {}).get("expr")]
    return exprs


def exact_matchers(export):
    """(label, value) pairs for literal equality matchers without variables."""
    out = set()
    for e in all_exprs(export):
        out |= matchers_in(e)
    return out


def matchers_in(expr):
    return {(label, value) for label, value in re.findall(r'([a-zA-Z_][\w.]*)\s*=\s*"([^"$]*)"', expr) if value}


def labels_in(expr):
    """Label names an expression filters or groups on."""
    stripped = re.sub(r'"(?:[^"\\]|\\.)*"', '""', expr)
    labels = set()
    for block in re.findall(r"\{([^}]*)\}", stripped):
        labels.update(re.findall(r"([a-zA-Z_][\w.]*)\s*(?:=~|!~|!=|=)", block))
    for grp in re.findall(r"\b(?:by|without|on|ignoring)\s*\(([^)]*)\)", stripped):
        labels.update(x.strip() for x in grp.split(",") if x.strip())
    labels.update(re.findall(r'\| *([a-zA-Z_]\w*) *(?:=~|!~|!=|=)', stripped))
    return labels - {"le", "__name__"}


def is_log_query(expr, ds_kind=None):
    if ds_kind:
        return ds_kind == "loki"
    s = expr.strip()
    return s.startswith("{") or bool(re.search(r"(_over_time|rate)\s*\(\s*\{", s) and ("|" in s or "_over_time" in s))


def exprs_by_signal(export):
    """{"metrics": [...], "logs": [...]}: every query, by the datasource it ran on."""
    try:
        ds = json.load(open(os.path.join(export, "datasources.json")))
    except (OSError, ValueError):
        ds = {}

    def kind(ref, fallback=None):
        uid = ref.get("uid") if isinstance(ref, dict) else ref
        t = ref.get("type") if isinstance(ref, dict) else None
        return t or (ds.get(uid) or {}).get("type") or fallback

    out = {"metrics": [], "logs": []}
    ddir = os.path.join(export, "dashboards")
    for fn in os.listdir(ddir):
        stack = list(json.load(open(os.path.join(ddir, fn))).get("panels", []))
        while stack:
            p = stack.pop()
            stack.extend(p.get("panels", []))
            pk = kind(p.get("datasource"))
            for t in p.get("targets", []):
                if t.get("expr"):
                    out["logs" if is_log_query(t["expr"], kind(t.get("datasource"), pk)) else "metrics"].append(t["expr"])
    apath = os.path.join(export, "alerts.json")
    if os.path.exists(apath):
        for g in json.load(open(apath)):
            for r in g["rules"]:
                for d in r.get("grafana_alert", {}).get("data", []):
                    e = d.get("model", {}).get("expr")
                    if e:
                        out["logs" if is_log_query(e, kind(d.get("datasourceUid"))) else "metrics"].append(e)
    return out


def referenced_names(export, with_histograms=False):
    """Collect metric identifiers and label names used by the exported queries."""
    exprs = []
    ddir = os.path.join(export, "dashboards")
    for fn in os.listdir(ddir):
        dash = json.load(open(os.path.join(ddir, fn)))
        stack = list(dash.get("panels", []))
        while stack:
            p = stack.pop()
            stack.extend(p.get("panels", []))
            for t in p.get("targets", []):
                if t.get("expr"):
                    exprs.append(t["expr"])
        for v in dash.get("templating", {}).get("list", []):
            q = v.get("query")
            q = q.get("query") if isinstance(q, dict) else q
            if isinstance(q, str):
                exprs.append(q)
    apath = os.path.join(export, "alerts.json")
    if os.path.exists(apath):
        for g in json.load(open(apath)):
            for r in g["rules"]:
                for d in r.get("grafana_alert", {}).get("data", []):
                    if d.get("model", {}).get("expr"):
                        exprs.append(d["model"]["expr"])
    metrics, labels = set(), set()
    for e in exprs:
        stripped = re.sub(r'"(?:[^"\\]|\\.)*"', '""', e)
        for block in re.findall(r"\{([^}]*)\}", stripped):
            labels.update(re.findall(r"([a-zA-Z_][\w.]*)\s*(?:=~|!~|!=|=)", block))
        for grp in re.findall(r"\b(?:by|without|on|ignoring)\s*\(([^)]*)\)", stripped):
            labels.update(x.strip() for x in grp.split(",") if x.strip())
        for m in re.findall(r'\| *([a-zA-Z_]\w*) *(?:=~|!~|!=|=)', stripped):
            labels.add(m)
        body = re.sub(r"\{[^}]*\}|\[[^\]]*\]", " ", stripped)
        body = re.sub(r"\|\s*[a-zA-Z_]\w*", " ", body)  # LogQL pipeline labels (| detected_level=...) aren't metrics
        body = re.sub(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", " ", body)
        body = re.sub(r"\blabel_values\s*\(([^,)]*),[^)]*\)", r" \1 ", body)
        for tok in re.findall(r"(?<![\w:.$])([a-zA-Z_:][\w:]*)(?![\w(])", body):
            if "_" in tok and tok.lower() == tok:
                metrics.add(tok)
    # Collapse X_bucket/X_sum/X_count to the histogram family X, but only when
    # the family really is a histogram (a _bucket series is referenced).
    # Otherwise a gauge like go_goroutine_count would lose its real suffix.
    hist_families = {m[: -len("_bucket")] for m in metrics if m.endswith("_bucket")}
    families = set()
    for m in metrics:
        fam = m
        for sfx in HIST_SUFFIXES:
            if m.endswith(sfx) and m[: -len(sfx)] in hist_families:
                fam = m[: -len(sfx)]
        families.add(fam)
    if with_histograms:
        return sorted(families), sorted(labels - {"le", "__name__"}), hist_families
    return sorted(families), sorted(labels - {"le", "__name__"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export")
    ap.add_argument("--out")
    ap.add_argument("--instance")
    ap.add_argument("--env-file")
    ap.add_argument("--check", action="store_true", help="only confirm Cardinal is receiving data")
    ap.add_argument("--orgs", action="store_true", help="list the user's Cardinal orgs and exit")
    args = ap.parse_args()
    if not (args.check or args.orgs) and not (args.export and args.out):
        ap.error("--export and --out are required (or pass --check / --orgs)")
    load_env_file(args.env_file)
    conn = connect_info()
    if args.orgs:
        orgs = list_orgs(conn)
        if orgs is None:
            print("can't list orgs: " + ("not connected; run cardinal-connect" if not conn
                                         else "no control-plane token (Claude Code's cardinal-connect "
                                              "mints one: re-run it with --rotate; other agents: use "
                                              "CARDINAL_ORG_ID + a login token)"), file=sys.stderr)
            sys.exit(EXIT_CONNECT_REJECTED if conn else EXIT_NOT_CONNECTED)
        print(f"Cardinal orgs for {conn.get('user_email', 'this user')}:")
        for n, o in enumerate(orgs, 1):
            mark = "  <- connected" if o["id"] == conn.get("org_id") else ""
            print(f"  {n}. {o['name']} ({o['slug']})  id={o['id']}  role={o['role']}{mark}")
        return
    target_org = os.environ.get("CARDINAL_ORG_ID") or conn.get("org_id")
    # A connect token with telemetry:query reaches every org the user is in, so it
    # counts as a token here: the REST path below works for any target org.
    has_token = (os.environ.get("CARDINAL_TOKEN") or os.environ.get("CARDINAL_API_KEY")
                 or connect_can(conn, ["telemetry:query"]))
    if args.check and not has_token and conn.get("mcp_key") and target_org != conn.get("org_id"):
        print(f"org {target_org} isn't the cardinal-connect org, so its data can only be checked with "
              "the login token: add CARDINAL_TOKEN to .env.cardinal and re-run with --env-file",
              file=sys.stderr)
        sys.exit(EXIT_NEEDS_TOKEN)
    if args.check and not has_token:
        if not conn.get("mcp_key"):
            print("not connected to Cardinal" + (" (connected for telemetry only)" if conn else "")
                  + ": run cardinal-connect" + (" --rotate" if conn else "") + " "
                  + " ".join(MIGRATION_SCOPES), file=sys.stderr)
            sys.exit(EXIT_CONNECT_REJECTED if conn else EXIT_NOT_CONNECTED)
        return check_via_mcp(conn, args.instance)
    c = Cardinal.from_env(connect_scopes=["telemetry:query"])
    if not args.check:
        os.makedirs(args.out, exist_ok=True)

    code, body = c.req("GET", "/api/lakerunner/instances")
    if code != 200:
        sys.exit(f"could not list Cardinal instances ({code}): {body}")
    instances = body.get("instances", [])
    if not instances:
        sys.exit("this Cardinal org has no lakerunner (data lake) instance; connect one before migrating")
    inst = instances[0]
    if args.instance:
        match = [i for i in instances if args.instance in (i["id"], i.get("slug"), i.get("name"))]
        if not match:
            sys.exit(f"instance '{args.instance}' not found; available: {[i.get('slug') for i in instances]}")
        inst = match[0]
    elif len(instances) > 1:
        print(f"note: {len(instances)} instances; using the default '{inst.get('slug')}'. "
              f"Pass --instance to choose: {[i.get('slug') for i in instances]}", file=sys.stderr)
    if not args.check:
        json.dump({"chosen": inst, "all": instances}, open(os.path.join(args.out, "instance.json"), "w"), indent=2)
    prom = f"/api/lakerunner/{inst['id']}/prometheus/api/v1"

    # Metric names come from the Prometheus-compatible metadata route; everything
    # else uses lakerunner's native query API (POST JSON, SSE responses), which is
    # what Cardinal's own UI calls.
    code, body = c.req("GET", f"{prom}/label/__name__/values")
    if code != 200:
        sys.exit(f"could not read Cardinal metric names ({code}): {body}")
    cardinal_metrics = body.get("data", []) if isinstance(body, dict) else []
    if not cardinal_metrics:
        sys.exit("Cardinal returned no metrics for this instance: it isn't receiving data yet")
    if args.check:
        print(f"Cardinal is receiving data: instance '{inst.get('slug') or inst['id']}', "
              f"{len(cardinal_metrics)} metric names, e.g. {', '.join(sorted(cardinal_metrics)[:5])}")
        return
    native = NativeQuery(c, inst["id"])

    by_norm = {}
    for m in cardinal_metrics:
        by_norm.setdefault(norm(m), []).append(m)

    families, used_labels, hist_families = referenced_names(args.export, with_histograms=True)
    mapping = {"metrics": {}, "labels": {}, "log_labels": {}, "drop_labels": [],
               "native_histograms": [], "_review": []}
    for fam in families:
        hit = None
        for cand in metric_candidates(fam):
            if norm(cand) in by_norm:
                hit = by_norm[norm(cand)][0]
                break
        mapping["metrics"][fam] = hit
        if hit is None:
            close = [m for m in cardinal_metrics if norm(fam).split("_")[0] in norm(m)][:8]
            mapping["_review"].append({"metric": fam, "issue": "no Cardinal metric matched",
                                       "similar_in_cardinal": close})
        elif hit != fam:
            mapping["_review"].append({"metric": fam, "mapped_to": hit, "issue": "renamed (check it's the same series)"})
        if fam in hist_families and hit and (hit + "_bucket") not in cardinal_metrics:
            mapping["native_histograms"].append(fam)

    # Labels actually present on the mapped metrics, and on logs.
    cardinal_labels = set()
    for target in {v for v in mapping["metrics"].values() if v}:
        cardinal_labels.update(native.tags("metrics", target))
    log_labels = set(native.tags("logs"))
    label_norm = {}
    for l in sorted(cardinal_labels | log_labels):
        label_norm.setdefault(norm(l), l)
        label_norm.setdefault(norm(re.sub(r"^resource[._]", "", l)), l)
    for l in used_labels:
        if l == "detected_level":
            continue
        target = label_norm.get(norm(l)) or label_norm.get(norm(re.sub(r"^resource[._]", "", l)))
        if target and target != l:
            mapping["labels"][l] = target
    # Filters must match data that exists in Cardinal, or every panel using them goes
    # blank (e.g. an environment label whose value differs between the old and new
    # pipelines). Decided per signal: a label missing from the metrics but present on
    # the logs is removed from metric queries only, so log panels stay scoped.
    by_sig = exprs_by_signal(args.export)
    matchers = {sig: set().union(*map(matchers_in, ex)) if ex else set() for sig, ex in by_sig.items()}
    used = {sig: set().union(*map(labels_in, ex)) if ex else set() for sig, ex in by_sig.items()}
    drops, review = decide_label_drops(
        matchers, used, cardinal_labels, log_labels,
        lambda sig, label: set(native.tag_values(sig, label) or []), mapping["labels"])
    mapping["drop_labels"] = sorted(drops["metrics"])
    mapping["drop_log_labels"] = sorted(drops["logs"])
    mapping["_review"] += review
    # Loki's `detected_level` is a Grafana-side derived field; lakerunner keeps severity on `level`.
    if "detected_level" in used_labels:
        mapping["log_labels"]["detected_level"] = "level"

    json.dump(sorted(cardinal_metrics), open(os.path.join(args.out, "metrics.json"), "w"), indent=1)
    json.dump(sorted(cardinal_labels), open(os.path.join(args.out, "labels.json"), "w"), indent=1)
    json.dump(sorted(log_labels), open(os.path.join(args.out, "log_labels.json"), "w"), indent=1)

    # histogram_quantile on classic histograms (Cardinal keeps the _bucket series): probe it.
    supports_hq = False
    if hist_families and not mapping["native_histograms"]:
        probe = next((mapping["metrics"][f] for f in hist_families if mapping["metrics"].get(f)), None)
        if probe:
            n, err = native.query("metrics", f"histogram_quantile(0.95, sum by (le) (rate({probe}_bucket[5m])))")
            supports_hq = n > 0 and not err
    mapping["supports_histogram_quantile"] = supports_hq
    # Histograms Cardinal keeps under their base name: measure how this instance
    # answers the functions the converter needs instead of assuming.
    native_fams = {f: mapping["metrics"][f] for f in mapping["native_histograms"] if mapping["metrics"].get(f)}
    if native_fams:
        mapping.update(probe_histograms(native, native_fams))
        for fam, ok in sorted(mapping["histogram_quantile_ok"].items()):
            if not ok:
                mapping["_review"].append({"metric": fam, "issue": "histogram_quantile gives no usable answer on this "
                                           "histogram in Cardinal (e.g. negative values): percentile panels will "
                                           "show the average instead, retitled 'avg'"})
        if mapping["histogram_rate_mode"] != "per_second":
            mapping["_review"].append({"issue": f"request rate from histograms: Cardinal answers "
                                       f"'{mapping['histogram_rate_mode']}' (not per second at every step); "
                                       "see the report notes on request-rate panels"})
    any_metric = next((v for v in mapping["metrics"].values() if v), None)
    if any_metric:
        mapping["supports_or_vector"] = probe_or_vector(native, any_metric)
    json.dump(mapping, open(os.path.join(args.out, "mapping.suggested.json"), "w"), indent=2)

    print(json.dumps({
        "instance": inst, "native_histograms": mapping["native_histograms"],
        "drop_labels": mapping["drop_labels"], "drop_log_labels": mapping["drop_log_labels"],
        "cardinal_metrics": len(cardinal_metrics), "grafana_metric_families": len(families),
        "matched": sum(1 for v in mapping["metrics"].values() if v), "unmatched": [k for k, v in mapping["metrics"].items() if not v],
        "renamed": {k: v for k, v in mapping["metrics"].items() if v and v != k},
        "label_renames": mapping["labels"], "supports_histogram_quantile": supports_hq,
        "histogram_quantile_ok": mapping.get("histogram_quantile_ok"),
        "histogram_rate_mode": mapping.get("histogram_rate_mode"),
        "supports_or_vector": mapping.get("supports_or_vector"),
    }, indent=2))


if __name__ == "__main__":
    main()
