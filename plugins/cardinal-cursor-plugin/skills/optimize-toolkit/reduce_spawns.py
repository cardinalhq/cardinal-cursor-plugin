#!/usr/bin/env python3
"""Mechanical reduction of spawn descriptions.

Input: raw cluster_spawns JSON (from stdin) — every spawn as its own "cluster"
       when called with min_jaccard=0.99 + min_cluster_size=1.
Output: reduced JSON with dedup + verb-bucket + tool-sig groupings, small
        enough to feed to an LLM for the semantic pass.

Stages:
  1. Flatten: unnest cluster.members into a flat spawn list.
  2. Tag zero-signal spawns (tracker entries + pre-enrichment spawns with
     no model / no tokens / no tool_signature) — RETAINED, not dropped.
     See "Zero-signal spawns are retained, not dropped" below for why v4
     changed this from v3's filter-and-discard behavior.
  3. Sub-cluster key = (verb, tool_shape). Verb = first content word of
     the label ("Migrate", "Verify", "Trace", "Find", "Research", ...).
     tool_shape = dominant + secondary tool from the spawn's tool_counts
     ("Bash+Read", "Read+Write", "WebSearch+WebFetch", etc.). A verb
     bucket like `research` gets split into `(research, Bash+Read)` for
     code-reading vs `(research, WebSearch+WebFetch)` for web lookups —
     structurally different work, previously merged. Zero-signal spawns
     have an empty tool_signature by definition, so they bucket under
     `(verb, "<empty>")` — visible, not merged into quantified rows.
  4. Dedupe by exact normalized label within each sub-cluster.
  5. Session-burst collapse: spawns from the same session within a 5min
     window count as one "burst" (retain per-burst member count).
  6. Emit: per-sub-cluster row with {verb, tool_shape, spawn_count,
     unique_labels, top_labels[:8], tokens_total, session_count,
     burst_count, zero_signal_count, sample_top_by_tokens}.

Zero-signal spawns are retained, not dropped (v4 change).

v3 filtered spawns with no model + no tokens + no tool_signature entirely,
reasoning they "carry no evidence useful for kind selection." That's true
for cost/tier math (adopt savings, downgrade share, pin thresholds all need
model or tokens) — but it silently deleted the single richest evidence
source for the *contrast-pair* mechanic (SKILL.md §5's "toolkit-consistency
adopt" play): labels like "General code review" / "Silent failure hunt" /
"Test coverage review" / "Type design review" carry zero tokens/model (they
predate per-spawn enrichment, or ride a code path that doesn't emit it) but
map 1:1 onto named agents a user's `my_toolkit_adoption` shows heavy use of
elsewhere (e.g. `pr-review-toolkit:{code-reviewer,silent-failure-hunter,
pr-test-analyzer,type-design-analyzer}`). Dropping them made that contrast
invisible.

v4 keeps every spawn in the reduction. Each spawn is tagged `zero_signal:
true/false`; each reduced row carries `zero_signal_count` (how many of its
members are label-only) alongside `spawn_count` (all members). Rows that
are ENTIRELY zero-signal naturally end up keyed under tool_shape="<empty>"
(zero-signal implies empty tool_signature) — still emitted, not hidden.
Consumers doing cost/kind math should treat `tokens_total` and
`sample_top_by_tokens[].model` as authoritative only for the
non-zero-signal share of a row (spawn_count - zero_signal_count); consumers
doing contrast-pair / label-evidence matching should read `top_labels`
regardless of zero_signal_count, since the label survives even when the
telemetry doesn't.

Caveat this reduction cannot fix: cluster_spawns' wire response never
carries `subagent_type` (verified against maestro's cluster-spawns route —
it extracts subagent_type per spawn internally but the response schema
only emits session_id/at/subagent_description/subagent_model/tokens_total).
So neither this reducer nor the label-matching pass built on it can prove
a given spawn did NOT go through a named agent — only that its label
doesn't mention one and its shape matches one's domain. Frame contrast
pairs as label/pattern evidence, not a routing fact. See SKILL.md §5's
"Contrast pairs: named-agent domain vs label-only bypass" for how the
semantic pass is expected to caveat this honestly.
"""
import json
import re
import sys
from collections import defaultdict, Counter
from datetime import datetime

def normalize(s: str) -> str:
    return re.sub(r'\s+', ' ', s.strip().lower())

def first_verb(label: str) -> str:
    """First content word, lowercased. Preserves prefixes like 'Search:' or 'W3.T3.2'."""
    words = label.strip().split()
    if not words:
        return "<empty>"
    w = words[0].lower().rstrip(',.:;')
    # Preserve compound/prefix identifiers verbatim
    if re.match(r'^(w\d|plg|mcp|search|sonnet-\d|fresh-eyes|per-adapter)', w):
        return w
    return w

