#!/usr/bin/env python3
"""Convert an exported Grafana bundle into Cardinal (Maestro) dashboards and alert rules.

Pure/offline: reads the export dir + a mapping file, writes a plan dir. Nothing
is sent anywhere. Run cardinal_apply.py afterwards to create the objects.

Usage:
  convert.py --export ./export --mapping ./mapping.json --out ./plan
             [--no-histogram-quantile] [--default-range 5m]

mapping.json (built by the skill workflow from cardinal_catalog.py output):
  {
    "metrics": {"<grafana metric name>": "<cardinal metric name>" | null, ...},
    "labels":  {"<grafana label>": "<cardinal label>", ...},          # metrics labels
    "log_labels": {"detected_level": "level", ...},                     # LogQL labels
    "supports_histogram_quantile": true|false,
    "native_histograms": ["<grafana histogram family>", ...],   # Cardinal has no _bucket/_sum/_count
    "drop_labels": ["<grafana label not present in Cardinal>", ...] # matchers on these are removed
  }
  A metric mapped to null means "not in Cardinal" -> panels/rules using it are skipped.
  Metrics missing from the mapping are passed through unchanged (and reported).

Writes:
  plan/dashboards/<uid>.json   {"name": ..., "spec": <Cardinal Dashboard spec>}
  plan/alerts.json             [{"name":..., "rule_spec": {...}, "source": {...}}]
  plan/report.json             every panel/rule: migrated | adapted | skipped, with notes
"""
import argparse
import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# Query translation
# ---------------------------------------------------------------------------

GRAFANA_MACROS = {
    r"\$__rate_interval": "5m",
    r"\$\{__rate_interval\}": "5m",
    r"\$__interval": "1m",
    r"\$\{__interval\}": "1m",
    r"\$__range": "1h",
    r"\$\{__range\}": "1h",
    r"\$__auto": "1m",
}

# metric-name tokens: identifiers not followed by "(" (functions) and not inside {...} / "..."
IDENT = re.compile(r'(?<![\w:."$])([a-zA-Z_:][a-zA-Z0-9_:]*)(?![\w(])')
PROMQL_WORDS = {
    "by", "without", "on", "ignoring", "group_left", "group_right", "and", "or", "unless",
    "offset", "bool", "inf", "nan", "le", "sum", "avg", "min", "max", "count", "stddev", "stdvar",
    "topk", "bottomk", "quantile", "count_values", "group",
}


def split_outside(s, opener, closer):
    """Yield (segment, inside) splitting s on bracket pairs, respecting quotes."""
    out, buf, depth, quote = [], "", 0, None
    for ch in s:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'`":
            quote = ch
            buf += ch
            continue
        if ch == opener:
            if depth == 0:
                out.append((buf, False)); buf = ""
            depth += 1
            buf += ch
            continue
        if ch == closer and depth:
            depth -= 1
            buf += ch
            if depth == 0:
                out.append((buf, True)); buf = ""
            continue
        buf += ch
    out.append((buf, depth > 0))
    return out


def rename_labels_in_matcher(block, labels):
    # block is "{a="x", b=~"y"}" ; rename label keys only
    def sub(m):
        return labels.get(m.group(1), m.group(1)) + m.group(2)
    return re.sub(r'([a-zA-Z_][a-zA-Z0-9_.]*)(\s*(?:=~|!~|!=|=))', sub, block)


def rename_labels_in_grouping(text, labels):
    def sub(m):
        inner = ", ".join(labels.get(x.strip(), x.strip()) for x in m.group(2).split(",") if x.strip())
        return f"{m.group(1)}({inner})"
    return re.sub(r'\b(by|without|on|ignoring|group_left|group_right)\s*\(([^)]*)\)', sub, text)


def drop_matchers(q, labels):
    """Remove `label op "value"` matchers for the given labels from {...} selectors
    (and LogQL `| label op "value"` stages). Returns (query, dropped_labels)."""
    dropped = set()
    lab = "|".join(re.escape(l) for l in labels)
    def clean_block(m):
        inner = m.group(1)
        parts = [p for p in re.findall(r'\s*([^,]+?"(?:[^"\\]|\\.)*"|[^,]+)\s*(?:,|$)', inner) if p.strip()]
        keep = []
        for p in parts:
            if re.match(r"\s*(" + lab + r")\s*(=~|!~|!=|=)", p):
                dropped.add(re.match(r"\s*([\w.]+)", p).group(1))
            else:
                keep.append(p.strip())
        return "{" + ", ".join(keep) + "}"
    q = re.sub(r"\{([^{}]*)\}", clean_block, q)
    def pipe(m):
        dropped.add(m.group(1))
        return ""
    q = re.sub(r"\|\s*(" + lab + r')\s*(?:=~|!~|!=|=)\s*"(?:[^"\\]|\\.)*"', pipe, q)
    q = re.sub(r"(\w)\{\}", r"\1", q)  # metric{} -> metric
    return q, dropped


