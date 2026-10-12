---
name: cardinal-checks
description: Build and run a Cardinal check (preview) — a reusable, evidence-backed decision made for every trace in a time range, with a contract the user approves, approved examples, and per-trace results with evidence. Use when answering needs a judgment about each trace that one aggregate query cannot give — "how often does X happen", "find traces where…", "flag any…", "check whether … for <time range>" — such as relationships between events inside one trace, what an agent said versus what its tools returned, or a rule the user wants to reuse. Do not use for counts, error rates, top endpoints or latency; answer those with the Cardinal query tools.
---

# checks — per-trace decisions over telemetry (preview)

A **check** decides, for every trace of one service in a time range, whether it matches a
plain-language rule, and shows the evidence for each decision. Users never see the words
"Behavioral Program", "DiagnosticVersion" or "UDF", and never type a trace ID, write Python
or call a developer tool: you do all of that with the tools named below.

**Read [CORE.md](CORE.md) in full before the first step.** It is the guided flow (route,
scope, contract, author, examples, evidence sufficiency, labels, teach, run, results,
storyboard, reuse) and the lessons that keep checks honest. It names tools without their
prefix; the full names on Cursor are below.

## Tools this skill drives

- Cardinal query tools (the `cardinal` server's `lakerunner__*` tools, instance `prod` unless the user names another):
  services, field values, spans, logs. Use them to answer directly, to scope, to find
  example traces and to measure trace extents.
- Check tools (the `cardinal-behavior` server's tools):
  `get_behavior_sdk`, `compile_behavior`, `test_behavior`, `inspect_behavior`,
  `get_behavior_compilation`, `run_behavior_regressions`, `accept_behavior`,
  `execute_behavior_range` / `continue_behavior_range` / `get_behavior_range` (any time
  range, run in the background), `get_behavior_execution`, `render_storyboard`.
  `execute_behavior` / `next_behavior_result` stay available for a single window of at most
  one hour.
- The teaching-label ledger and reuse, on the same server: `set_check_example`,
  `approve_check_example`, `remove_check_example`, `get_check_draft`,
  `lock_check_examples`, `list_accepted_checks`. The server runs outside Cursor's Auto-review sandbox, so
  these record labels without a shell command or a sandbox prompt; the user's label approval
  (step 7) is still required.

Check executions only reach the organization's production LakeRunner. A service that only
exists on another instance (for example `otel-demo`) cannot be checked: say so and stop.

## Cursor specifics

- **Waiting for a range run: poll, don't sleep.** After `execute_behavior_range` (or
  `continue_behavior_range`), call `get_behavior_range` with the `range_id`, `after_event`
  (0 first, then the previous call's `next_event`) and `wait_seconds` 20. Each call waits up
  to 20 s for new events and returns one line per event. Keep calling it until `done` is
  true, then do what `next_action` says. Never run `sleep` or a shell loop for this.
- `get_behavior_range` returns a completed run's `summary` and the `needs_extents` details,
  so you rarely need the files. Other long results land in files the tools name
  (`receipt_file`, `result_file`) under `~/.cardinal/behavior-executions/`; read them with
  the file-read tool.
- The user turns the preview on or off in their own terminal with this plugin's
  `scripts/cardinal-checks on|off`. Never run it yourself. A change takes effect at the
  next Cursor session: Cursor has no skill reload, so a running session keeps the skill it
  started with.
