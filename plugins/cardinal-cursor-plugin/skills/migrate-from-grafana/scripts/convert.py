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
    "histogram_quantile_ok": {"<family>": true|false},          # probed per native histogram
    "histogram_rate_mode": "per_second"|"per_minute"|"unknown", # how histogram_count(rate()) answers
    "supports_or_vector": true|false,                            # does `<empty> or vector(0)` give 0
    "drop_labels": ["<label>" | "<label>=<value>", ...],  # filters removed from metric queries
    "drop_log_labels": [...]   # the same for log queries (older mappings: absent, and
                               # drop_labels applies to both)
  }
  drop_*: empty unless the user chose to widen a query. "label" removes every positive
  filter (=, =~) on it, "label=value" only label="value"; negative filters (!=, !~) are
  always kept. Alert rules that would lose a filter are skipped, not widened.
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
    out, buf, depth, quote, escaped = [], "", 0, None, False
    for ch in s:
        if quote:
            buf += ch
            if escaped:
                escaped = False
            elif ch == "\\" and quote != "`":
                escaped = True
            elif ch == quote:
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


QUOTED = r'"(?:[^"\\]|\\.)*"|`[^`]*`'
MATCHER = re.compile(r'^\s*([a-zA-Z_][\w.]*)\s*(=~|!~|!=|=)\s*(' + QUOTED + r')\s*$', re.S)
# LogQL stages that create labels from the line: a filter after one of these is on a
# parsed field, not on a stored label.
LOG_PARSER = re.compile(r"\|\s*(?:json|logfmt|regexp|pattern|unpack|label_format|unwrap)\b")


def sub_outside_quotes(text, fn):
    """Apply fn to the parts of text that aren't quoted strings."""
    parts = re.split(r"(" + QUOTED + r")", text)
    return "".join(p if i % 2 else fn(p) for i, p in enumerate(parts))


def split_matchers(body):
    """'a="x", b=~"y,z"' -> ['a="x"', 'b=~"y,z"'] (commas inside quotes kept)."""
    out, buf, quote, escaped = [], "", None, False
    for ch in body:
        if quote:
            buf += ch
            if escaped:
                escaped = False
            elif ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"`":
            quote = ch
        if ch == ",":
            out.append(buf)
            buf = ""
            continue
        buf += ch
    out.append(buf)
    return [m.strip() for m in out if m.strip()]


def unquote(v):
    return v[1:-1]


def map_selectors(q, fn):
    """Rewrite every {...} selector (outside quoted strings): fn(matchers, named) returns
    the new matcher list; named = the selector follows a metric name. Returns
    (query, emptied) where emptied = a selector without a metric name ended up empty."""
    pieces, out, emptied = split_outside(q, "{", "}"), [], False
    for seg, inside in pieces:
        if not inside or not seg.startswith("{") or not seg.endswith("}"):
            out.append(seg)
            continue
        named = bool(re.search(r"[\w:]\s*$", out[-1] if out else ""))
        before = split_matchers(seg[1:-1])
        after = fn(before, named)
        if before and not after and not named:
            emptied = True
        out.append("{" + ", ".join(after) + "}")
    return "".join(out), emptied


def rename_labels_in_matcher(block, labels):
    # block is "{a="x", b=~"y"}" ; rename label keys only, never text inside a quoted value
    def sub(m):
        return labels.get(m.group(1), m.group(1)) + m.group(2)
    return sub_outside_quotes(block, lambda s: re.sub(r'([a-zA-Z_][a-zA-Z0-9_.]*)(\s*(?:=~|!~|!=|=))', sub, s))


def rename_labels_in_grouping(text, labels):
    def sub(m):
        inner = ", ".join(labels.get(x.strip(), x.strip()) for x in m.group(2).split(",") if x.strip())
        return f"{m.group(1)}({inner})"
    return re.sub(r'\b(by|without|on|ignoring|group_left|group_right)\s*\(([^)]*)\)', sub, text)


def parse_drops(entries):
    """drop_labels entries -> (labels whose positive filters all go, {(label, value)} going
    for that one value only). An entry is "label" or "label=value"."""
    whole, pairs = set(), set()
    for e in entries:
        if "=" in e:
            label, value = e.split("=", 1)
            pairs.add((label.strip(), value))
        else:
            whole.add(e.strip())
    return whole, pairs