class Translator:
    def __init__(self, mapping):
        self.metrics = mapping.get("metrics", {})
        self.labels = mapping.get("labels", {})
        self.log_labels = mapping.get("log_labels", {})
        self.hq = mapping.get("supports_histogram_quantile", True)
        # "native": Cardinal stores a histogram under its base name only (no
        # _bucket/_sum/_count series); rate(M) is the request rate and
        # max by (...) (M) is the latency value Cardinal's own dashboards use.
        self.native_hist = set(mapping.get("native_histograms", []))
        self.drop_labels = set(mapping.get("drop_labels", []))
        self.const_vars = {}

    def resolve_metric(self, name):
        """Map a Grafana metric name (incl. histogram _bucket/_sum/_count) to Cardinal.
        Returns (new_name, known) where new_name is None if mapped to "not in Cardinal"."""
        if name in self.metrics:
            return self.metrics[name], True
        for sfx in ("_bucket", "_sum", "_count"):
            if name.endswith(sfx) and name[: -len(sfx)] in self.metrics:
                target = self.metrics[name[: -len(sfx)]]
                return (target + sfx if target else None), True
        return name, False

    # --- PromQL ---------------------------------------------------------
    def promql(self, expr):
        """Return (new_expr | None, notes[]). None means unusable -> skip."""
        notes = []
        q = expr
        for pat, rep in GRAFANA_MACROS.items():
            if re.search(pat, q):
                q = re.sub(pat, rep, q)
                notes.append(f"Grafana macro replaced with fixed window ({rep})")
        for name, value in self.const_vars.items():
            if re.search(r"\$\{?" + re.escape(name) + r"\b\}?", q):
                q = re.sub(r"\$\{" + re.escape(name) + r"\}|\$" + re.escape(name) + r"\b", value, q)
                notes.append(f"variable ${name} inlined as '{value}'")

        if self.drop_labels:
            q, dropped = drop_matchers(q, self.drop_labels)
            if dropped:
                notes.append(f"filter on label(s) not present in Cardinal removed: {', '.join(sorted(dropped))}")
        if self.native_hist:
            q, hnotes, ok = self._rewrite_native_histograms(q)
            notes += hnotes
            if not ok:
                return None, notes
        if not self.hq and "histogram_quantile" in q:
            new = self._rewrite_histogram_quantile(q)
            if new is None:
                return None, notes + ["histogram_quantile is not supported by this Cardinal instance and could not be rewritten"]
            q = new
            notes.append("histogram_quantile not supported in Cardinal: replaced with average (sum/count)")

        missing, unknown = [], []
        pieces = []
        for seg, inside in split_outside(q, "{", "}"):
            if inside:
                pieces.append(rename_labels_in_matcher(seg, self.labels))
                continue
            # rename metric identifiers in the non-matcher part (outside quotes/brackets [..])
            def sub(m):
                name = m.group(1)
                if name in PROMQL_WORDS or re.fullmatch(r"\d+[smhdwy]?", name):
                    return name
                target, known = self.resolve_metric(name)
                if known and target is None:
                    missing.append(name)
                    return name
                if not known and re.fullmatch(r"[a-z][a-z0-9_:]*", name) and ("_" in name or ":" in name):
                    unknown.append(name)
                return target
            # Protect range selectors and grouping clauses (label lists) from
            # metric renaming; grouping labels are renamed separately.
            groups = []
            def hold(m):
                groups.append(rename_labels_in_grouping(m.group(0), self.labels))
                return f"\2{len(groups) - 1}\3"
            seg = re.sub(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", hold, seg)
            seg = re.sub(r"\[[^\]]*\]", lambda m: "\0" + m.group(0)[1:-1] + "\1", seg)
            seg = IDENT.sub(sub, seg)
            seg = seg.replace("\0", "[").replace("\1", "]")
            seg = re.sub("\x02(\\d+)\x03", lambda m: groups[int(m.group(1))], seg)
            pieces.append(seg)
        q = "".join(pieces)
        if missing:
            return None, notes + [f"metric not found in Cardinal: {', '.join(sorted(set(missing)))}"]
        if unknown:
            notes.append(f"metric passed through unmapped (verify it exists): {', '.join(sorted(set(unknown)))}")
        return q, notes

    def _rewrite_native_histograms(self, q):
        """Rewrite Prometheus classic-histogram idioms for Cardinal native histograms."""
        notes, fams = [], "|".join(sorted((re.escape(f) for f in self.native_hist), key=len, reverse=True))
        if not fams:
            return q, notes, True
        sel = r"(\{[^}]*\})?"
        # histogram_quantile(φ, sum by (le, X) (rate(M_bucket{sel}[w])))  ->  max by (X) (M{sel})
        pat_hq = re.compile(r"histogram_quantile\(\s*([\d.]+)\s*,\s*sum\s*(?:by)?\s*\(([^)]*)\)\s*\(\s*(?:rate|irate|increase)\(\s*("
                            + fams + r")_bucket" + sel + r"\s*\[[^\]]+\]\s*\)\s*\)\s*\)")
        def hq(m):
            by = [g.strip() for g in m.group(2).split(",") if g.strip() and g.strip() != "le"]
            notes.append(f"p{round(float(m.group(1)) * 100)} (histogram_quantile) not available in Cardinal: "
                         f"uses the histogram's own value (max by ...), like Cardinal's built-in dashboards")
            return f"max{' by (' + ', '.join(by) + ')' if by else ''} ({m.group(3)}{m.group(4) or ''})"
        q = pat_hq.sub(hq, q)
        # sum by (X)(rate(M_sum[w])) / sum by (X)(rate(M_count[w]))  (average)  ->  max by (X) (M{sel})
        pat_avg = re.compile(r"\(?\s*sum\s*(?:by\s*\(([^)]*)\))?\s*\(\s*(?:rate|irate|increase)\(\s*(" + fams + r")_sum" + sel
                             + r"\s*\[[^\]]+\]\s*\)\s*\)\s*/\s*sum\s*(?:by\s*\([^)]*\))?\s*\(\s*(?:rate|irate|increase)\(\s*\2_count"
                             + sel + r"\s*\[[^\]]+\]\s*\)\s*\)\s*\)?")
        def avg(m):
            notes.append("average (sum/count) rewritten to the histogram's own value (max by ...)")
            return f"max{' by (' + m.group(1).strip() + ')' if m.group(1) else ''} ({m.group(2)}{m.group(3) or ''})"
        q = pat_avg.sub(avg, q)
        # M_count -> M (rate()/increase() of a Cardinal histogram counts observations)
        q2 = re.sub(r"\b(" + fams + r")_count\b", r"\1", q)
        if q2 != q:
            notes.append("histogram _count series replaced by the histogram itself (rate() counts requests in Cardinal)")
            q = q2
        left = re.findall(r"\b(?:" + fams + r")_(bucket|sum)\b", q)
        if left:
            return q, notes + [f"uses histogram _{left[0]} series, which Cardinal doesn't expose"], False
        return q, notes, True

    @staticmethod
    def _rewrite_histogram_quantile(q):
        # histogram_quantile(φ, sum by (le, X) (rate(M_bucket{sel}[w])))  ->
        # sum by (X) (rate(M_sum{sel}[w])) / sum by (X) (rate(M_count{sel}[w]))
        m = re.search(
            r"histogram_quantile\(\s*[\d.]+\s*,\s*sum\s*(?:by|without)?\s*\(([^)]*)\)\s*\(\s*(rate|increase|irate)\(\s*"
            r"([a-zA-Z_:][\w:]*)_bucket(\{[^}]*\})?\s*\[([^\]]+)\]\s*\)\s*\)\s*\)", q)
        if not m:
            return None
        grouping = [g.strip() for g in m.group(1).split(",") if g.strip() and g.strip() != "le"]
        by = f" by ({', '.join(grouping)})" if grouping else ""
        sel, win, fn, base = m.group(4) or "", m.group(5), m.group(2), m.group(3)
        repl = f"(sum{by} ({fn}({base}_sum{sel}[{win}])) / sum{by} ({fn}({base}_count{sel}[{win}])))"
        return q[: m.start()] + repl + q[m.end():]

    # --- LogQL ----------------------------------------------------------
    def logql(self, expr):
        notes = []
        q = expr
        for pat, rep in GRAFANA_MACROS.items():
            if re.search(pat, q):
                q = re.sub(pat, rep, q)
                notes.append(f"Grafana macro replaced with fixed window ({rep})")
        for name, value in self.const_vars.items():
            q, n = re.subn(r"\$\{" + re.escape(name) + r"\}|\$" + re.escape(name) + r"\b", value, q)
            if n:
                notes.append(f"variable ${name} inlined as '{value}'")
        # lakerunner's LogQL engine rejects set operators against scalars
        # (`... or vector(0)`); drop the fallback, the panel just shows no data instead of 0.
        q2 = re.sub(r"\s+or\s+vector\(\s*[\d.]+\s*\)\s*$", "", q)
        if q2 != q:
            notes.append("'or vector(0)' fallback removed (not supported on log queries in Cardinal)")
            q = q2
        if self.drop_labels:
            q, dropped = drop_matchers(q, self.drop_labels)
            if dropped:
                notes.append(f"filter on label(s) not present in Cardinal removed: {', '.join(sorted(dropped))}")
            # A LogQL stream selector can't be empty; match every service instead.
            q = re.sub(r"\{\s*\}", '{service_name=~".+"}', q)
        before = q
        for src, dst in self.log_labels.items():
            q = re.sub(r"(?<![\w.])" + re.escape(src) + r"(?=\s*(=~|!~|!=|=|\)|,))", dst, q)
        for src, dst in self.labels.items():
            if src not in self.log_labels:
                q = re.sub(r"(?<=[{,\s(])" + re.escape(src) + r"(?=\s*(=~|!~|!=|=))", dst, q)
        if q != before:
            notes.append("log labels renamed for Cardinal")
        return q, notes


# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------

UNIT_MAP = {
    "s": "s", "ms": "ms", "µs": "µs", "us": "µs", "ns": "ns", "m": "min", "h": "h",
    "bytes": "bytes", "decbytes": "bytes", "bits": "bits", "kbytes": "KiB", "mbytes": "MiB",
    "Bps": "B/s", "binBps": "B/s", "bps": "bit/s",
    "reqps": "req/s", "ops": "ops/s", "rps": "req/s", "wps": "writes/s", "iops": "io/s",
    "percent": "%", "percentunit": "ratio", "short": "", "none": "", "": "",
}
CALC_MAP = {"lastNotNull": "last", "last": "last", "mean": "mean", "avg": "mean", "max": "max",
            "min": "min", "sum": "sum", "total": "sum", "firstNotNull": "last", "first": "last"}


def ds_type(ds, datasources, fallback=None):
    if isinstance(ds, dict):
        if ds.get("type"):
            return ds["type"]
        uid = ds.get("uid")
        if uid in datasources:
            return datasources[uid]["type"]
    if isinstance(ds, str):
        for uid, d in datasources.items():
            if ds in (uid, d["name"]):
                return d["type"]
    return fallback


def guess_kind_from_query(expr):
    s = expr.strip()
    if re.match(r"^(sum|count|rate|avg|max|min|topk|bottomk)?\s*(by\s*\([^)]*\))?\s*\(?\s*(count_over_time|rate|bytes_over_time|sum_over_time)?\s*\(?\s*\{", s) and ("|" in s or "_over_time" in s):
        return "loki"
    if s.startswith("{") and ("|=" in s or "| " in s or s.endswith("}")):
        return "loki"
    return "prometheus"


def convert_variables(dash, tr, datasources):
    variables, notes = [], []
    for v in dash.get("templating", {}).get("list", []):
        name, vtype = v.get("name"), v.get("type")
        if vtype == "query":
            query = v.get("query")
            query = query.get("query") if isinstance(query, dict) else query
            m = re.match(r"\s*label_values\(\s*(?:([a-zA-Z_:][\w:]*)\s*(\{[^}]*\})?\s*,\s*)?([\w.]+)\s*\)\s*$", query or "")
            vt = ds_type(v.get("datasource"), datasources, "prometheus")
            if m and vt == "prometheus" and m.group(1):
                metric = tr.resolve_metric(m.group(1))[0] or m.group(1)
                for fam in tr.native_hist:
                    target = tr.metrics.get(fam) or fam
                    if metric in (target + "_count", target + "_sum", target + "_bucket"):
                        metric = target
                label = tr.labels.get(m.group(3), m.group(3))
                src = {"signal": "metrics", "metric": metric, "label": label}
                if m.group(2):
                    scope_sel = drop_matchers(m.group(2), tr.drop_labels)[0] if tr.drop_labels else m.group(2)
                    scope = rename_labels_in_matcher(scope_sel, tr.labels).strip("{} ") if scope_sel.startswith("{") else ""
                    if scope:
                        src["scope"] = scope
                variables.append({"name": name, "label": v.get("label") or name, "kind": "query", "source": src,
                                  "multi": bool(v.get("multi")), "includeAll": bool(v.get("includeAll"))})
                continue
            if m and vt == "loki":
                label = tr.log_labels.get(m.group(3), tr.labels.get(m.group(3), m.group(3)))
                variables.append({"name": name, "label": v.get("label") or name, "kind": "query",
                                  "source": {"signal": "logs", "label": label},
                                  "multi": bool(v.get("multi")), "includeAll": bool(v.get("includeAll"))})
                continue
        # custom / constant / interval / textbox / unsupported query -> inline current value
        cur = v.get("current", {}) or {}
        val = cur.get("value")
        if isinstance(val, list):
            val = "|".join(x for x in val if x != "$__all") or ".+"
        if val in (None, "", "$__all"):
            val = ".+" if v.get("includeAll") else (v.get("query") or "")
        tr.const_vars[name] = str(val)
        notes.append(f"variable ${name} ({vtype}) has no Cardinal equivalent; inlined as '{val}'")
    return variables, notes


def convert_panel(p, tr, datasources, pid):
    """Return (cardinal_panel | None, status, notes)."""
    gtype = p.get("type")
    title = p.get("title") or "Untitled"
    notes = []
    panel_ds = ds_type(p.get("datasource"), datasources)

    if gtype in ("text", "news", "dashlist", "alertlist", "annolist", "welcome", "gettingstarted"):
        return None, "skipped", [f"'{gtype}' panels have no Cardinal equivalent"]
    if gtype in ("traces", "nodeGraph", "flamegraph") or panel_ds in ("tempo", "jaeger", "zipkin", "grafana-pyroscope-datasource"):
        return None, "skipped", ["trace/profile panels are not supported in Cardinal dashboards; use Cardinal's trace explorer"]

    queries, logs_exprs = [], []
    for t in p.get("targets", []):
        if t.get("hide"):
            continue
        tds = ds_type(t.get("datasource"), datasources, panel_ds)
        expr = t.get("expr") or t.get("query") or ""
        if not expr or tds in ("tempo", "jaeger", "zipkin"):
            if tds in ("tempo", "jaeger", "zipkin"):
                notes.append("trace query dropped")
            continue
        kind = "loki" if tds == "loki" else "prometheus" if tds in ("prometheus", "grafana-amazonprometheus-datasource") else guess_kind_from_query(expr)
        if tds and tds not in ("loki", "prometheus", "grafana-amazonprometheus-datasource", "-- Mixed --", "datasource"):
            notes.append(f"query on unsupported datasource '{tds}' dropped")
            continue
        if kind == "loki":
            new, n = tr.logql(expr)
        else:
            new, n = tr.promql(expr)
        notes += n
        if new is None:
            continue
        if t.get("format") == "heatmap":
            notes.append("heatmap-format query kept as a plain series")
        q = {"query": new, "queryKind": kind}
        legend = t.get("legendFormat")
        if legend and legend != "__auto":
            renames = {**tr.labels, **tr.log_labels} if kind == "loki" else tr.labels
            q["name"] = re.sub(r"\{\{\s*([\w.]+)\s*\}\}", lambda m: "{{" + renames.get(m.group(1), m.group(1)) + "}}", legend)
        if kind == "loki" and not re.search(r"(_over_time|rate)\s*\(", new):
            logs_exprs.append(new)
        else:
            queries.append(q)

    fc = (p.get("fieldConfig") or {}).get("defaults", {}) or {}
    unit = UNIT_MAP.get(fc.get("unit", ""), fc.get("unit", ""))
    base = {"id": pid, "title": title}
    if p.get("description"):
        base["description"] = p["description"]

    if gtype == "logs" or (logs_exprs and not queries):
        if not logs_exprs:
            return None, "skipped", notes + ["no usable log query"]
        panel = dict(base, kind="log-events", queries=[], rawLogql=logs_exprs[0], limit=100)
        if len(logs_exprs) > 1:
            notes.append("only the first log query was kept")
        return panel, ("adapted" if notes else "migrated"), notes

    if not queries:
        return None, "skipped", notes + ["no query could be migrated"]

    custom = fc.get("custom", {}) or {}
    calc = CALC_MAP.get(((p.get("options") or {}).get("reduceOptions") or {}).get("calcs", ["lastNotNull"])[0:1][0] if ((p.get("options") or {}).get("reduceOptions") or {}).get("calcs") else "lastNotNull", "last")

    if gtype in ("timeseries", "graph", "trend", "heatmap", "state-timeline", "status-history", "xychart"):
        panel = dict(base, kind="timeseries", queries=queries)
        if unit:
            panel["unit"] = unit
        stacking = (custom.get("stacking") or {}).get("mode")
        draw = custom.get("drawStyle")
        if gtype == "graph" and p.get("stack"):
            stacking = "normal"
        if stacking == "percent":
            panel["variant"] = "normalized-bar" if draw == "bars" else "stacked-area"
        elif stacking == "normal":
            panel["variant"] = "stacked-bar" if draw == "bars" else "stacked-area"
        elif draw == "bars" or (gtype == "graph" and p.get("bars")):
            panel["variant"] = "bar"
        if isinstance(fc.get("min"), (int, float)):
            panel["yMin"] = fc["min"]
        if isinstance(fc.get("max"), (int, float)):
            panel["yMax"] = fc["max"]
        if gtype != "timeseries" and gtype != "graph":
            notes.append(f"'{gtype}' shown as a time series chart")
    elif gtype in ("stat", "singlestat", "gauge", "bargauge"):
        panel = dict(base, kind="stat", queries=queries[:1], calculation=calc)
        fmt = {}
        if unit:
            fmt["unit"] = unit
        if isinstance(fc.get("decimals"), int):
            fmt["decimalPlaces"] = fc["decimals"]
        if fmt:
            panel["format"] = fmt
        if (p.get("options") or {}).get("graphMode", "area") != "none":
            panel["sparkline"] = True
        if len(queries) > 1:
            notes.append("stat panel had several queries; only the first was kept")
        if gtype in ("gauge", "bargauge"):
            notes.append(f"'{gtype}' shown as a stat")
        if (fc.get("thresholds") or {}).get("steps", [])[1:]:
            notes.append("threshold colours are not carried over")
    elif gtype == "piechart":
        panel = dict(base, kind="pie", queries=queries, reducer=calc)
    elif gtype == "barchart":
        panel = dict(base, kind="bar", queries=queries, reducer=calc)
        if unit:
            panel["unit"] = unit
    elif gtype == "table":
        panel = dict(base, kind="label", queries=queries, reducer=calc, sort="value-desc")
        if unit:
            panel["format"] = {"unit": unit}
        notes.append("table shown as a label/value list")
    else:
        panel = dict(base, kind="timeseries", queries=queries)
        notes.append(f"unknown panel type '{gtype}' shown as a time series chart")

    return panel, ("adapted" if notes else "migrated"), notes


def flatten_sections(dash):
    """Yield (section_title, [panels]) from Grafana's flat-with-rows layout."""
    sections, current = [], {"title": dash.get("title", "Dashboard"), "row_y": -1, "panels": []}
    for p in sorted(dash.get("panels", []), key=lambda x: (x.get("gridPos", {}).get("y", 0), x.get("gridPos", {}).get("x", 0))):
        if p.get("type") == "row":
            if current["panels"]:
                sections.append(current)
            current = {"title": p.get("title") or "Row", "row_y": p.get("gridPos", {}).get("y", 0),
                       "panels": list(p.get("panels", [])), "collapsed": bool(p.get("collapsed"))}
            continue
        current["panels"].append(p)
    if current["panels"]:
        sections.append(current)
    if len(sections) == 1 and sections[0]["row_y"] == -1:
        sections[0]["title"] = "Panels"
    return sections


def convert_dashboard(dash, tr, datasources):
    tr.const_vars = {}
    variables, var_notes = convert_variables(dash, tr, datasources)
    report = {"uid": dash.get("uid"), "title": dash.get("title"), "variables": var_notes, "panels": []}
    panels, sections = {}, []
    n = 0
    for sec in flatten_sections(dash):
        cells = []
        for p in sec["panels"]:
            n += 1
            pid = f"p{n}"
            cp, status, notes = convert_panel(p, tr, datasources, pid)
            report["panels"].append({"title": p.get("title"), "grafana_type": p.get("type"),
                                     "status": status, "notes": sorted(set(notes))})
            if not cp:
                continue
            gp = p.get("gridPos", {}) or {}
            y = max(0, gp.get("y", 0) - (sec["row_y"] + 1 if sec["row_y"] >= 0 else 0))
            cells.append({"i": pid, "x": gp.get("x", 0), "y": y, "w": gp.get("w", 12), "h": max(3, gp.get("h", 8))})
            panels[pid] = cp
        if cells:
            s = {"title": sec["title"], "cells": cells}
            if sec.get("collapsed"):
                s["defaultCollapsed"] = True
            sections.append(s)
    frm = (dash.get("time") or {}).get("from", "now-1h")
    m = re.match(r"now-(\d+[smhdwy])$", frm or "")
    spec = {"schemaVersion": 2, "duration": m.group(1) if m else "1h", "panels": panels, "sections": sections}
    if variables:
        spec["variables"] = variables
    spec["metadata"] = {"migratedFrom": {"tool": "migrate-from-grafana", "grafanaUid": dash.get("uid"),
                                         "grafanaTitle": dash.get("title")}}
    return {"name": dash.get("title") or dash.get("uid"), "spec": spec}, report


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

OPS = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<=", "eq": "==", "ne": "!=", "neq": "!="}
REDUCERS = {"last": "last", "mean": "avg", "avg": "avg", "min": "min", "max": "max", "sum": "sum", "count": "sum"}


def dur_seconds(s, default=0):
    if s in (None, ""):
        return default
    if isinstance(s, (int, float)):
        return int(s)
    total = 0
    for num, unit in re.findall(r"(\d+)([smhdw])", s):
        total += int(num) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return total or default


def strip_go_templates(text):
    # Grafana annotations use Go templates ({{ $labels.x }}, {{ $values.B }}); Cardinal
    # annotations are plain text, so turn label refs into readable placeholders.
    text = re.sub(r"\{\{\s*\$labels\.([\w.]+)\s*\}\}", r"<\1>", text or "")
    text = re.sub(r"\{\{\s*\$values?\.[\w.]+(\.Value)?\s*\}\}", "<value>", text)
    return re.sub(r"\{\{[^}]*\}\}", "", text).strip()


def convert_rule(rule, group, tr, datasources):
    ga = rule.get("grafana_alert", {})
    title = ga.get("title") or "Untitled rule"
    notes, data = [], {d["refId"]: d for d in ga.get("data", [])}
    report = {"title": title, "folder": group["folder"], "group": group["group"]}

    queries = [d for d in data.values() if d.get("datasourceUid") not in ("__expr__", "-100")]
    exprs = {d["refId"]: d for d in data.values() if d.get("datasourceUid") in ("__expr__", "-100")}
    if len(queries) != 1:
        return None, dict(report, status="skipped", notes=[f"rule uses {len(queries)} data queries; only single-query rules can be migrated"])
    qd = queries[0]
    model = qd.get("model", {})
    qtype = ds_type({"uid": qd.get("datasourceUid")}, datasources) or guess_kind_from_query(model.get("expr", ""))
    signal = "logs" if qtype == "loki" else "metrics"
    expr = model.get("expr") or ""
    if not expr:
        return None, dict(report, status="skipped", notes=["data query has no expression"])
    new, n = (tr.logql(expr) if signal == "logs" else tr.promql(expr))
    notes += n
    if new is None:
        return None, dict(report, status="skipped", notes=sorted(set(notes)))

    # Find the condition chain: threshold / classic_conditions / math over reduce.
    cond = exprs.get(ga.get("condition"))
    reducer, op, threshold = "last", None, None
    if cond is None:
        return None, dict(report, status="skipped", notes=notes + ["condition expression not found"])
    ctype = cond.get("model", {}).get("type")
    if ctype == "threshold":
        ev = cond["model"]["conditions"][0]["evaluator"]
        op, threshold = OPS.get(ev.get("type")), (ev.get("params") or [None])[0]
        if ev.get("type") in ("within_range", "outside_range"):
            return None, dict(report, status="skipped", notes=notes + [f"'{ev['type']}' thresholds are not supported"])
        src = exprs.get(cond["model"].get("expression"))
        if src and src.get("model", {}).get("type") == "reduce":
            reducer = REDUCERS.get(src["model"].get("reducer", "last"), "last")
            if src["model"].get("reducer") not in REDUCERS:
                notes.append(f"reducer '{src['model'].get('reducer')}' mapped to 'last'")
            if src["model"].get("reducer") == "count":
                notes.append("reducer 'count' mapped to 'sum' (verify)")
    elif ctype == "classic_conditions":
        c = cond["model"]["conditions"]
        if len(c) != 1:
            return None, dict(report, status="skipped", notes=notes + ["multi-condition classic rules are not supported"])
        ev = c[0]["evaluator"]
        op, threshold = OPS.get(ev.get("type")), (ev.get("params") or [None])[0]
        reducer = REDUCERS.get(c[0].get("reducer", {}).get("type", "last"), "last")
    elif ctype == "math":
        m = re.match(r"^\s*\$\{?(\w+)\}?\s*(>=|<=|==|!=|>|<)\s*(-?[\d.eE+]+)\s*$", cond["model"].get("expression", ""))
        if not m:
            return None, dict(report, status="skipped", notes=notes + [f"math condition '{cond['model'].get('expression')}' too complex to migrate"])
        op, threshold = m.group(2), float(m.group(3))
        src = exprs.get(m.group(1))
        if src and src.get("model", {}).get("type") == "reduce":
            reducer = REDUCERS.get(src["model"].get("reducer", "last"), "last")
    else:
        return None, dict(report, status="skipped", notes=notes + [f"condition type '{ctype}' is not supported"])
    if op is None or threshold is None:
        return None, dict(report, status="skipped", notes=notes + ["could not read operator/threshold"])

    rng = (qd.get("relativeTimeRange") or {}).get("from", 300) or 300
    interval = dur_seconds(group.get("interval"), 60)
    ann = rule.get("annotations") or {}
    annotations = {k: strip_go_templates(v) for k, v in ann.items() if strip_go_templates(v)}
    if any("{{" in (v or "") for v in ann.values()):
        notes.append("annotation templates converted to plain text")
    labels = dict(rule.get("labels") or {})
    rule_spec = {
        "name": title,
        "signal_type": signal,
        "query": {"expr": new, "range_seconds": int(rng)},
        "detection": {"kind": "static_threshold", "reducer": reducer, "operator": op, "threshold": float(threshold)},
        "eval_interval_seconds": max(15, interval),
        "for_duration_seconds": dur_seconds(rule.get("for"), 0),
    }
    if signal == "metrics":
        rule_spec["query"]["step_seconds"] = 60
    if labels:
        rule_spec["labels"] = labels
    if annotations:
        rule_spec["annotations"] = annotations
    if annotations.get("summary") or annotations.get("description"):
        rule_spec["description"] = annotations.get("description") or annotations.get("summary")
    nd = ga.get("no_data_state")
    if nd and nd not in ("OK", "NoData"):
        notes.append(f"no-data behaviour '{nd}' is not carried over (Cardinal does not fire on missing data)")
    if ga.get("is_paused"):
        notes.append("rule was paused in Grafana")
    status = "adapted" if notes else "migrated"
    return {"name": title, "rule_spec": rule_spec, "paused": bool(ga.get("is_paused")),
            "source": {"grafana_uid": ga.get("uid"), "folder": group["folder"], "group": group["group"]}}, \
        dict(report, status=status, notes=sorted(set(notes)))


# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", required=True)
    ap.add_argument("--mapping", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    mapping = json.load(open(args.mapping))
    datasources = json.load(open(os.path.join(args.export, "datasources.json")))
    tr = Translator(mapping)
    os.makedirs(os.path.join(args.out, "dashboards"), exist_ok=True)

    report = {"dashboards": [], "alerts": []}
    ddir = os.path.join(args.export, "dashboards")
    for fn in sorted(os.listdir(ddir)):
        dash = json.load(open(os.path.join(ddir, fn)))
        out, rep = convert_dashboard(dash, tr, datasources)
        report["dashboards"].append(rep)
        if out["spec"]["panels"]:
            json.dump(out, open(os.path.join(args.out, "dashboards", fn), "w"), indent=2)

    alerts = []
    apath = os.path.join(args.export, "alerts.json")
    for group in (json.load(open(apath)) if os.path.exists(apath) else []):
        for rule in group["rules"]:
            tr.const_vars = {}
            out, rep = convert_rule(rule, group, tr, datasources)
            report["alerts"].append(rep)
            if out:
                alerts.append(out)
    json.dump(alerts, open(os.path.join(args.out, "alerts.json"), "w"), indent=2)
    json.dump(report, open(os.path.join(args.out, "report.json"), "w"), indent=2)

    def tally(items):
        t = {"migrated": 0, "adapted": 0, "skipped": 0}
        for i in items:
            t[i["status"]] += 1
        return t
    panels = [p for d in report["dashboards"] for p in d["panels"]]
    print(json.dumps({"dashboards": len(report["dashboards"]), "panels": tally(panels),
                      "alerts": tally(report["alerts"])}, indent=2))


if __name__ == "__main__":
    main()
