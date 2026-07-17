---
name: cardinal-optimize-toolkit
description: Mine this engineer's own session telemetry for capability-fit recommendations (extract a new sub-agent, pin/downgrade a model tier, adopt or swap an existing capability, consolidate duplicates) and, on confirmation, write the accepted artifact into the working tree. Only run this when the user explicitly asks to optimize their toolkit, review capability-fit recommendations, or names this skill directly — do not trigger it opportunistically off a loose topical match.
---

# Cardinal Optimize Toolkit

Use this skill when the user explicitly asks Cursor to optimize their
toolkit, mine their own session history for capability-fit
recommendations, or review whether a recurring inline pattern should
become a reusable sub-agent. It is **not** a skill to reach for
opportunistically just because the conversation is adjacent to agents
or tooling — see **What not to do** below for why.

A **per-user, past-informed, future-effective** optimizer — not a
session-local one. It looks at your own last 30 days of sessions,
picks the top few recurring inline-work patterns worth acting on,
authors the artifact itself grounded in your actual toolkit, and — only
on your explicit confirmation — writes the accepted artifact to this
working tree. **Anything written takes effect next session**, not this
one: Cursor loads custom subagent definitions (`.cursor/agents/*.md`)
at startup — the same restart requirement this plugin's connect flow
already asks for when it writes `~/.cursor/mcp.json` and
`~/.cursor/hooks.json` — there is no live-registration channel, so
accepting a candidate here will not change what happens for the rest
of this conversation.

This burns your own session tokens to run. The server (conductor's
maestro, via the `cardinal` MCP server) provides evidence — clusters,
model mix, toolkit adoption, tier pricing, priced-savings math. **You
(Cursor) do the authorship** from first principles: pick the
recommendation kind by reasoning about the evidence, then write the
`.cursor/agents/<name>.md` artifact grounded in this user's real
capabilities, real tool signatures, real cluster labels. There is no
server-side artifact template — the artifact is composed here, per
invocation, and passed back to the ledger verbatim on `mark`.

## Before you start

This skill orchestrates six `outcomes__*` tools served by the
`cardinal` MCP server (see this plugin's `cardinal-connect` script —
same server, same consent). They are documented in conductor's
`docs/specs/optimize-toolkit-mcp-tools.md`:

1. `outcomes__my_turn_pattern`
2. `outcomes__my_toolkit_adoption`
3. `outcomes__cluster_spawns`
4. `outcomes__org_offered_tiers`
5. `outcomes__estimate_savings`
6. `outcomes__mark`

`outcomes__my_recent_spawns` also exists on the same server — it's a
raw spawn history for ad-hoc debugging, not part of this flow. Reach
for it only if the user explicitly asks "what did I spawn recently?".

**Check your available tools before doing anything else.** If none of
the `outcomes__*` tools appear in your tool list (they'd show under
the `cardinal` MCP server, alongside whatever other integrations this
org has configured), say so plainly and stop:

> "The optimize-toolkit tools aren't wired into this org's Cardinal MCP
> server yet — that's a known rollout gap, not a problem with your
> connection. Nothing to do here yet."

