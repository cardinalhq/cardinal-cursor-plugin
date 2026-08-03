# Spike findings — DAG harvest from real Claude Code sessions

Ran a ~300 LOC heuristic-only harness against three sessions with contrasting shapes. Every number below is empirical, not from spec. What the heuristic could and couldn't do is what makes this document worth keeping.

**A note for non-Claude adapter readers.** This document ships identically to every adapter's `skills/mechanize/` (Claude, Codex, Cursor, Gemini) because the *findings* are transcript-format-agnostic — F1 (semantic edges, not syntactic), F5 (local-only tools break reuse), F7, F9 apply to every agent. The *examples and tool names* are Claude-specific because that's the corpus the harness ran against: `Bash` (F2), `~/.claude/` (F3), `Read`/`Write`/`Edit` (F5), and the "Kept in the plan as-is" bullets naming the Claude adapter. When you're reading this from a Codex/Cursor/Gemini adapter, translate the tool names to your adapter's equivalents (see your `SKILL.md`'s Stage 2 addendum for the mapping); the underlying pattern is the same.

## Sessions

| Label | Domain | Tools | Edges (token) | Dead ends | Atts | Shape |
|---|---|---|---|---|---|---|
| A | Lakerunner "worklane depth spike" investigation | 21 | 80 | 6 | 1 (PNG) | observability investigation |
| B | Python plugin cache-path debugging | 14 | 49 | 2 | 0 | local debugging investigation |
| C | Omnigent PR rebase + review reply | 26 | 109 | 4 | 0 | task execution, NOT investigation |

Full per-session outputs: `out/session-<A|B|C>-{report.md,naive-dag.yaml}`.

## The single most important finding

**Zero "strong" edges across all three sessions.** Not one instance of a tool_use input being a verbatim substring (>=12 chars) of a prior tool_result. The model always *synthesizes* next-step commands from prior context — it never copies. This one number tells us the compiler cannot rely on syntactic linking. Every non-trivial edge is semantic: the model read the result, understood it, and constructed the next input using that understanding.

Consequence: **the compiler's edge-inference layer needs either token-rarity weighting (TF-IDF style) or LLM-assisted attribution.** Simple substring matching is a dead end at this abstraction level.

## Findings that reshape phase 1

### F1 — Token matching finds real edges but is noisy

Token-level matching (>=6 char tokens, split on `[^A-Za-z0-9_./]+`) found edges. Real edges: rare identifiers like `claimWaitHist`, `internal/worklane/telemetry.go`, `f96bd72`. Noise edges: domain vocabulary like `lakerunner`, `omnigent`, `context`, `metric`, and — worst offender — the operator's cwd prefix `<operator-workspace>/`.

**Fix for the real compiler:**
- Compute per-session token frequency; downweight tokens appearing in >30% of tool inputs/results.
- Strip environment-specific prefixes before tokenizing (`/Users/<user>/`, `~/`, hostnames, org names).
- Keep a per-session "domain vocabulary" list from the objective and treat matches on those tokens as evidence of topic continuity, not causal linkage.

### F2 — Bash-tool `description` fields pollute the input signal

The Bash tool has a required `description` field ("Fetch PR review comment", "List codex adapter"). My heuristic tokenized these as inputs — they polluted both the edge-inference and parameterization-candidate outputs. The `description` field is human-oriented labeling, not tool input.

**Fix:** the adapter must carry per-tool-name field-role metadata. For `Bash`, the semantic input is `command` only; `description` is annotation. Same problem will exist for any tool with mixed semantic/annotation fields.

### F3 — Meta-tool calls exist and pollute the investigation

Session A's tools #20 and #21 were `jq -r '.summary' ~/.claude/projects/.../session.jsonl` — the operator prodding the *session file itself* AFTER the investigation was concluded. These are not part of the investigation. The heuristic correctly flagged them as dead ends but had no way to know they were meta.

**Fix:** filter out tool calls whose inputs reference `~/.claude/` paths, or that occur after the last substantive assistant message. This needs a spec rule (§29 stage 3 should list "meta-tool calls" as an INCIDENTAL category).

### F4 — Task-execution sessions look structurally identical to investigations

Session C ("respond to reviewer and rebase branch") has 26 tools, 109 inferred edges, a clear objective, and a clean conclusion. Structurally indistinguishable from A or B. But it is not an investigation — it is task execution, and a compiled Sentinel from it would be a "run these git and gh commands" playbook, not a reusable procedure. §40's negative-reuse case is real and unavoidable.

**Fix:** the compiler needs a **conclusion-shape classifier** before deciding to produce a Sentinel. Investigation conclusions classify or explain ("the number is stuck because..."). Task conclusions report actions ("Done. Rebased X, replied to Y."). Cheap heuristic: check the first sentence of the conclusion for verb-form (past-tense action verbs → task; present-tense stative/classifying verbs → investigation). If task, refuse compilation with a clear message — do not silently produce a fake Sentinel.

### F5 — `Bash` is opaque; it must be decomposed into synthetic capabilities

Across the three sessions, `Bash` calls did: `grep`, `kubectl`, `git`, `gh`, `jq`, `find`, `cat`, `ls`, `mv`. Every one of these is a *different* capability from a Sentinel-portability POV (§10). Bucketing them all under `toolRef: Bash` is worthless.

**Fix:** the adapter must decompose Bash by `argv[0]` and emit synthetic capability IDs like `bash.grep`, `bash.kubectl`, `bash.git`, `bash.gh`. For capability binding purposes, `bash.gh` needs to bind to something GitHub-shaped (GitHub CLI or MCP), not to "a bash shell." Same problem will exist for `Read`, `Write`, `Edit` — those are all Claude Code local-only unless replaced.

### F6 — Attachment extraction works on real data

Session A's opening image was correctly identified: kind=`image`, mime=`image/png`, sizeBytes=342856, digest=`sha256:33020f7e704a1493...`, referenced at event #7. No decoding, no base64 inlining, no generated description. §28 and §28.1 as-spec'd work end-to-end for the case we tested.

**Kept in the plan as-is.** WP-3's exit criterion for attachment handling is exercised by the golden fixture.

### F7 — Conclusion → node output attribution is unreachable via heuristics

Session A's conclusion cites specific numbers (`20136 ms`, `179 minute-buckets`, `LevelMaxAgeCap`) that came from tool #14 or #15's output. My heuristic has no way to map those numbers back to their producing nodes. This is genuinely the hardest compiler problem exposed by the spike.

**Fix:** the compiler needs LLM-assisted attribution — feed the conclusion + each candidate producing node's output and ask which node produced which cited fact. There is no cheap heuristic for this. Budget this as the largest single stage in the phase 2 compiler.

### F8 — Dead ends are useful signal, not noise

In sessions A and B, dead-end tool calls are the *exploratory* steps (grep for something, found nothing, moved on). §29 stage 3's EXPLORATORY classification is exactly this. The heuristic already surfaces them cleanly.

**Kept in the plan as-is.** The compiler audit log must preserve the fact that these existed, even though they are excluded from the DAG — otherwise reviewers can't tell the compiler considered and rejected them.

### F9 — Parameterization heuristic ("literal appears once = input") is too weak

The "literals appearing once" list is dominated by Bash `description` field values ("Read connect script"), which are annotations, not inputs. Filtering to only `command`-field values would help but still isn't enough — a grep pattern like `worklane_claim_wait_ms` is unique (appears once) but is a *constant of the investigation domain*, not a parameterizable input. The distinction between "unique-but-domain-constant" and "unique-because-it's-the-input-value" needs semantic judgment.

**Fix:** parameterization decisions need LLM-assisted classification. Signal to feed the classifier: is this string identifiable as a known-service name, environment name, ID, or time range (input-shaped)? Or is it a code identifier, metric name, or file path from the investigation itself (constant-shaped)?

## What phase 1 should stop trying to do

Based on the spike, phase 1's plan overreaches in one specific way: it assumes a **hand-authored** Sentinel from a real capture is a reasonable exercise. It is — but only for A-shaped sessions (investigation). Attempting the same exercise on a C-shaped session would produce a Sentinel that is really a shell script wearing a DAG hat.

**Phase 1 amendment:** the golden fixture (Session A) is correct. But WP-12's integration test should also include a **negative case** — attempt to compile Session C and verify the (yet-to-exist) classifier refuses. That way the negative-reuse discipline of §40 is baked into phase 1's exit gate, not deferred to phase 3.

## What phase 1 should absolutely keep

- Harvester + Claude Code adapter as specified.
- Attachment handling as specified (§28, §28.1, §28.2 amendments — validated on real data).
- Redaction module in `cardinal_core.redaction`.
- Executor with function, condition, emit nodes.
- Replay bundle.
- Hand-authored Sentinel from Session A as the golden.

## What phase 1's plan should acquire from this spike

Add these WP-3 adapter rules (validated by the spike):

1. **Per-tool field-role metadata.** For each known tool name, declare which fields are semantic input vs annotation vs description. Store as a table; ship one entry each for Bash, Read, Write, Edit, Grep, Glob, and each MCP tool this repo cares about.
2. **cwd/hostname stripping.** Before emitting CaptureEvents, strip absolute paths matching `/Users/<user>/` or `~/` down to project-relative paths.
3. **Meta-tool filter.** Tool calls whose inputs reference paths under `~/.claude/projects/` or that occur after the terminal assistant message are marked with `isMetaCall: true`. Included in the event stream, excluded from compilation.
4. **Bash decomposition.** Each Bash tool call is enriched with a synthetic `capabilityId` derived from `argv[0]` (e.g. `bash.grep`, `bash.kubectl`). The raw `toolRef: Bash` is preserved; the synthetic ID is what the compiler uses.

Add a WP-12 requirement:

5. **Negative-case fixture.** Session C is committed as a second fixture. The phase-1 exit gate includes an assertion that a (stub) `should_compile()` classifier returns `false` for Session C. The classifier can be a simple regex-based heuristic in phase 1 — the point is that the surface exists, not that it's smart.

## What this spike proves about the spec

The spec was structurally correct. Every finding above amends *implementation guidance*, not the spec's shape:

- §28 CaptureEvent schema — unchanged, plus attachment amendments already made in this session — validated
- §28.1 adapter contract — needs one added rule (per-tool field-role metadata); otherwise correct
- §28.2 redaction — unchanged, needs the `/Users/<user>/` prefix rule added to the redaction table
- §29 stage 3 (causal contribution) — needs INCIDENTAL to explicitly include "meta-tool calls"
- §40 (negative reuse) — validated as necessary, not optional — phase 1 must include a classifier stub

## Wall time to produce this evidence

~2 hours end-to-end (writing harness + 2 iteration cycles on the heuristic + writing this doc). Cost of doing this before committing to the phase 1 plan: negligible. Cost of *not* doing it: we would have shipped a phase 1 that had no answer for Bash decomposition, meta-tool contamination, or the negative case.