def post_parser_spans(q):
    """(start, end) spans of LogQL pipeline text that comes after a parser stage
    (`| json`, `| logfmt`, ...): label filters there are on parsed fields."""
    masked = re.sub(QUOTED, lambda m: '"' + "_" * (len(m.group(0)) - 2) + '"', q)
    spans, depth = [], 0
    for i, ch in enumerate(masked):
        if ch == "{":
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                # the selector's pipeline runs to its range, a closing paren or the next selector
                m = re.search(r"[\[){]", masked[i + 1:])
                end = i + 1 + m.start() if m else len(masked)
                p = LOG_PARSER.search(masked, i + 1, end)
                if p:
                    spans.append((p.start(), end))
    return spans


def drop_matchers(q, entries):
    """Remove the positive filters (`=`, `=~`) named by entries ("label" or "label=value")
    from {...} selectors and from single-condition LogQL `| label op "value"` stages before
    any parser. Negative filters (`!=`, `!~`) are never removed: on a label Cardinal
    doesn't have they already match everything, and removing one widens the query.
    Returns (query, dropped, emptied): dropped = {'label' or 'label="value"'} removed,
    emptied = a selector without a metric name lost every matcher (it would match all)."""
    whole, pairs = parse_drops(entries)
    dropped = set()

    def goes(label, op, value):
        if op == "=" and (label, value) in pairs:
            return f'{label}="{value}"'
        if op in ("=", "=~") and label in whole:
            return label
        return None

    def clean(matchers, named):
        keep = []
        for mt in matchers:
            m = MATCHER.match(mt)
            hit = m and goes(m.group(1), m.group(2), unquote(m.group(3)))
            if hit:
                dropped.add(hit)
            else:
                keep.append(mt)
        return keep
    q, emptied = map_selectors(q, clean)

    parsed = post_parser_spans(q)
    pipe = re.compile(r"\|\s*([a-zA-Z_][\w.]*)\s*(=~|=)\s*(" + QUOTED + r")(?!\s*(?:and\b|or\b|,))")

    def stage(m):
        hit = goes(m.group(1), m.group(2), unquote(m.group(3)))
        if not hit or any(s <= m.start() < e for s, e in parsed):
            return m.group(0)
        dropped.add(hit)
        return ""
    q = pipe.sub(stage, q)
    q = re.sub(r"([\w:])\{\}", r"\1", q)  # metric{} -> metric
    return q, dropped, emptied


def inject_matchers(q, extra, logql=False):
    """Add matchers to every selector of a query: into each {...}, and (PromQL) as a
    selector after every bare metric name. Used for Grafana's ad-hoc filters, which
    Grafana adds to every query of the dashboard."""
    if not extra:
        return q
    q, _ = map_selectors(q, lambda ms, named: ms + [e for e in extra if e not in ms])
    if logql:
        return q
    pieces, out = split_outside(q, "{", "}"), []
    for i, (seg, inside) in enumerate(pieces):
        if inside:
            out.append(seg)
            continue
        followed = i + 1 < len(pieces) and pieces[i + 1][1]

        def add(m, seg_len):
            name = m.group(1)
            if is_keyword(name) or (followed and m.end() == seg_len):
                return name
            return name + "{" + ", ".join(extra) + "}"
        out.append(sub_metric_idents(seg, add))
    return "".join(out)


def is_keyword(name):
    return name in PROMQL_WORDS or bool(re.fullmatch(r"\d+[smhdwy]?", name))


def sub_metric_idents(seg, fn, grouping=lambda g: g):
    """Apply fn(match, masked_len) to metric-name identifiers in a non-selector piece of
    PromQL, leaving quoted strings, range selectors and grouping label lists alone
    (grouping lists go through `grouping` instead). masked_len lets fn tell whether the
    name ends the piece (i.e. a selector follows)."""
    held = []

    def hold(text):
        held.append(text)
        return f"\2{len(held) - 1}\3"
    seg = re.sub(QUOTED, lambda m: hold(m.group(0)), seg)
    seg = re.sub(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)",
                 lambda m: hold(grouping(m.group(0))), seg)
    seg = re.sub(r"\[[^\]]*\]", lambda m: hold(m.group(0)), seg)
    n = len(seg.rstrip())
    seg = IDENT.sub(lambda m: fn(m, n), seg)
    while "\2" in seg:
        seg = re.sub("\x02(\\d+)\x03", lambda m: held[int(m.group(1))], seg)
    return seg