Do not attempt a substitute analysis (no reading transcripts, no
grepping local logs, no falling back to "let me look at your repo
instead"). MCP unreachable or empty candidates → one line, stop, no
retries.

## How you (Cursor) should run this

Budget: usually 5–7 tool calls total, ≤10 in the worst case. You'll
also read a handful of files from the working tree to ground kind
picking and authorship — that's fine, keep it targeted.

### 1. Open — situational-awareness bundle (2 calls)

Call `outcomes__my_turn_pattern` and `outcomes__my_toolkit_adoption`,
both with `window: "30d"`. From the two responses, compose a short
(≤6 line) opening paragraph before mentioning any candidate:

- State the evidence window explicitly ("based on your last 30 days of
  sessions") — never let the opening imply "this session."
- Name the caller's top 1–2 model-mix rows by cost from
  `my_turn_pattern.models`.
- Name the toolkit-adoption headline the first candidate will target,
  e.g. "231 Explore-shaped spawns / 32.6M subtok in the last 30 days"
  — from whichever `my_toolkit_adoption` surface has the biggest
  signal this window: `.agents`, `.skills`, `.commands`,
  `.mcp_servers`, or `.tool_counts`. Built-ins count on equal footing
  with namespaced agents here — see step 5.
- **No usable evidence, stop before any ratio math.** If
  `my_toolkit_adoption.coverage.sessions_scanned == 0`, or
  `my_turn_pattern.turns_total` is 0, there is nothing to compute a
  coverage ratio from — say "no optimization candidates right now,
  not enough session history yet" and stop.
- **Coverage caveat.** Use `my_toolkit_adoption.coverage.sessions_with_tier_attribution
  / coverage.sessions_scanned` as the enrichment-coverage proxy. When
  that ratio is under 0.5, prepend a one-line caveat: "evidence from
  N/M of your sessions in the last 30 days — coverage will climb as
  you keep working on a current plugin version." Also check
  `my_turn_pattern.coverage.plugin_versions_seen` — if it shows a
  stale version for recent turns, mention the plugin-version-drift
  possibility and suggest a Cursor restart.

### 2. Discover — pull raw spawns (1 call)

Call `outcomes__cluster_spawns` with `window: "30d"`, **`min_jaccard: 0.99`**,
**`min_cluster_size: 1`**. Those thresholds effectively disable the server's
built-in token-Jaccard clustering — each spawn returns as its own single-
member "cluster", giving you the raw spawn population (label + session_id +
tool_signature + tokens + model). You do the actual clustering client-side
in the next step, because the server's token-Jaccard is too coarse for
semantically-similar-but-token-diverse labels ("Trace polly cwd flow" and
"Map maestro sites/org API" are both investigation-shape but share zero
content tokens).

Note the `coverage.with_description_share` — if it's under 0.5, mention it
in the caveat you already added in step 1 (older sessions from pre-enrichment
plugin versions don't emit `subagent_description`; it's legacy drift, not
a per-agent gap).

### 3. Reduce + semantically cluster (Bash + LLM pass)

**Stage 3a — mechanical reduction (Bash).** Save the cluster_spawns
response to a temp file and pipe it through the reducer that ships
alongside this SKILL.md. The reducer collapses N-identical labels,
groups by first content-word (verb), sub-groups by dominant-tool
signature, and collapses same-session bursts. It turns 100+ raw spawn
records into ~15–30 sub-clusters — a small enough set for the semantic
pass in 3b to reason over without paging in the whole raw population.

```bash
# Find the reducer that shipped with this skill. Portable across
# install locations.
REDUCER=$(find ~/.cursor/plugins ~/.cursor/skills ~/.cursor/extensions . \
  -name reduce_spawns.py -path '*optimize-toolkit*' 2>/dev/null | head -1)
[ -z "$REDUCER" ] && {{ echo "reduce_spawns.py not found"; exit 1; }}

# Feed cluster_spawns response as JSON on stdin; get verb-bucketed JSON out.
python3 "$REDUCER" < /tmp/spawns_raw.json > /tmp/spawns_reduced.json
cat /tmp/spawns_reduced.json
```

Output shape: `{ input_spawns_raw, zero_signal_spawns, sub_cluster_count,
reduced_rows: [{verb, tool_shape, spawn_count, zero_signal_count,
enriched_spawn_count, avg_tokens_per_enriched_spawn, tools_seen,
unique_labels, top_labels[:8], tokens_total, session_count,
burst_count, sample_top_by_tokens}] }`. Rows come sorted by
`tokens_total` descending. `avg_tokens_per_enriched_spawn` and
`tools_seen` are what step 5's counterfactual-ratio and extract-vs-gap
mechanics read directly.

**Zero-signal rows are retained, not dropped.** A row where
`zero_signal_count == spawn_count` has no tokens/model on any member
(pre-enrichment sessions or tracker entries) — `tokens_total` reads 0
and `tool_shape` reads `<empty>` for these, so don't use them for
cost/tier math (adopt-savings, downgrade share, pin thresholds). But
their `top_labels` are real, often bespoke, self-narrated Task
descriptions — exactly the evidence the contrast-pair mechanic below
needs. Don't filter these rows out of your own reasoning just because
`tokens_total` is 0.

**Stage 3b — semantic cluster (your reasoning, in-context).** Read the
reducer's output and group verb buckets into meta-clusters by intent, not
by surface tokens. Verb buckets like `code`/`silent-failure`/`silent`/
`test`/`comment`/`type`/`review`/`independent`/`fresh-eyes`/`general` all
fold into one **Code Review** meta-cluster; `trace`/`research`/`investigate`
/`find`/`explore`/`map`/`inventory`/`reconcile` fold into one **Research
& Investigation** meta-cluster. Expected meta-clusters for a typical
engineer include: **Code Review**, **Research & Investigation**,
**Migration / Implementation**, **Testing & Validation**, **Planning /
Organization**. Not every meta-cluster surfaces for every user; only
name the ones that clear a real token/recurrence floor from the data
in front of you.

The semantic pass is **your judgment** — no server-side taxonomy. Use
the reducer's rich per-bucket evidence (top_labels, session_count,
tool_shape) to justify each grouping. A verb bucket that
doesn't cleanly fit any meta-cluster stays as its own single-bucket
meta-cluster; don't force-fit for symmetry.

Take the top **K = 3** meta-clusters by `tokens_total`. If fewer than 3
clear meta-clusters exist, present fewer — do not pad.

**Known coverage gap.** `cluster_spawns` only covers Task-tool subagent
spawns. Slash commands (visible in `my_toolkit_adoption.commands`)
never show up here — a heavy user of a release-command won't see a
"Releases" cluster no matter how the reducer runs. If the user asks
about command patterns, say so plainly and point at
`my_toolkit_adoption.commands` for the raw counts; don't fabricate a
cluster from spawn data alone.

### 4. Ground yourself in the user's real toolkit (0–2 file reads per meta-cluster)

Before picking a kind for a meta-cluster, look at what the user actually
has. `my_toolkit_adoption` lists capabilities by name and usage, but
"a name in a usage map" is not the same as "a file on disk that will
still be there next session." For each of the top-K meta-clusters:

- If `my_toolkit_adoption` shows an agent/skill whose name looks like
  it might cover the meta-cluster's dominant verb buckets +
  `tool_shape`, read the matching file under
  `.cursor/agents/` (or the user-scoped equivalent) to confirm the fit
  before recommending `adopt`. Bad `adopt` recommendations happen when
  the name matches but the actual scope doesn't. If the candidate is a
  **built-in** with no file on disk, use its description from your own
  tool listing instead — expected, not a reason to skip it (step 5).
- If `my_toolkit_adoption.mcp_servers` shows an entry with
  `err > 0.1 * n`, that's evidence for a `swap`/`pin` flag on the MCP
  side — check `~/.cursor/mcp.json` for how that server is configured
  before saying anything actionable about it.
- If you're considering `extract` (mint new), grep under
  `.cursor/agents/` for the meta-cluster's `tool_shape` — a
  similar-shape agent may already exist under a name your
  `my_toolkit_adoption` scan didn't surface.
- If you're considering `pin`/`downgrade`, locate the existing agent
  file so you can propose a **minimal edit** (change one `model` line)
  rather than overwriting the whole file.

Keep this bounded — one or two focused reads per meta-cluster, not a
full-repo sweep. If the file isn't obviously there, that's information
("adopt target's name matched but the file isn't under `.cursor/agents/` —
treat as evidence to soften the adopt pitch, or reframe as `gap`").

### 5. Pick the kind, from first principles

Call `outcomes__org_offered_tiers` once — this tells you the org's
actual `cheap` and `reasoning` model tiers. **Never suggest a model
the org isn't offered**; if a tier is `null`, that door is closed for
this org.

**Drill into sub-clusters — meta-clusters are framing, not the
recommendation unit.** The meta-cluster tells you the organizing shape
of an engineer's work (Research is heavy, Code Review is heavy); the
actionable play lives one level deeper, at the sub-cluster level: the
individual verb-bucket from the reducer with its specific
`top_labels`, `tool_shape`, `sample_top_by_tokens[].model`,
`session_count`, and `burst_count`. Pitching at the meta-cluster level
alone produces truisms ("mechanical work should run on Haiku") — the
non-obvious plays only appear when you compare a sub-cluster's shape
against `my_toolkit_adoption`.

**Strongest pattern (empirically): toolkit-consistency adopt, found via
contrast pairs — across every capability surface, not just agents.**
A contrast pair sets a named capability's aggregate usage (from
`my_toolkit_adoption`, the "routed" side) against a reducer sub-cluster
whose labels credibly describe the same domain but never name that
capability (the "bypassed" side). Do **not** look for the contrast
inside a single spawn's `tool_signature` — an `Agent`/`Skill` call
there reflects the *parent* turn's routing decision, not the child
subagent's own trace, and `tool_shape` (top-2 tools by frequency)
usually buries a lone `Agent`/`Skill` call under `Bash`/`Read` noise
anyway. Cross-data-source contrast is the mechanic that actually
surfaces evidence:

1. **Build the routed side from every `my_toolkit_adoption` surface,
   not just `agents`.** Keep any entry — agent, skill, command, or MCP
   server — clearing **≥10 spawns/invocations OR ≥1M `subtok`/`tok`**.
   This explicitly *includes* built-ins with no file on disk — a
   built-in clearing 200+ spawns is a real routed path, not "just the
   generic fallback." Also check `.skills` (≥5 invocations OR ≥100k
   `tok`) and `.commands` (≥5 invocations) the same way.
