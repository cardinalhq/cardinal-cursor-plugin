# Checks — the guided flow

Follow the steps in order. Each step says what the user sees and what you do with the tools.
Speak in terms of *checks*, *contracts*, *examples* and *results*. Keep tool mechanics out of
the conversation unless the user asks.

Tools are named here without their prefix: the query tools are Cardinal's (`cardinal`
server), the check tools are `cardinal-behavior`'s. Your SKILL.md gives the full names on
this agent, how you wait for a range run, and how you read the files the tools name. The
`cardinal-behavior` server runs outside any agent sandbox, so its tools record labels and
read range runs without shell commands.

## 1. Route: can a query answer it?

Answer with the query tools and stop when one or a few aggregate queries answer the
question: counts, rates, error ratios, top-N, latency percentiles, "which endpoint
contributes most". Do not mention checks, or say in one plain sentence that a check would
add nothing here.

A check is needed only when every trace needs its own judgment:
- relationships between events inside one trace (this span descends from that one, this
  happened before that one ended);
- reading what was said (an agent's answer compared with what its tools returned);
- a rule the user wants to keep and run again.

When a check is needed, say why in one sentence and ask whether to go ahead. Continuous
watching ("keep an eye on…") is not part of this preview. Offer to run the check over a
past range now and to rerun it on request.

## 2. Scope

1. Resolve the service with `list_services` / `list_span_field_values` and pin the time
   range to exact UTC bounds. Confirm both with the user when the request leaves either one open.
2. Check that the service has spans in the range. Executions reach only the production
   LakeRunner: a service seen only on another instance cannot be checked. Say so and stop.
3. Call `get_behavior_sdk` and read the real sources it writes to `sdk_directory` (profile,
   `run.events`, `event.attrs`, Recorder, judge API, CompilePlan schema, examples).
   - A program sees **every span of the trace, from every service**, as `run.events`.
     The population service only decides which traces are selected.
     The service is in `event.attrs["service_name"]`.
4. Confirm with the query tools that the spans carry the evidence the rule needs (the
   attribute, the message content, the status, the parent links). If they do not, tell the
   user what is missing and stop. Never fall back to guessing.

## 3. Contract

Show the decision contract in plain language and get the user's explicit confirmation:

```
Check: <one-line name>                         Service: <service>
Applies to:          <which traces; others are NOT_APPLICABLE>
Flag (MATCH) when:   <the condition>
Clean (NON_MATCH) only when: <the evidence that must be present>
Can't tell (UNKNOWN) when:   <evidence missing / budget exceeded / judge unsure>
Decided by:          rules only | rules + judge  (judge-based checks are Experimental)
```

- Prefer rules. Use the judge only for a genuinely semantic question (what a message
  claims, whether a statement is supported). If you use it, label the check
  **Experimental** here and everywhere results appear.
- **No causal language** anywhere: not in contracts, reasons or results. Do not write
  "caused", "because of", "due to", "led to" or "root cause". A check finds evidence that
  things co-occur in a defined structural relationship. If the user asks about causation,
  say the check reports association only, and that causation would need a defined
  attribution rule with its own evidence, which this preview does not provide.
- The confirmed contract text becomes the `description` of `compile_behavior`. The exact
  description plus the service defines the check's development scope. Keep it stable
  while revising the program. **Changing the contract starts a new scope and a new draft.**

## 4. Author

Write the program and compile plan from the SDK you read. Show no Python unless asked.
Lessons that are not optional:

- **Missing evidence means UNKNOWN, never NON_MATCH.** Use the agent's completeness signal
  where one exists (for Megan, `megan_operation_manifest`: returned, no open spans, no
  gaps, nothing dropped, content captured). NON_MATCH needs positive evidence.
- **Judge questions are short and plain,** with two or three evidence items. Long questions
  that list exclusions produced near-random answers.
- **One claims-level judge question, not a judge call per tool call.** Looping the judge
  over every call turned real violations into UNKNOWN when the decision budget ran out.
- **Exception handlers may only re-raise;** the runtime owns ERROR.
- **`Decision` is not truthy:** read `.yes`, `.no` and `.uncertain` explicitly.
- **`run.trace.trace_id`, not `run.trace_id`.**
- **Claude Code telemetry:** only Bash calls record full arguments. Background model calls
  also issue tool calls, so do not treat every tool call as the user's turn.
- Structural rules (timeouts, failures, parent and child, timing) are mechanical. Read
  status codes, error messages and parent span IDs from `event.attrs`. Express descent by
  walking parent links. Express "ends no later than" by comparing end timestamps.

## 5. Find examples

Use `execute_spans_query` and the related query tools over **narrow windows of 10 minutes
or less** (wider teaching windows often exceed the 256 MiB selection limit). Pick at most
**8** real traces that cover likely MATCH, NON_MATCH and UNKNOWN cases, plus at least one
out-of-scope trace for NOT_APPLICABLE. All of them must fall inside one teaching window of
at most an hour (`test_behavior` takes one window). Note each trace's window.

Pick a draft id for this contract, e.g. `<service>-<short-name>-v1`. Bump the suffix when the
contract changes.

## 6. Evidence sufficiency (mechanical, no user decision)

Before showing any example, check each candidate against this contract with the query tools:
- the agent's completeness signal, where it has one, is clean;
- the spans the contract depends on were retained;
- the decisive values come from the trace itself, not from your reconstruction.

Record every candidate with `set_check_example`:

```
set_check_example {draft: <id>, trace_id: <trace_id>, evidence: "sufficient",
                   proposed: MATCH|NON_MATCH|UNKNOWN|NOT_APPLICABLE, reason: "<why, under the contract>"}
set_check_example {draft: <id>, trace_id: <trace_id>, evidence: "insufficient",
                   proposed: "UNKNOWN", missing: "<the evidence that is not there>", reason: "<why>"}
```

`remove_check_example {draft, trace_id}` drops a candidate (a mistyped trace ID, a
swapped-out example) until the labels are locked. `get_check_draft {draft}` shows what is
recorded.

An insufficient candidate is **UNKNOWN by construction**. A label cannot create evidence the
trace does not have. Keep it as an UNKNOWN example (useful teaching), or swap in another candidate.

## 7. Approve labels

For each example with sufficient evidence, show the key evidence quoted from its spans
(service, span name, the decisive attribute or text), your proposed classification under
the approved contract, and the reason. Then ask the user to approve or correct each one.

- The contract approval (step 3) settled *what counts*. This approval settles *how the
  contract applies to this trace*.
- Record each decision only after the user gave it, with
  `approve_check_example {draft, trace_id[, label: <correction>][, user_reply: <their words>]}`;
  `user_reply` quotes the user's approving or correcting reply verbatim. The ledger records
  how and when each label was approved.
  The ledger **refuses** a correction that conflicts with the sufficiency result, such as
  MATCH on a trace whose deciding span wasn't retained. Tell the user the refusal reason
  as it was given.
- A correction that shows **the contract itself is wrong** goes back to step 3 to revise
  the contract under a new draft id. It must not become a one-off label.
- Approve everything before the first test.
  `lock_check_examples {draft}` returns `expected_verdicts` and locks the labels. The
  development suite refuses a changed label for a trace it has already tested.

## 8. Teach and revise

1. `compile_behavior` (description = the contract, service, source, plan), then
   `test_behavior` with the example trace IDs, their window and the `expected_verdicts`.
2. **The program disagrees with an approved label:** the program is wrong. Show the
   disagreement, revise the source, compile again with the **same** description, and retest.
   Stop after 3 rounds and report what still disagrees.
3. **The runtime found missing evidence that step 6 missed** (UNKNOWN with coverage gaps on
   an example labelled MATCH or NON_MATCH): the label is wrong. Explain it to the user.
   Fixing that label needs a new draft: restate the contract so the missing-evidence clause
   is explicit, then carry the other approved labels over with this one corrected to UNKNOWN.
   Never change the program to reach a verdict the evidence doesn't support.
4. `run_behavior_regressions`, then `accept_behavior`. A failure at any step leaves the
   previously accepted version in place; tell the user that.

## 9. Run over the requested range

The run happens in the background. You follow its events and read its results; you never
rerun windows yourself.

1. `execute_behavior_range` with the accepted version, the service and the exact range. It
   returns at once with `range_id`, `events_file`, how to follow the run (`watch_command`
   or `poll`) and `next_action`.
2. Follow the run the way your SKILL.md says. Each line is one event: a window done with
   its verdict counts, a window split (too large, or its results couldn't be read), a
   retry, and so on. Following ends on `needs_extents`, `completed`, `failed` or
   `stalled`. Keep the user posted in a line or two as windows finish. Don't narrate
   every event.
3. **`needs_extents`**: traces that cross a window boundary may have been judged on part
   of their spans. Take `multi_window_traces` and `boundaries` from the event
   (`get_behavior_range` returns them as `needs_extents`; it is also the last line of
   `events_file`). Use the spans query tools to measure the span extent (earliest start,
   latest end, across every service) of each listed trace, and of each trace of this
   service with spans on both sides of a boundary. Look a few minutes either side of
   each boundary. Call `continue_behavior_range` with `trace_extents`
   `{trace_id: {start, end}}`, or `{}` when none cross, then follow the run again. Edge traces
   without an extent become UNKNOWN (`window-edge`). Say so.
4. **`stalled`**: the worker stopped. `continue_behavior_range` restarts it from its saved
   state, then follow the run again. **`failed`**: report the error verbatim and stop.
5. **`completed`**: read the summary (bounded): `get_behavior_range` returns it as
   `summary`; it is also `summary_file`. Only open `result_file` (every raw per-window
   verdict plus the canonical column) for the details you need.

## 10. Results

Show, in this order:

1. **Experimental banner** first, for a judge-based check (the result's `experimental` field).
2. **Counts** for all five verdicts (MATCH, NON_MATCH, UNKNOWN, NOT_APPLICABLE, ERROR) over
   `population` traces, the exact range, and the service.
3. **UNKNOWN reasons**, grouped with their counts (`unknown_reasons`).
4. **Flagged traces**: trace ID, the reason, and the witness span IDs. Give links only where a
   tool returned one. `get_behavior_execution` with the `execution_id` and trace shows a
   trace's judge receipts.
5. **Edge traces and conflicts**: how many edge traces there were, how many conflicts, and
   for each conflict its per-window raw verdicts next to its canonical verdict.
6. **Coverage holes**: windows that failed or stayed too large even when narrowed. Those
   minutes were not evaluated.
7. **Judge usage**, as reported: calls, tokens, and cost or "not available"
   (`judge_usage`). It must be 0 calls for a rules-only check. Never estimate.

Everything per window, raw, is in `result_file`; the run's event log is `events_file`. If you checkpoint the investigation, cite
the evidence ids of the tool results.

## 11. Storyboard

`render_storyboard` renders one completed execution at a time (one window). Render it for the
executions that hold flagged traces, using `execution_id` from `flagged`, and give the
files it returns. It cannot draw a whole range, edge canonicalization or judge usage; say so
in plain text instead of inventing a visual. Updating or publishing the session's Cardinal
storyboard is the storyboard skill's job, and only when the user asks.

## 12. Reuse

Before step 3, call `list_accepted_checks {service_name: <service>}`. If a check with the same
contract is already accepted, show its contract, confirm it still matches the request, and go
straight to step 9 with its `diagnostic_version`. An accepted version runs only on the service
it was accepted for. Running it on another service is refused. Build a new check for the new
service instead.