def var_ref(name):
    """Regex for a reference to dashboard variable `name` ($x, ${x}, ${x:fmt}, [[x]])."""
    n = re.escape(name)
    return r"(?:\$\{" + n + r"(?::\w+)?\}|\$" + n + r"\b|\[\[" + n + r"\]\])"


def constrain_variables(q, constraints):
    """For `label=~"$var"` where var's "All" was narrower in Grafana than Cardinal's `.+`
    (an allValue, or a regex limiting its values), add `label=~"<constraint>"`."""
    if not constraints:
        return q

    def fn(matchers, named):
        out = list(matchers)
        for mt in matchers:
            m = MATCHER.match(mt)
            if not m or m.group(2) != "=~":
                continue
            for var, c in constraints.items():
                if re.fullmatch(r"[\"`]" + var_ref(var) + r"[\"`]", m.group(3)):
                    extra = f'{m.group(1)}=~"{c}"'
                    if extra not in out:
                        out.append(extra)
        return out
    q, _ = map_selectors(q, fn)
    for var, c in constraints.items():
        q = re.sub(r'(\|\s*([a-zA-Z_][\w.]*)\s*=~\s*"' + var_ref(var) + r'")',
                   lambda m: f'{m.group(1)} | {m.group(2)}=~"{c}"', q)
    return q


# Note texts other code keys on (panel retitling, the Grafana value comparison).
PCT_AS_AVG = "shows the average, not the percentile"
PCT_ESTIMATE = "percentile estimated from Cardinal's histogram sketch"
RATE_PER_MINUTE = "request rate from Cardinal's per-minute histogram count"
OR_VECTOR_NOOP = "'or vector(0)' has no effect in Cardinal"
# A filter was removed (the mapping's drop_labels / drop_log_labels): the query is wider
# than in Grafana. Alert rules with this note are not migrated.
FILTER_REMOVED = "filter removed, so the query is wider than in Grafana"
EMPTIED = "every filter of a selector was removed; skipped rather than match everything"
# lakerunner's rollup bucket: its per-minute histogram count / this = per second.
RATE_BUCKET_SECONDS = 60