2. For each kept capability, build a small domain-vocabulary set from
   its real behavior: `Read` the `description:` frontmatter for a
   file-backed agent/skill, or use the description string your own
   tool listing carries for a built-in with no file on disk.
3. Walk the reducer's `reduced_rows` — **including rows where
   `zero_signal_count == spawn_count`** (the reducer retains these;
   their `top_labels` are real evidence, often the *strongest* signal
   precisely because they're bespoke, self-narrated Task descriptions
   rather than toolkit boilerplate). Flag a row as a bypass candidate
   when its `top_labels` share ≥2 content words with a kept
   capability's domain vocabulary. Also check `.tool_counts` for
   recurring native-tool patterns echoing a sub-cluster's `tool_shape`.
4. Require **≥2 spawns**, same session or sessions within the window.
   A burst under a few minutes (check `at` timestamps) is a stronger
   tell than a single spawn. Bonus confidence: the same verb-phrase
   recurring across sessions with only the trailing subject varying.
5. **Compute the counterfactual ratio.** Routed-side avg = the
   capability's own `subtok / n` (agents) or `tok / n`
   (skills/commands). Bypassed-side avg = the sub-cluster's
   `avg_tokens_per_enriched_spawn` (from the reducer, divided by
   non-zero-signal spawns only). Ratio = bypassed avg / routed avg.
   **≥3x is a real magnitude signal** worth stating out loud even
   with no cohort/$ pricing.