def tool_shape(tool_signature: dict) -> str:
    """Grouping key over the spawn's tool_counts. Picks the top 2 tools by
    frequency, then sorts THEM alphabetically for a stable key — so
    {Bash:11, Read:10} and {Bash:10, Read:11} both bucket to "Bash+Read"
    (same underlying pattern, different noise-level ordering). Without
    the alphabetical sort, minor count fluctuations split otherwise-
    identical patterns across sub-clusters. Zero-signal spawns (no
    tool_signature) always land here as "<empty>" — a real bucket key,
    not a filtered-out state."""
    if not tool_signature:
        return "<empty>"
    ranked = sorted(tool_signature.items(), key=lambda kv: -kv[1])
    top_two_names = sorted([t for t, _ in ranked[:2]])
    if len(top_two_names) == 1:
        return f"{top_two_names[0]}-only"
    return "+".join(top_two_names)

def burst_key(session_id: str, at_iso: str, bucket_minutes: int = 5) -> str:
    t = datetime.fromisoformat(at_iso.replace('Z', '+00:00'))
    slot = t.replace(minute=(t.minute // bucket_minutes) * bucket_minutes,
                     second=0, microsecond=0)
    return f"{session_id}@{slot.isoformat()}"

def is_zero_signal(spawn: dict) -> bool:
    """A spawn with no model + no tokens + empty tool_signature carries no
    quantified evidence (adopt/downgrade/pin/extract savings math all need
    model or tokens) — but its label is still real evidence for contrast-
    pair / label-matching purposes. These come in two shapes:
      1. TaskCreate/TaskUpdate tracker entries (test-session self-pollution).
      2. Pre-enrichment sessions (v0.11 and earlier plugin versions that
         didn't emit subagent_model / tokens_total / tool_counts).
    Both are indistinguishable structurally. v4 no longer filters these
    out (see module docstring) — this function now only TAGS them so
    downstream consumers can weight/caveat appropriately."""
    tokens = spawn.get('tokens') or 0
    model = spawn.get('model')
    tool_sig = spawn.get('tool_signature') or {}
    return tokens == 0 and model is None and not tool_sig


def main():
    raw = json.load(sys.stdin)
    spawns = []
    for cluster in raw.get('clusters', []):
        sig = cluster.get('tool_signature') or {}
        for m in cluster.get('members', []):
            spawns.append({
                'label': m.get('subagent_description') or '',
                'session_id': m.get('session_id'),
                'at': m.get('at'),
                'model': m.get('subagent_model'),
                'tokens': m.get('tokens_total') or 0,
                'tool_signature': sig,
            })
    if not spawns:
        print(json.dumps({'error': 'no spawns with descriptions'}))
        return

    # v4: tag zero-signal spawns instead of dropping them. Every spawn
    # stays in the working set; `zero_signal_spawns` reports the count so
    # the caller still sees how much of the population lacks quantified
    # evidence, same honesty the old `filtered_zero_signal_spawns` gave —
    # just without discarding the labels that carry contrast-pair signal.
    raw_spawn_count = len(spawns)
    for s in spawns:
        s['zero_signal'] = is_zero_signal(s)
    zero_signal_spawn_count = sum(1 for s in spawns if s['zero_signal'])

    # Fix (1) — sub-group by (verb, tool_shape) instead of verb alone.
    # A `research` bucket previously merged Bash+Read (opus code-reading)
    # and WebSearch+WebFetch (opus web-lookup) under one row — different
    # patterns, same stem. Sub-grouping surfaces them as separate
    # recommendations without exploding the total row count (empirically
    # ~1.3x row count; still well under the LLM budget).
    buckets = defaultdict(lambda: {
        'verb': None,
        'tool_shape': None,
        'labels': Counter(),
        'tokens': 0,
        'sessions': set(),
        'bursts': set(),
        'zero_signal_count': 0,
        'sample_examples': [],  # (label, tokens, model, shape)
    })
    for s in spawns:
        v = first_verb(s['label'])
        sh = tool_shape(s['tool_signature'])
        key = (v, sh)
        b = buckets[key]
        b['verb'] = v
        b['tool_shape'] = sh
        b['labels'][s['label']] += 1
        b['tokens'] += s['tokens']
        b['sessions'].add(s['session_id'])
        b['bursts'].add(burst_key(s['session_id'], s['at']))
        if s['zero_signal']:
            b['zero_signal_count'] += 1
        b['sample_examples'].append(
            (s['label'], s['tokens'], s['model'], sh)
        )

    # Emit
    reduced = []
    for (verb, shape), b in sorted(buckets.items(), key=lambda kv: -kv[1]['tokens']):
        exs = sorted(b['sample_examples'], key=lambda x: -x[1])[:5]
        spawn_count = sum(b['labels'].values())
        reduced.append({
            'verb': verb,
            'tool_shape': shape,
            'spawn_count': spawn_count,
            'zero_signal_count': b['zero_signal_count'],
            'unique_labels': len(b['labels']),
            'top_labels': [l for l, _ in b['labels'].most_common(8)],
            'tokens_total': b['tokens'],
            'session_count': len(b['sessions']),
            'burst_count': len(b['bursts']),
            'sample_top_by_tokens': [
                {'label': l, 'tokens': t, 'model': m, 'shape': sh}
                for l, t, m, sh in exs
            ],
        })
    print(json.dumps({
        'input_spawns_raw': raw_spawn_count,
        'zero_signal_spawns': zero_signal_spawn_count,
        'sub_cluster_count': len(buckets),
        'reduced_rows': reduced,
    }, indent=2, default=str))

if __name__ == '__main__':
    main()