class Translator:
    def __init__(self, mapping):
        self.metrics = mapping.get("metrics", {})
        self.labels = mapping.get("labels", {})
        self.log_labels = mapping.get("log_labels", {})
        self.hq = mapping.get("supports_histogram_quantile", True)
        # "native": Cardinal stores a histogram under its base name only (no
        # _bucket/_sum/_count series). There rate(M) and sum(M) read the sum of the
        # observed values (seconds of request time, for a duration), not the request
        # count: requests need histogram_count(), averages histogram_avg().
        self.native_hist = set(mapping.get("native_histograms", []))
        self.hq_ok = mapping.get("histogram_quantile_ok", {})
        self.rate_mode = mapping.get("histogram_rate_mode", "unknown")
        self.or_vector = mapping.get("supports_or_vector", True)
        self.drop_labels = set(mapping.get("drop_labels", []))
        self.drop_log_labels = set(mapping.get("drop_log_labels", mapping.get("drop_labels", [])))
        self.reset_dashboard()

    def reset_dashboard(self):
        """Per-dashboard state, set by convert_variables."""
        self.const_vars = {}    # variable -> value inlined into queries
        self.constraints = {}   # variable -> regex its "All" was limited to in Grafana
        self.adhoc = {"prometheus": [], "loki": []}  # Grafana ad-hoc filters, as matchers

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
        q, n = self._variables(q, logql=False)
        notes += n
        if q is None:
            return None, notes

        if self.drop_labels:
            q, dropped, emptied = drop_matchers(q, self.drop_labels)
            if dropped:
                notes.append(f"{FILTER_REMOVED}: {', '.join(sorted(dropped))} (not in Cardinal's metrics)")
            if emptied:
                return None, notes + [EMPTIED]
        if not self.or_vector and re.search(r"\bor\s+vector\(", q):
            notes.append(f"{OR_VECTOR_NOOP}: the panel shows no data where Grafana shows 0")
        if self.native_hist:
            q, hnotes, ok = self._rewrite_native_histograms(q)
            notes += hnotes
            if not ok:
                return None, notes
        # Classic histograms only: a native one was already rewritten above (no _bucket left).
        if not self.hq and "histogram_quantile" in q and "_bucket" in q:
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
            # rename metric identifiers in the non-matcher part (outside quotes, range
            # selectors and grouping clauses; grouping labels are renamed separately)
            def sub(m, _n):
                name = m.group(1)
                if is_keyword(name):
                    return name
                target, known = self.resolve_metric(name)
                if known and target is None:
                    missing.append(name)
                    return name
                if not known and re.fullmatch(r"[a-z][a-z0-9_:]*", name) and ("_" in name or ":" in name):
                    unknown.append(name)
                return target
            pieces.append(sub_metric_idents(seg, sub, lambda g: rename_labels_in_grouping(g, self.labels)))
        q = "".join(pieces)
        if missing:
            return None, notes + [f"metric not found in Cardinal: {', '.join(sorted(set(missing)))}"]
        if unknown:
            notes.append(f"metric passed through unmapped (verify it exists): {', '.join(sorted(set(unknown)))}")
        return q, notes

    def _rewrite_native_histograms(self, q):
        """Rewrite Prometheus classic-histogram idioms for histograms Cardinal keeps
        under their base name. On such a histogram rate(M)/sum(M) read the sum of the
        observed values, so each idiom maps to the histogram function that answers it:
        requests -> histogram_count, averages -> histogram_avg, percentiles ->
        histogram_quantile where the catalog probe found it usable, else histogram_avg."""
        notes, fams = [], "|".join(sorted((re.escape(f) for f in self.native_hist), key=len, reverse=True))
        if not fams:
            return q, notes, True
        sel, win, fn = r"(\{[^}]*\})?", r"\s*\[([^\]]+)\]", r"(?:rate|irate|increase)"
        # histogram_quantile(φ, sum by (le, X) (rate(M_bucket{sel}[w])))
        pat_hq = re.compile(r"histogram_quantile\(\s*([\d.]+)\s*,\s*sum\s*(?:by)?\s*\(([^)]*)\)\s*\(\s*" + fn + r"\(\s*("
                            + fams + r")_bucket" + sel + win + r"\s*\)\s*\)\s*\)")

        def hq(m):
            phi, fam, s, w = m.group(1), m.group(3), m.group(4) or "", m.group(5)
            by = [g.strip() for g in m.group(2).split(",") if g.strip() and g.strip() != "le"]
            grp = f" by ({', '.join(by)})" if by else ""
            pct = f"p{float(phi) * 100:g}"
            if self.hq_ok.get(fam):
                notes.append(f"{pct}: {PCT_ESTIMATE} (bucket resolution, so it can differ from Grafana's interpolation)")
                return f"histogram_quantile({phi}, sum{grp} (rate({fam}{s}[{w}])))"
            notes.append(f"{pct}: histogram_quantile gives no usable answer on this histogram in Cardinal; "
                         f"{PCT_AS_AVG} (title says avg)")
            return f"histogram_avg(sum{grp} (rate({fam}{s}[{w}])))"
        q = pat_hq.sub(hq, q)
        # sum by (X)(rate(M_sum[w])) / sum by (X)(rate(M_count[w]))  (the average)
        pat_avg = re.compile(r"\(?\s*sum\s*(?:by\s*\(([^)]*)\))?\s*\(\s*" + fn + r"\(\s*(" + fams + r")_sum" + sel + win
                             + r"\s*\)\s*\)\s*/\s*sum\s*(?:by\s*\([^)]*\))?\s*\(\s*" + fn + r"\(\s*\2_count"
                             + sel + r"\s*\[[^\]]+\]\s*\)\s*\)\s*\)?")

        def avg(m):
            notes.append("average (sum/count) computed with histogram_avg")
            grp = f" by ({m.group(1).strip()})" if m.group(1) else ""
            return f"histogram_avg(sum{grp} (rate({m.group(2)}{m.group(3) or ''}[{m.group(4)}])))"
        q = pat_avg.sub(avg, q)
        # rate(M_count[w]) is the request rate: histogram_count(rate(M[w])).
        pat_cnt = re.compile(r"\b(rate|irate|increase)\(\s*(" + fams + r")_count" + sel + win + r"\s*\)")

        def cnt(m):
            f, fam, s, w = m.group(1), m.group(2), m.group(3) or "", m.group(4)
            if self.rate_mode == "per_minute":
                notes.append(f"{RATE_PER_MINUTE} (/60): correct at 1-minute resolution or finer; "
                             "zoomed-out views overstate it until lakerunner answers per second")
                per_s = f"histogram_count(rate({fam}{s}[{w}])) / {RATE_BUCKET_SECONDS}"
                return f"({per_s} * {dur_seconds(w)})" if f == "increase" else f"({per_s})"
            if self.rate_mode != "per_second":
                notes.append("request rate as histogram_count(rate(...)): Cardinal's scale for it could not be "
                             "measured; check the values against Grafana")
            return f"histogram_count({'increase' if f == 'increase' else 'rate'}({fam}{s}[{w}]))"
        q2 = pat_cnt.sub(cnt, q)
        if q2 != q:
            q = q2
        # rate(M_sum[w]): the sum of observed values per second, which is what rate(M) reads.
        q2 = re.sub(r"\b(rate|irate|increase)\(\s*(" + fams + r")_sum" + sel + win + r"\s*\)",
                    lambda m: f"{m.group(1)}({m.group(2)}{m.group(3) or ''}[{m.group(4)}])", q)
        if q2 != q:
            notes.append("rate of the histogram's sum: rate() on a Cardinal histogram reads the sum of observed values")
            q = q2
        # Any other M_count (e.g. counting series): the histogram's series, presence only.
        q2 = re.sub(r"\b(" + fams + r")_count\b", r"\1", q)
        if q2 != q:
            notes.append("histogram _count series replaced by the histogram itself (series presence, not a count)")
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
        q, n = self._variables(q, logql=True)
        notes += n
        if q is None:
            return None, notes
        # lakerunner's LogQL engine rejects set operators against scalars
        # (`... or vector(0)`); drop the fallback, the panel just shows no data instead of 0.
        q2 = re.sub(r"\s+or\s+vector\(\s*[\d.]+\s*\)\s*$", "", q)
        if q2 != q:
            notes.append("'or vector(0)' fallback removed (not supported on log queries in Cardinal)")
            q = q2
        if self.drop_log_labels:
            q, dropped, emptied = drop_matchers(q, self.drop_log_labels)
            if dropped:
                notes.append(f"{FILTER_REMOVED}: {', '.join(sorted(dropped))} (not on Cardinal's logs)")
            # A stream selector left empty would read every service's logs: skip instead.
            if emptied:
                return None, notes + [EMPTIED]
        before = q

        def rename(text):
            for src, dst in self.log_labels.items():
                text = re.sub(r"(?<![\w.])" + re.escape(src) + r"(?=\s*(=~|!~|!=|=|\)|,))", dst, text)
            for src, dst in self.labels.items():
                if src not in self.log_labels:
                    text = re.sub(r"(?<=[{,\s(|])" + re.escape(src) + r"(?=\s*(=~|!~|!=|=))", dst, text)
            return text
        q = sub_outside_quotes(q, rename)  # never inside line filters or values
        if q != before:
            notes.append("log labels renamed for Cardinal")
        return q, notes

    def _variables(self, q, logql):
        """Inline constant variables, add the dashboard's ad-hoc filters, and keep each
        variable's "All" as narrow as it was in Grafana."""
        notes = []
        for name, value in self.const_vars.items():
            q, n = re.subn(var_ref(name), lambda _m: value, q)
            if n:
                notes.append(f"variable ${name} inlined as '{value}'")
        adhoc = self.adhoc["loki" if logql else "prometheus"]
        if None in adhoc:
            return None, notes + ["the dashboard's ad-hoc filter can't be expressed in Cardinal"]
        if adhoc:
            q = inject_matchers(q, adhoc, logql=logql)
            notes.append(f"Grafana ad-hoc filter(s) added to the query: {', '.join(adhoc)}")
        q2 = constrain_variables(q, self.constraints)
        if q2 != q:
            used = sorted(v for v in self.constraints if re.search(var_ref(v), q))
            notes.append(f"'All' of ${', $'.join(used)} kept as narrow as in Grafana (extra matcher)")
            q = q2
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