6. State the caveat honestly: `cluster_spawns` never exposes
   `subagent_type`, so you cannot prove the bypassed spawns did NOT
   route through the named capability — only that their labels don't
   mention it. Pitch it as "these don't reference `<capability>` and
   look hand-rolled" rather than "you didn't use `<capability>`."

Once you have a pair, the play is `adopt` — the user has the
capability, they're inconsistently reaching for it.

**Extract vs gap — where the bar actually sits.** `gap` is not the
default fallback when there's no adopt target. A single sub-cluster
row clearing **≥2 spawns** with a non-empty `tool_shape` is `extract`.
If sibling rows share the same verb but split across `tool_shape`
buckets purely from top-2 noise (check `tools_seen` — the full tool
set per row; near-identical `tools_seen` across rows means the same
underlying job), **combine their spawn_count** before applying the ≥2
bar. Only fall to `gap` when, after combining, the surviving
`tools_seen` sets share fewer than 2 tool names, or no combination
clears 2 spawns.

**Tie-break — adopt beats downgrade when both fire.** If a sub-cluster
is both `adopt`-covered (an existing agent handles this shape) AND
downgrade-shaped (running on reasoning tier with a mechanical
tool_signature), pitch `adopt` alone. The existing agent's own model
config is a separate concern; routing the work to the agent captures
the primary win. Do not double-recommend.

For each of the top-K meta-clusters, reason about kind from the
evidence in front of you (the semantic cluster label, its member verb
buckets, its dominant tool shapes, its token magnitude, and the
`my_toolkit_adoption` match you just grounded). There is no server-side kind gate — you pick, you
justify. **None of these kinds are agent-only** — every kind below can
target an agent, a skill, or an MCP server. The five buildable kinds
and one signal-only kind:

- **`adopt`** — the cluster's `tool_signature` and label overlap an
  existing capability you saw in step 3 (agent or skill). Recommend
  the user reach for the existing thing consistently, no new file.
  Softer signal: the user's own `my_toolkit_adoption` shows the
  target with meaningful usage already.
