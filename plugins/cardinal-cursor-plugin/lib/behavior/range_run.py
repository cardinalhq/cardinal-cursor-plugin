# Copyright (c) 2025-2026 CardinalHQ, Inc. All rights reserved.
"""Run an accepted check over any time range as consecutive one-hour executions.

Deployed execution covers at most one hour (and 256 objects / 256 MiB) per
window. A range is split into windows of at most an hour and run one at a time
through the existing execute/poll code paths; a window the API rejects for its
size is halved and retried. Verdict logic, the judge and receipts are never
touched: this module only plans windows, keeps every raw per-window result,
and adds a canonical verdict column.

Traces that cross a window edge were seen with only part of their spans in at
least one window, so a partial window's verdict may rest on incomplete
evidence that programs without a completeness signal cannot detect:

1. Detect: a trace is an edge trace when it appears in more than one window,
   or when its span extent (earliest start to latest end, supplied from the
   query tools) reaches past the edges of a window that evaluated it.
2. Re-evaluate whole: when the extent fits within one hour, one extra window
   aligned to cover it is executed; its verdict is canonical.
3. Otherwise UNKNOWN: no window can hold the whole trace, no extent is known,
   the trace is absent from the aligned window, or the aligned evaluation
   still reports missing evidence (coverage gaps) -> UNKNOWN with
   EDGE_REASON.
4. Never upgrade silently: partial-window verdicts never decide an edge
   trace's canonical verdict; only a whole-trace evaluation does.
5. Report: edge-trace count, conflict count (partials disagreeing with each
   other or with the canonical verdict), each conflict's per-window verdicts.
6. Preserve: every raw per-window observation stays in the output.
"""
from __future__ import annotations

import ast
from collections import Counter
import datetime
import math
import re

MAX_WINDOW = 3600
MIN_WINDOW = 60
# Pad an aligned whole-trace window by a second each side: span timestamps are
# nanoseconds, window bounds RFC3339 seconds.
ALIGN_PAD = 1
EDGE_REASON = 'window-edge: trace not evaluated whole'
VERDICTS = ('MATCH', 'NON_MATCH', 'UNKNOWN', 'ERROR', 'NOT_APPLICABLE')
# lakerunner queryapi/behavior_programs.go: "behavior population exceeds 256
# selected trace objects; narrow the time window" / "... exceeds 256 MiB; ...".
SIZE_REJECTION = re.compile(r'exceeds 256|narrow the time window', re.IGNORECASE)