ADHOC_OPS = {"=", "!=", "=~", "!~"}


def prom_string(s):
    """s as the contents of a double-quoted PromQL/LogQL string."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def regex_escape(s):
    """A literal value inside a regex matcher (what Grafana does for multi-value variables)."""
    return re.sub(r"([\\^$*+?.()|\[\]{}])", r"\\\1", str(s))


def all_constraint(v):
    """The regex a query variable's "All" was limited to in Grafana, as PromQL string
    contents, or None when it wasn't (Cardinal's All is `.+`). Returns (constraint, note)."""
    allv = v.get("allValue")
    if allv not in (None, "", ".*", ".+"):
        return allv, None  # Grafana pastes allValue into the query as it is
    rx = v.get("regex") or ""
    if not rx:
        return None, None
    m = re.fullmatch(r"/(.*)/([a-z]*)", rx, re.S)
    body, flags = (m.group(1), m.group(2)) if m else (rx, "")
    if re.search(r"(?<!\\)\((?!\?:)", body):
        return None, (f"variable ${v.get('name')}: its regex {rx} reshapes the values (capture group); "
                      "not carried over, so 'All' matches every value of the label")
    # Grafana searches the regex anywhere in the value; PromQL regexes are anchored.
    return prom_string(("(?i)" if "i" in flags else "") + f".*(?:{body}).*"), None


def adhoc_matchers(v):
    """Grafana ad-hoc filters -> matchers. Returns (matchers, unsupported filters)."""
    out, bad = [], []
    for f in v.get("filters") or []:
        key, op, val = f.get("key"), f.get("operator", "="), f.get("value", "")
        if not key or op not in ADHOC_OPS:
            bad.append(f"{key} {op} {val}")
            continue
        out.append(f'{key}{op}"{prom_string(val)}"')
    return out, bad


def inline_value(v):
    """The value a non-query variable is inlined as. Returns (value, note or None)."""
    cur = v.get("current", {}) or {}
    val = cur.get("value")
    opts = [o.get("value") for o in v.get("options") or [] if o.get("value") not in (None, "$__all")]
    is_all = val == "$__all" or (isinstance(val, list) and "$__all" in val)
    if is_all:
        if v.get("allValue"):
            return v["allValue"], None
        if v.get("type") == "custom" and opts:
            # Grafana's All is the variable's own options, not every value.
            return "|".join(regex_escape(o) for o in opts), None
        return ".+", f"variable ${v.get('name')}: 'All' now matches every value (Grafana limited it to its options)"
    if isinstance(val, list):
        vals = [x for x in val if x != "$__all"]
        return ("|".join(regex_escape(x) for x in vals) if len(vals) > 1 else (vals[0] if vals else ".+")), None
    if val in (None, ""):
        if v.get("includeAll"):
            return v.get("allValue") or ".+", None
        return (opts[0] if opts else (v.get("query") or "")), None
    return str(val), None


def convert_variables(dash, tr, datasources):
    variables, notes = [], []
    for v in dash.get("templating", {}).get("list", []):
        name, vtype = v.get("name"), v.get("type")
        if vtype == "adhoc":
            matchers, bad = adhoc_matchers(v)
            kind = "loki" if ds_type(v.get("datasource"), datasources, "prometheus") == "loki" else "prometheus"
            tr.adhoc[kind] += matchers
            if matchers:
                notes.append(f"ad-hoc filter ${name} added to every {kind} query: {', '.join(matchers)}")
            if bad:
                tr.adhoc[kind].append(None)  # marks: some filter can't be expressed
                notes.append(f"ad-hoc filter ${name} has filters Cardinal can't express ({'; '.join(bad)}): "
                             f"{kind} queries are skipped rather than shown unfiltered")
            continue
        if vtype == "query":
            query = v.get("query")
            query = query.get("query") if isinstance(query, dict) else query
            m = re.match(r"\s*label_values\(\s*(?:([a-zA-Z_:][\w:]*)\s*(\{[^}]*\})?\s*,\s*)?([\w.]+)\s*\)\s*$", query or "")
            vt = ds_type(v.get("datasource"), datasources, "prometheus")
            if m and (vt == "loki" or m.group(1)):
                constraint, cnote = all_constraint(v)
                if constraint:
                    tr.constraints[name] = constraint
                    notes.append(f"variable ${name}: its 'All' was limited to {constraint!r} in Grafana; "
                                 "kept as an extra matcher next to it")
                if cnote:
                    notes.append(cnote)
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
        val, vnote = inline_value(v)
        tr.const_vars[name] = val
        notes.append(f"variable ${name} ({vtype}) has no Cardinal equivalent; inlined as '{val}'")
        if vnote:
            notes.append(vnote)
    return variables, notes


def avg_title(text):
    """'p95 latency' -> 'avg latency'; a title without a pNN gets ' (avg)'."""
    new, n = re.subn(r"\b[pP](50|75|90|95|99|999)\b", "avg", text or "")
    if not n:
        new, n = re.subn(r"(?i)\bpercentiles?\b", "average", new)
    return new if n else f"{text} (avg)"


# Grafana transformations that remove rows, series or fields from what the panel shows.
FILTERING_TRANSFORMS = {"filterByValue", "filterFieldsByName", "filterByRefId", "filterByName", "limit"}


def display_filter_notes(p):
    """What Grafana does to a panel's results after the query that doesn't carry over."""
    notes = []
    for t in p.get("transformations") or []:
        if t.get("disabled"):
            continue
        tid = t.get("id", "?")
        hides = tid == "organize" and any((t.get("options") or {}).get("excludeByName", {}).values())
        if tid in FILTERING_TRANSFORMS or hides:
            notes.append(f"Grafana transformation '{tid}' is not carried over: the panel can show "
                         "series or values Grafana filtered out")
        else:
            notes.append(f"Grafana transformation '{tid}' is not carried over")
    for o in ((p.get("fieldConfig") or {}).get("overrides") or []):
        for prop in o.get("properties") or []:
            if prop.get("id") == "custom.hideFrom" and (prop.get("value") or {}).get("viz"):
                notes.append("series hidden in Grafana (field override) are shown in Cardinal")
                break
    if p.get("repeat"):
        notes.append(f"repeated per ${p['repeat']} in Grafana; one panel for all its values in Cardinal")
    return notes


def convert_panel(p, tr, datasources, pid):
    """Return (cardinal_panel | None, status, notes, refs): refs are the Grafana refIds
    of the panel's Cardinal queries, in order (for the Grafana value comparison)."""
    gtype = p.get("type")
    title = p.get("title") or "Untitled"
    notes = []
    panel_ds = ds_type(p.get("datasource"), datasources)

    if gtype in ("text", "news", "dashlist", "alertlist", "annolist", "welcome", "gettingstarted"):
        return None, "skipped", [f"'{gtype}' panels have no Cardinal equivalent"], []
    if gtype in ("traces", "nodeGraph", "flamegraph") or panel_ds in ("tempo", "jaeger", "zipkin", "grafana-pyroscope-datasource"):
        return None, "skipped", ["trace/profile panels are not supported in Cardinal dashboards; use Cardinal's trace explorer"], []

    queries, logs_exprs, refs, log_refs = [], [], [], []
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
        legend = t.get("legendFormat")
        if any(PCT_AS_AVG in x for x in n) and legend:
            legend = avg_title(legend) if re.search(r"\b[pP]\d+\b", legend) else legend
        if t.get("format") == "heatmap":
            notes.append("heatmap-format query kept as a plain series")
        q = {"query": new, "queryKind": kind}
        if legend and legend != "__auto":
            renames = {**tr.labels, **tr.log_labels} if kind == "loki" else tr.labels
            q["name"] = re.sub(r"\{\{\s*([\w.]+)\s*\}\}", lambda m: "{{" + renames.get(m.group(1), m.group(1)) + "}}", legend)
        if kind == "loki" and not re.search(r"(_over_time|rate)\s*\(", new):
            logs_exprs.append(new)
            log_refs.append(t.get("refId"))
        elif any(x["query"] == new for x in queries):
            # p50/p95/p99 that all became the same average: one series, not three.
            notes.append("queries that became identical were merged")
        else:
            queries.append(q)
            refs.append(t.get("refId"))

    notes += display_filter_notes(p)
    fc = (p.get("fieldConfig") or {}).get("defaults", {}) or {}
    unit = UNIT_MAP.get(fc.get("unit", ""), fc.get("unit", ""))
    if any(PCT_AS_AVG in x for x in notes):
        title = avg_title(title)
    base = {"id": pid, "title": title}
    if p.get("description"):
        base["description"] = p["description"]

    if gtype == "logs" or (logs_exprs and not queries):
        if not logs_exprs:
            return None, "skipped", notes + ["no usable log query"], []
        panel = dict(base, kind="log-events", queries=[], rawLogql=logs_exprs[0], limit=100)
        if len(logs_exprs) > 1:
            notes.append("only the first log query was kept")
        return panel, ("adapted" if notes else "migrated"), notes, log_refs[:1]

    if not queries:
        return None, "skipped", notes + ["no query could be migrated"], []

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

    if panel["kind"] == "stat":
        refs = refs[:1]
    return panel, ("adapted" if notes else "migrated"), notes, refs