- **`swap`** — cluster overlaps an existing capability, but that
  existing capability is the wrong shape or wrong model for this
  work — or, for an MCP server, the wrong integration for the job.
  Recommend replacing it. Harder to justify than `adopt`; state the
  reason ("existing agent is pinned to a reasoning tier but the
  tool_signature is mechanical — swap it for a cheap-tier variant").
  MCP-server candidates surface from `.mcp_servers`: an entry with
  `err > 0.1 * n` is worth flagging, but a swap needs an alternative
  to point at — absent cross-org data, you usually can't name one; say
  so and skip the artifact rather than force a `swap`.
- **`pin`** — cluster runs on a mix of tiers with the org's cheap
  tier already carrying meaningful share (say, ≥30% of the
  cluster's `subagent_model` occurrences) and no evidence the
  reasoning tier is load-bearing. Recommend pinning the existing
  capability to the cheap tier. Requires you to have located an
  existing file to edit.
- **`downgrade`** — cluster runs predominantly on the reasoning
  tier (say, ≥50% share) but the `tool_signature` looks mechanical
  (`jaccard_within` ≥ 0.6, small tool set). Recommend re-tiering
  the existing capability down. Same file-locate requirement as
  `pin`.
- **`extract`** — a recurring inline pattern with no existing named
  capability to point at. Mint a new sub-agent. This is the one
  kind where you author a full new `.cursor/agents/<name>.md` file
  from scratch. Requires you to name the capability and derive its
  `tools:` allowlist from the cluster's `tool_signature`.
- **`consolidate`** — two clusters look like the same underlying
  job under two different labels. No new file; recommend merging.
  Present conversationally, don't auto-locate the files.
- **`gap`** (signal only, no artifact) — cluster is real recurring
  work, but you cannot pick a kind honestly even after the "Extract
  vs gap" combining step above. Say so plainly, no artifact, no
  confirmation question, no `estimate_savings` call for this
  candidate. Not every capability surface produces a play from a
  given window — that's a quiet window, not a mechanic failure.

Thresholds above are rules of thumb, not gates. Adjust when the
cluster's specifics clearly warrant.

### 6. Estimate savings (1 call per non-`gap` candidate)

For each candidate that isn't `gap`, call
`outcomes__estimate_savings` with the cluster's `total_tokens`,
per-component tokens if you can derive them, `current_model` (the
dominant `subagent_model` across cluster members), and `target_tier`.
Read `estimate.assumptions.placeholder_output_ratio` /
`placeholder_cache_ratio` and `estimate.estimate` on every response —
see **Placeholder savings, honestly** below.

### 7. Author + Present (no tool call; you write the artifact)

For each candidate, author the artifact yourself from first
principles before asking for confirmation. Per kind:

**`extract` — new file at `.cursor/agents/<name>.md`.** You author:

- `name`: kebab-case, `^[a-z][a-z0-9-]*$`, derived from the
  cluster's `representative_label`. If a same-name file already
  exists in `.cursor/agents/`, disambiguate rather than overwrite.
- `description`: one sentence stating what work this handles and
  when Cursor should delegate to it. Ground it in the cluster's
  actual observed work — reference the tool_signature's dominant
  tools and the label's phrasing. Not a template.
- `tools`: derived from the cluster's `tool_signature`. Include the
  tools that account for the mass of calls, drop the long tail. If
  the tool_signature is empty, omit the `tools:` line.
- `model`: the `target_model_id` from `org_offered_tiers` for the
  tier you picked in step 5.
- Body: a short (≤10 line) system prompt describing the role,
  grounded in this cluster's specifics.

**`pin`/`downgrade` — edit an existing file at `.cursor/agents/<name>.md`.**
The meaningful change is one frontmatter line:
`model: <target_model_id>`. Author the dry-run as a one-field edit,
not a full-file replacement.

**`adopt` — usually no file.** Author a plain-language
recommendation: "stop spawning this inline pattern; your existing
`<capability>` already handles it — I saw N sessions where it would
have applied," plus the counterfactual ratio from step 5. No
confirmation-to-write question. **Never offer a settings-entry or
other prose-nudge note as a fallback** — not on request. A
recommendation with no clean capability-level artifact is signal you
present honestly and move on.

**`swap` — edit or replace an existing file** (`.cursor/agents/*.md`
or `~/.cursor/mcp.json` for an MCP-server swap). Author the dry-run
as the specific edit you'd propose.

**`consolidate` — no automated file work.** Present the two-candidate
overlap and ask the user which capability should absorb the other.

**No clean capability-level intervention exists.** Some patterns don't
map onto `.cursor/agents/*.md` or `~/.cursor/mcp.json` at all — an
MCP-server error-rate flag with no alternative to name is the clearest
example. Say so plainly and stop; never substitute a settings-note
nudge instead.

**`gap` — no artifact.** State the pattern, call `mark` with
`status: "presented"` and `proposed_kind: "gap"`; skip the
write/confirmation loop.

**Present** the authored artifact with:

- The evidence summary in plain language.
- The `matching_sessions` slice from the cluster if present.
- The savings figure, honestly caveated per placeholder rules.
- A plain confirmation question ("write this to
  `.cursor/agents/<name>.md`? yes/no"). One candidate at a time.

### 8. Write (only on explicit confirmation)

**Validate the target-file basename before anything else.** Reject
if the name (a) contains `/` or `\`, (b) contains `..`, or (c)
doesn't match kebab-case `^[a-z][a-z0-9-]*$`. On rejection, surface
the value verbatim to the user with the specific reason and stop.

Only write after an explicit "yes"-shaped answer in the
conversation. No write on silence, hedging, or topic change. After
writing, tell the user this **takes effect next session** — never
imply the current conversation just changed. Consent, revert, and
distribution are git.

### 9. Mark (1 call per candidate you presented)

**Exactly one `mark` call per candidate.** Pick from:

- `status: "accepted"` — confirmed and written.
- `status: "dismissed"` — **explicit refusal only**. Ask one short
  follow-up and forward the answer verbatim as `reason` (cap ~200
  chars).
- `status: "presented"` — shown, no decision either way. **Never
  auto-dismiss on non-confirmation.**

Use `action: { kind: "cluster", cluster_id, proposed_kind }`. When
the candidate is `extract`/`pin`/`downgrade`/`swap`/`consolidate`
and you authored an artifact, pass `agent_spec_md` (your authored
body, verbatim) and `est_savings_low_usd` /
`est_savings_high_usd` (from `estimate_savings`) so the ledger row
isn't lossy. For `adopt` with no file written, and for `gap`, omit
those fields.

Mark is best-effort: if the call fails, don't error the
conversation over it.

## Failure handling for non-`mark` tools

`mark` is the one tool that follows the silent-log rule — every
other tool is on the hard-stop rule. If any of `my_turn_pattern`,
`my_toolkit_adoption`, `cluster_spawns`, `org_offered_tiers`, or
`estimate_savings` returns `503`, `400`, or an empty result set
where the flow depends on at least one row, **surface the error
verbatim, stop the flow, do not retry**.

## Placeholder savings, honestly

Every `outcomes__estimate_savings` response carries fields that
exist specifically so you don't overstate a number:

- `estimate: "no_cohort_catalog_only"` means catalog pricing math
  only, not cohort-validated. Say so: "treat it as a ceiling, not a
  promise."
- `assumptions.placeholder_output_ratio` /
  `placeholder_cache_ratio` set to `true` mean typical-ratio
  fallback. Say "estimated within a wide band" rather than a bare
  point figure.
- **`current_cost_usd: null` — dominant model not priced in this
  org's catalog.** If the sub-cluster's dominant model (e.g.,
  `claude-opus-4-8`) isn't in `org_offered_tiers.all`,
  `estimate_savings` returns `current_cost_usd: null` and
  `savings_high/low_usd: 0`. Do not quote a $ figure. Pitch the play
  on consistency or shape grounds ("mechanical tool-use pattern, better
  routed through <existing agent>") — the savings surface just isn't
  usable in this org for this model.

## What not to do

- Don't spawn a sub-agent to do independent investigation.
- Don't invent a cohort comparison when a tool response says
  there isn't one.
- Don't write anything without an explicit "yes" in this
  conversation.
- Don't auto-invoke yourself opportunistically. Cursor has no
  hard invocation gate the way the claude adapter's
  `disable-model-invocation: true` provides — treat this prose
  rule as load-bearing. Only run this flow when the user
  explicitly asks.
- Don't paper over a bad pick with a plausibly-worded artifact.
  If you find yourself writing template-shaped prose, reclassify
  as `gap` and say so.
- Don't reach for `gap` just because a sub-cluster's exact
  `tool_shape` row only has one spawn — check whether combining
  same-verb sibling rows by `tools_seen` overlap changes the count
  first.
- Don't offer a settings-entry or other prose-nudge note as a
  fallback artifact — for any kind, on request or not.
- Don't exceed the ~10-call budget on `outcomes__*` tools.