def parse_time(value) -> float:
    """RFC3339 (Z or offset, optional fraction) or epoch seconds -> epoch seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError('time must be RFC3339')
    text = value.strip()
    if text.endswith('Z') or text.endswith('z'):
        text = text[:-1] + '+00:00'
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:\d{2})', text)
    if not match:
        raise ValueError(f'time must be RFC3339 with a timezone: {value!r}')
    base = datetime.datetime.fromisoformat(match.group(1) + match.group(3))
    fraction = float(match.group(2)) if match.group(2) else 0.0
    return base.timestamp() + fraction


def format_time(seconds: float) -> str:
    """RFC3339 UTC with at most millisecond precision (the API rejects finer)."""
    millis = int(round(seconds * 1000))
    whole, fraction = divmod(millis, 1000)
    stamp = datetime.datetime.fromtimestamp(whole, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    if fraction:
        stamp += ('.%03d' % fraction).rstrip('0')
    return stamp + 'Z'


def split_windows(start: float, end: float, max_seconds: int = MAX_WINDOW) -> list:
    """[start, end) as consecutive windows of at most max_seconds."""
    if end <= start:
        raise ValueError('end must be after start')
    windows, cursor = [], start
    while cursor < end:
        nxt = min(end, cursor + max_seconds)
        windows.append([cursor, nxt])
        cursor = nxt
    return windows


def halve(window, min_seconds: int = MIN_WINDOW):
    """Two halves of a window, or None when a half would be under min_seconds."""
    start, end = window
    if (end - start) / 2 < min_seconds:
        return None
    mid = start + round((end - start) / 2)   # whole seconds from the window start
    return [[start, mid], [mid, end]]


def is_size_rejection(message) -> bool:
    return bool(SIZE_REJECTION.search(str(message or '')))


def contains(window, extent) -> bool:
    return extent[0] >= window[0] and extent[1] <= window[1]


def align_whole(extents: dict, max_seconds: int = MAX_WINDOW, pad: float = ALIGN_PAD):
    """Group trace extents into as few aligned windows as possible.

    Returns (groups, too_long): groups are {'window': [s, e], 'trace_ids': [...]}
    with every member's extent inside the window and the window at most
    max_seconds; too_long lists traces whose own extent exceeds max_seconds."""
    groups, too_long = [], []
    for trace_id, (start, end) in sorted(extents.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[0])):
        if end - start > max_seconds:
            too_long.append(trace_id)
            continue
        if groups:
            group = groups[-1]
            lo, hi = group['span']
            if max(hi, end) - lo <= max_seconds:
                group['span'] = [lo, max(hi, end)]
                group['trace_ids'].append(trace_id)
                continue
        groups.append({'span': [start, end], 'trace_ids': [trace_id]})
    out = []
    for group in groups:
        lo, hi = group['span']
        # Whole seconds, widened outward: span timestamps are nanoseconds.
        window = [math.floor(lo - pad), math.ceil(hi + pad)]
        if window[1] - window[0] > max_seconds:
            window = [math.floor(lo), math.ceil(hi)]
        if window[1] - window[0] > max_seconds:
            window = [lo, hi]
        out.append({'window': window, 'trace_ids': group['trace_ids']})
    return out, too_long


def uses_judge(source) -> bool:
    """Does the program call the judge (JEV.decide(proposition=...))?

    Unparseable source counts as judge-based, so a check is never shown without
    its Experimental label by mistake."""
    try:
        tree = ast.parse(source or '')
    except SyntaxError:
        return True
    return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'decide'
               and any(k.arg == 'proposition' for k in node.keywords)
               for node in ast.walk(tree))


def gap_reasons(raw) -> list:
    gaps = raw.get('coverage_gaps') or (raw.get('validation_observations') or {}).get('coverage_gaps') or []
    return [str(g.get('reason') or g.get('ref') or 'coverage gap') for g in gaps if isinstance(g, dict)]


def detect_edges(windows: list, observations: dict, extents: dict) -> dict:
    """{trace_id: [why, ...]} for every edge trace (rule 1).

    windows: [[s, e], ...] for the range's ordinary windows (by index);
    observations: {trace_id: [window_index, ...]}; extents: {trace_id: [s, e]}."""
    edges = {}
    for trace_id, indexes in observations.items():
        why = []
        if len(set(indexes)) > 1:
            why.append('seen in %d windows' % len(set(indexes)))
        extent = extents.get(trace_id)
        if extent is not None and any(not contains(windows[i], extent) for i in set(indexes)):
            why.append('span extent crosses a window edge')
        if why:
            edges[trace_id] = why
    return edges


def canonicalize(windows: list, window_results: list, extents: dict, whole_runs: list) -> dict:
    """Apply the edge rules. Pure: no I/O.

    windows: [[s, e], ...] ordinary windows that completed.
    window_results: per window, a list of compact rows {trace_id, verdict,
      reason, witness_refs, coverage_gaps, execution_id}.
    extents: {trace_id: [s, e]} from the query tools (may be partial or empty).
    whole_runs: [{'window', 'trace_ids', 'status', 'execution_id', 'results',
      'error'}] aligned re-evaluations (results as window_results rows).
    """
    observed = {}
    for index, rows in enumerate(window_results):
        for row in rows:
            observed.setdefault(row['trace_id'], []).append(index)
    edges = detect_edges(windows, observed, extents)
    whole_for = {}
    for run in whole_runs:
        rows = {r['trace_id']: r for r in run.get('results') or []}
        for trace_id in run.get('trace_ids') or []:
            whole_for[trace_id] = (run, rows.get(trace_id))

    traces, conflicts = [], []
    for trace_id in sorted(observed):
        partial = []
        for index in observed[trace_id]:
            row = next(r for r in window_results[index] if r['trace_id'] == trace_id)
            partial.append({'window_index': index, 'window': [format_time(t) for t in windows[index]],
                            'execution_id': row.get('execution_id'), 'verdict': row['verdict'],
                            'reason': row.get('reason')})
        entry = {'trace_id': trace_id, 'edge': trace_id in edges, 'edge_why': edges.get(trace_id, []),
                 'windows': partial}
        if trace_id not in edges:
            row = next(r for r in window_results[observed[trace_id][0]] if r['trace_id'] == trace_id)
            entry['canonical'] = {'verdict': row['verdict'], 'reason': row.get('reason'), 'source': 'window',
                                  'execution_id': row.get('execution_id'),
                                  'witness_refs': row.get('witness_refs', [])}
        else:
            run, whole = whole_for.get(trace_id, (None, None))
            if whole is not None:
                entry['whole'] = {'window': [format_time(t) for t in run['window']],
                                  'execution_id': whole.get('execution_id'), 'verdict': whole['verdict'],
                                  'reason': whole.get('reason')}
            gaps = gap_reasons(whole) if whole is not None else []
            if whole is not None and not gaps:
                entry['canonical'] = {'verdict': whole['verdict'], 'reason': whole.get('reason'),
                                      'source': 'whole', 'execution_id': whole.get('execution_id'),
                                      'witness_refs': whole.get('witness_refs', [])}
            else:
                if whole is not None:
                    why = 'the whole-trace evaluation still reports missing evidence: ' + '; '.join(gaps)
                elif trace_id not in extents:
                    why = 'no span extent was supplied, so no window could be aligned to it'
                elif extents[trace_id][1] - extents[trace_id][0] > MAX_WINDOW:
                    why = 'its spans cover more than one hour'
                elif run is None:
                    why = 'no aligned window was run for it'
                elif run.get('status') != 'COMPLETED':
                    why = 'the aligned window did not complete (%s)' % (run.get('error') or run.get('status'))
                else:
                    why = 'the aligned window did not select it'
                entry['canonical'] = {'verdict': 'UNKNOWN', 'reason': f'{EDGE_REASON} ({why})',
                                      'source': 'edge-unresolved', 'execution_id': None, 'witness_refs': []}
            seen = {p['verdict'] for p in partial}
            if len(seen | {entry['canonical']['verdict']}) > 1:
                entry['conflict'] = True
                conflicts.append({'trace_id': trace_id, 'windows': [{k: p[k] for k in ('window', 'verdict')}
                                                                     for p in partial],
                                  'whole': entry.get('whole', {}).get('verdict'),
                                  'canonical': entry['canonical']['verdict']})
        traces.append(entry)

    counts = Counter(t['canonical']['verdict'] for t in traces)
    raw_counts = Counter(p['verdict'] for t in traces for p in t['windows'])
    return {'traces': traces, 'conflicts': conflicts, 'edge_traces': sorted(edges),
            'counts': {v: counts.get(v, 0) for v in VERDICTS},
            'raw_window_counts': {v: raw_counts.get(v, 0) for v in VERDICTS},
            'population': len(traces)}


def judge_usage(raw_results) -> dict:
    """Actual judge (JEV) usage summed from retained per-trace receipts.

    Counts calls, attempts and tokens exactly as reported; cost is summed only
    when every receipt reports one, else 'not available'. Never estimates."""
    calls = attempts = input_tokens = output_tokens = 0
    complete = True
    costs = []
    models = Counter()
    for raw in raw_results:
        for receipt in raw.get('jev_receipts') or []:
            calls += 1
            attempts += len(receipt.get('attempts') or []) or 1
            usage = receipt.get('usage') or {}
            if type(usage.get('input_tokens')) is int:
                input_tokens += usage['input_tokens']
            else:
                complete = False
            if type(usage.get('output_tokens')) is int:
                output_tokens += usage['output_tokens']
            else:
                complete = False
            if receipt.get('usage_complete') is False:
                complete = False
            cost = receipt.get('cost_usd')
            costs.append(cost if type(cost) in (int, float) else None)
            model = (receipt.get('config') or {}).get('model')
            if model:
                models[str(model)[:128]] += 1
    cost_usd = round(sum(costs), 6) if costs and all(c is not None for c in costs) else None
    return {'judge_calls': calls, 'attempts': attempts, 'input_tokens': input_tokens,
            'output_tokens': output_tokens, 'usage_complete': complete,
            'cost_usd': cost_usd if calls else 0.0,
            'cost': ('%.6f USD' % cost_usd) if cost_usd is not None else ('0 USD' if not calls else 'not available'),
            'models': dict(models)}


def unknown_reasons(traces: list, limit: int = 20) -> list:
    """Canonical UNKNOWN reasons grouped and counted, most common first."""
    reasons = Counter()
    for trace in traces:
        if trace['canonical']['verdict'] == 'UNKNOWN':
            reasons[(trace['canonical'].get('reason') or 'no reason given')[:300]] += 1
    return [{'reason': reason, 'count': count} for reason, count in reasons.most_common(limit)]


def boundaries(windows: list) -> list:
    """Every edge a trace can be cut at: the range's ends and each internal boundary."""
    points = sorted({t for window in windows for t in window})
    return [format_time(t) for t in points]


def parse_extents(value) -> dict:
    """{trace_id: {'start': rfc3339, 'end': rfc3339}} (or [start, end]) -> {trace_id: [s, e]}."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('trace_extents must map trace IDs to {start, end}')
    out = {}
    for trace_id, extent in value.items():
        if not isinstance(trace_id, str) or not re.fullmatch('[a-f0-9]{32}', trace_id):
            raise ValueError('trace_extents keys must be 32-character lowercase hex trace IDs')
        if isinstance(extent, dict):
            start, end = extent.get('start'), extent.get('end')
        elif isinstance(extent, (list, tuple)) and len(extent) == 2:
            start, end = extent
        else:
            raise ValueError(f'trace_extents[{trace_id}] must be {{start, end}}')
        lo, hi = parse_time(start), parse_time(end)
        if hi < lo:
            raise ValueError(f'trace_extents[{trace_id}] ends before it starts')
        out[trace_id] = [lo, hi]
    return out
