#!/usr/bin/env python3
"""Compare migrated panel values against the Grafana panels they came from.

A query that runs and returns data can still be wrong: it can count something else,
or read a different set of series. This runs each Grafana panel query and the
Cardinal query it became over the same 15 minutes at a 60s step, matches their
series by label, and compares the values point by point.

Used by cardinal_verify.py --grafana-env; the comparison logic is pure (compare()).

Verdicts per query:
  match       every shared series agrees within tolerance (median ratio 0.8–1.25)
  differs     a shared series disagrees (shows Cardinal/Grafana as ×ratio)
  missing     Grafana has non-zero data that Cardinal lacks
  extra       Cardinal has non-zero series Grafana doesn't (e.g. a filter was removed)
  zero        Grafana shows 0, Cardinal shows no data (Cardinal ignores `or vector(0)`)
  empty       neither side has data
  estimate    a percentile Cardinal estimates from its sketch: off by up to ×3 (bucket
              resolution) is reported but not a problem; further off is `differs`
  skipped     not compared (log lines, the average shown for a percentile, Grafana error)
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cardinal_catalog import median, series_key  # noqa: E402
from convert import GRAFANA_MACROS, PCT_AS_AVG, PCT_ESTIMATE  # noqa: E402

TOLERANCE = (0.8, 1.25)
# Percentiles: Cardinal estimates from a sketch, Grafana interpolates inside a bucket;
# with exponential buckets the two can be a bucket apart (×2–2.5), not orders of magnitude.
ESTIMATE_TOLERANCE = (1 / 3, 3.0)
PROBLEMS = ("differs", "missing", "extra")
WINDOW_S, STEP_S, SETTLE_S = 15 * 60, 60, 120


def window(now=None):
    """(start, end) unix seconds: 15 minutes ending 2 minutes ago (both sides have
    settled), aligned to the step so both sides' points land on the same timestamps."""
    end = (int(now or time.time()) - SETTLE_S) // STEP_S * STEP_S
    return end - WINDOW_S, end


def grafana_expr(expr):
    """The Grafana query with macros and dashboard variables resolved like the converter
    does ("All" for every variable)."""
    for pat, rep in GRAFANA_MACROS.items():
        expr = re.sub(pat, rep, expr)
    return re.sub(r"\$\{\w+(?::\w+)?\}|\[\[\w+\]\]|\$(?!__)\w+", ".+", expr)


def cardinal_expr(expr):
    return re.sub(r"\$\{?\w+\}?", ".+", expr)