def flatten_sections(dash):
    """Yield (section_title, [panels]) from Grafana's flat-with-rows layout."""
    sections, current = [], {"title": dash.get("title", "Dashboard"), "row_y": -1, "panels": []}
    for p in sorted(dash.get("panels", []), key=lambda x: (x.get("gridPos", {}).get("y", 0), x.get("gridPos", {}).get("x", 0))):
        if p.get("type") == "row":
            if current["panels"]:
                sections.append(current)
            current = {"title": p.get("title") or "Row", "row_y": p.get("gridPos", {}).get("y", 0),
                       "panels": list(p.get("panels", [])), "collapsed": bool(p.get("collapsed")),
                       "repeat": p.get("repeat")}
            continue
        current["panels"].append(p)
    if current["panels"]:
        sections.append(current)
    if len(sections) == 1 and sections[0]["row_y"] == -1:
        sections[0]["title"] = "Panels"
    return sections


def convert_dashboard(dash, tr, datasources):
    tr.reset_dashboard()
    variables, var_notes = convert_variables(dash, tr, datasources)
    report = {"uid": dash.get("uid"), "title": dash.get("title"), "variables": var_notes, "panels": []}
    panels, sections = {}, []
    n = 0
    for sec in flatten_sections(dash):
        cells = []
        for p in sec["panels"]:
            n += 1
            pid = f"p{n}"
            cp, status, notes, refs = convert_panel(p, tr, datasources, pid)
            if sec.get("repeat") and cp:
                notes = notes + [f"row repeated per ${sec['repeat']} in Grafana; one row for all its values in Cardinal"]
                status = "adapted"
            entry = {"title": p.get("title"), "grafana_type": p.get("type"), "grafana_id": p.get("id"),
                     "status": status, "notes": sorted(set(notes))}
            if cp:
                entry.update(cardinal_id=pid, query_refs=refs)
                if cp["title"] != p.get("title"):
                    entry["cardinal_title"] = cp["title"]
            report["panels"].append(entry)
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
    if any(FILTER_REMOVED in x for x in n):
        # A wider query fires on data the Grafana rule never looked at (other
        # environments, other services, every status code): don't create it.
        return None, dict(report, status="skipped", notes=sorted(set(notes)) + [
            "not migrated: removing a filter would make the rule fire on data the Grafana rule ignores; "
            "recreate it in Cardinal with a filter that exists there"])

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
            "source": {"grafana_uid": ga.get("uid"), "folder": group["folder"], "group": group["group"],
                       "grafana_expr": expr, "datasource_uid": qd.get("datasourceUid")}}, \
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
            tr.reset_dashboard()
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