class GrafanaQuery:
    """Range queries through Grafana's /api/ds/query (what its panels call)."""

    def __init__(self, url, token, datasources):
        self.url, self.token, self.ds = url.rstrip("/"), token, datasources

    def resolve_ds(self, ref, kind):
        uid = ref.get("uid") if isinstance(ref, dict) else ref
        if uid and not str(uid).startswith("$") and uid in self.ds:
            return uid
        want = "loki" if kind == "loki" else "prometheus"
        return next((u for u, d in self.ds.items() if d.get("type") == want), None)

    def series(self, expr, ds_uid, start, end):
        """({series key: {unix seconds: value}}, error or None)."""
        q = {"refId": "A", "datasource": {"uid": ds_uid}, "expr": expr, "range": True, "instant": False,
             "intervalMs": STEP_S * 1000, "maxDataPoints": 10000, "queryType": "range"}
        body = json.dumps({"queries": [q], "from": str(start * 1000), "to": str(end * 1000)}).encode()
        req = urllib.request.Request(self.url + "/api/ds/query", body,
                                     {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read()).get("results", {}).get("A", {})
        except urllib.error.HTTPError as e:
            return {}, f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {}, str(e)[:200]
        if res.get("error"):
            return {}, str(res["error"])[:200]
        out = {}
        for fr in res.get("frames", []):
            fields = fr.get("schema", {}).get("fields", [])
            vals = fr.get("data", {}).get("values", [])
            ti = next((i for i, f in enumerate(fields) if f.get("type") == "time"), None)
            for i, f in enumerate(fields):
                if f.get("type") != "number" or ti is None:
                    continue
                pts = out.setdefault(series_key(f.get("labels") or {}), {})
                for t, v in zip(vals[ti], vals[i]):
                    if isinstance(v, (int, float)):
                        pts[int(t) // 1000] = float(v)
        return out, None


def normalize(series, renames):
    """Grafana label names -> Cardinal's (mapping renames, detected_level -> level);
    values compared case-insensitively (Loki's `info` is lakerunner's `INFO`)."""
    out = {}
    for key, pts in series.items():
        out[tuple(sorted((renames.get(k, k), v.lower()) for k, v in key))] = pts
    return out


def project(series, names):
    """Re-key series on just these label names, summing series that collapse."""
    out = {}
    for key, pts in series.items():
        k = tuple((n, v) for n, v in key if n in names)
        acc = out.setdefault(k, {})
        for t, v in pts.items():
            acc[t] = acc.get(t, 0.0) + v
    return out


def _meaningful(pts, eps):
    return any(abs(v) > eps for v in pts.values())


def compare(g, c, renames=None, notes=()):
    """Compare Grafana series g with Cardinal series c ({key: {ts: value}}).
    Returns {"verdict", "detail", "ratio"}."""
    notes = " ".join(notes)
    if PCT_AS_AVG in notes:
        return {"verdict": "skipped", "detail": "shows the average where Grafana shows a percentile", "ratio": None}
    g = normalize(g, renames or {})
    c = normalize(c, {})
    if not g and not c:
        return {"verdict": "empty", "detail": "no data in either", "ratio": None}
    scale = max([abs(v) for s in list(g.values()) + list(c.values()) for v in s.values()] or [0])
    eps = 1e-9 + 1e-6 * scale
    if not c:
        if not any(_meaningful(p, eps) for p in g.values()):
            return {"verdict": "zero", "detail": "Grafana shows 0, Cardinal shows no data", "ratio": None}
        return {"verdict": "missing", "detail": "Grafana has data, Cardinal has none", "ratio": None}
    if not g:
        if not any(_meaningful(p, eps) for p in c.values()):
            return {"verdict": "empty", "detail": "only zeros in Cardinal", "ratio": None}
        return {"verdict": "extra", "detail": "Cardinal has data, Grafana has none", "ratio": None}
    # Match on the label names both sides carry (bare selectors carry different extras).
    gnames = set.intersection(*[{n for n, _ in k} for k in g]) if g else set()
    cnames = set.intersection(*[{n for n, _ in k} for k in c]) if c else set()
    names = gnames & cnames
    g, c = project(g, names), project(c, names)
    ratios, worst = [], None
    for k in set(g) & set(c):
        rs = []
        for t in set(g[k]) & set(c[k]):
            gv, cv = g[k][t], c[k][t]
            if abs(gv) <= eps and abs(cv) <= eps:
                rs.append(1.0)
            elif abs(gv) > eps:
                rs.append(cv / gv)
            else:
                rs.append(float("inf"))
        r = median(rs)
        if r is None:
            continue
        ratios.append(r)
        off = abs(r - 1) if r != float("inf") else float("inf")
        if worst is None or off > worst[1]:
            worst = (k, off, r)
    missing = sorted(k for k in set(g) - set(c) if _meaningful(g[k], eps))
    extra = sorted(k for k in set(c) - set(g) if _meaningful(c[k], eps))
    label = lambda k: ",".join(f"{n}={v}" for n, v in k) or "all"  # noqa: E731
    if worst and not (TOLERANCE[0] <= worst[2] <= TOLERANCE[1]):
        loose = PCT_ESTIMATE in notes and ESTIMATE_TOLERANCE[0] <= worst[2] <= ESTIMATE_TOLERANCE[1]
        verdict = "estimate" if loose else "differs"
        r = worst[2]
        detail = (f"Cardinal is ×{r:.3g} of Grafana" if r != float("inf") else "Grafana is 0 where Cardinal isn't") \
            + (f" ({label(worst[0])})" if worst[0] else "")
        return {"verdict": verdict, "detail": detail, "ratio": r}
    if missing:
        return {"verdict": "missing", "detail": f"series only in Grafana: {', '.join(map(label, missing[:4]))}",
                "ratio": median(ratios)}
    if extra:
        return {"verdict": "extra", "detail": f"series only in Cardinal: {', '.join(map(label, extra[:4]))}",
                "ratio": median(ratios)}
    if not ratios:
        return {"verdict": "missing", "detail": "no shared timestamps or series", "ratio": None}
    return {"verdict": "match", "detail": f"within {TOLERANCE[0]}–{TOLERANCE[1]}", "ratio": median(ratios)}


def grafana_panels(dash):
    """{panel id: panel} including panels inside rows."""
    out, stack = {}, list(dash.get("panels", []))
    while stack:
        p = stack.pop()
        stack.extend(p.get("panels", []))
        if "id" in p:
            out[p["id"]] = p
    return out


def compare_dashboard(uid, plan_dash, report_dash, export_dir, gq, nq, mapping, now=None):
    """Compare every Cardinal query of one dashboard with its Grafana source. Returns rows:
    {panel, verdict, detail, ratio, query, grafana_query}."""
    path = os.path.join(export_dir, "dashboards", f"{uid}.json")
    if not os.path.exists(path):
        return [{"panel": "(dashboard)", "verdict": "skipped", "detail": f"no Grafana export at {path}"}]
    gpanels = grafana_panels(json.load(open(path)))
    renames = {**mapping.get("labels", {}), **mapping.get("log_labels", {})}
    start, end = window(now)
    rng = {"s": str(start * 1000), "e": str(end * 1000)}
    rows = []
    for entry in report_dash.get("panels", []):
        cid = entry.get("cardinal_id")
        cp = plan_dash["spec"]["panels"].get(cid) if cid else None
        gp = gpanels.get(entry.get("grafana_id"))
        if not cp or not gp:
            continue
        if cp.get("kind") == "log-events":
            rows.append({"panel": cp["title"], "verdict": "skipped", "detail": "log lines are not compared"})
            continue
        targets = {t.get("refId"): t for t in gp.get("targets", [])}
        for ref, cq in zip(entry.get("query_refs", []), cp.get("queries", [])):
            t = targets.get(ref)
            if not t or not t.get("expr"):
                continue
            kind = cq.get("queryKind", "prometheus")
            ds = gq.resolve_ds(t.get("datasource") or gp.get("datasource"), kind)
            g, gerr = gq.series(grafana_expr(t["expr"]), ds, start, end)
            row = {"panel": cp["title"], "query": cq["query"], "grafana_query": t["expr"]}
            if gerr:
                rows.append(dict(row, verdict="skipped", detail=f"Grafana query failed: {gerr}", ratio=None))
                continue
            c, cerr = nq.series("logs" if kind == "loki" else "metrics", cardinal_expr(cq["query"]), STEP_S, rng)
            if cerr:
                rows.append(dict(row, verdict="differs", detail=f"Cardinal query failed: {cerr}", ratio=None))
                continue
            rows.append(dict(row, **compare(g, c, renames, entry.get("notes", []))))
    return rows


def compare_alert(alert, report_alert, gq, nq, mapping, now=None):
    """Compare a migrated alert rule's query with the Grafana rule's query over the same
    window, so a rule that reads other series (a widened filter) is caught: a wider
    query returns *more* data, which a plain "has data" check would pass. Returns a row
    like compare_dashboard's, or None when the plan predates recording the Grafana query."""
    src, spec = alert.get("source") or {}, alert["rule_spec"]
    if not src.get("grafana_expr"):
        return None
    kind = "loki" if spec["signal_type"] == "logs" else "prometheus"
    start, end = window(now)
    row = {"panel": "rule query", "query": spec["query"]["expr"], "grafana_query": src["grafana_expr"]}
    g, gerr = gq.series(grafana_expr(src["grafana_expr"]), gq.resolve_ds(src.get("datasource_uid"), kind), start, end)
    if gerr:
        return dict(row, verdict="skipped", detail=f"Grafana query failed: {gerr}", ratio=None)
    c, cerr = nq.series(spec["signal_type"], cardinal_expr(spec["query"]["expr"]), STEP_S,
                        {"s": str(start * 1000), "e": str(end * 1000)})
    if cerr:
        return dict(row, verdict="differs", detail=f"Cardinal query failed: {cerr}", ratio=None)
    renames = {**mapping.get("labels", {}), **mapping.get("log_labels", {})}
    return dict(row, **compare(g, c, renames, (report_alert or {}).get("notes", [])))
