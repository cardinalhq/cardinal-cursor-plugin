---
name: cardinal-mechanize
description: Compile a completed Cursor session (a past investigation) into a candidate Sentinel DAG plus rationale — a reusable procedure that could later be executed against a similar problem. Use when the user asks to /mechanize, compile a session, or extract a reusable investigation procedure. Spike-quality; produces YAML + rationale, does not execute anything.
---

# mechanize (Cursor) — compile a Cursor session into a Sentinel DAG

**Spike-quality compiler.** Produces a candidate `sentinel.yaml` + `rationale.md` from a past investigation session. Does NOT execute the Sentinel; that's a separate executor. Does NOT ship — this is exploratory work, and the rationale is where the honesty lives.

This SKILL.md is the **Cursor-specific** part of the mechanize skill. The shared compilation algorithm — Stages 2 through 7, the Sentinel example, the ratification checklist, the expression language, the capability registry, the rules — lives in `CORE.md`, co-located in this directory.

## Known blocker — read this first

Cursor **does not expose a session transcript to the user's filesystem** in any standard, plugin-readable form.

- Cursor staff confirmed on their community forum that the transcript is JSONL of user/assistant messages with no usage records, no tool_call/tool_result records — and it is not exposed as a documented file path (referenced from `adapters/cursor/README.md:27`).
- The Cardinal Cursor plugin runs on live hook payloads (stdin JSON from `beforeSubmitPrompt`, `afterModel`, `afterTool`, `subagentStop`, etc.) — not on scraping a transcript. There is no repository-side code path that reads a session file from disk.
- Cursor exposes no CLI/programmatic entry point to spawn a subagent from a compilation run, so CORE.md Stage 5.5's cold-subagent ratification pass cannot be run cold in this environment.

**As a result, this SKILL cannot compile a Cursor session end-to-end today.** Invoking `/mechanize` in Cursor will hit the guardrails below and produce a `refusal-report.md` instead of a Sentinel.

Two conditional escape hatches exist:

**Escape hatch A — debug payloads.** If the user has `CARDINAL_CURSOR_DEBUG_PAYLOADS=1` set for their Cursor session, the Cardinal plugin writes raw hook payloads to `~/.cursor/cardinal/telemetry/debug/*` (per `adapters/cursor/README.md:27` and `adapters/cursor/hooks/cardinal-cursor-telemetry.py:190-193`). These payloads are NOT a Cursor-standard transcript — they are a Cardinal-specific dump of live-hook envelopes — but they contain user prompts, tool inputs, tool outputs, and generation IDs. A future SKILL revision could synthesize a compileable session from them; **this revision does not**, because the payload set is scattered per event, requires assembly, and has not been validated against the compiler.

**Escape hatch B — a hand-authored transcript.** If the user has manually assembled a session transcript in the general shape the mechanize compiler expects (turns with role, tool calls with name/input/output, attachments noted), pass the file path as an argument and Stage 1 will apply a general-structure inspector similar to the Gemini adapter's. This is a power-user path and is expected to be rare.

## How this skill is invoked

The user typed `/mechanize`, possibly with an argument.

**Argument parsing:**
- If the user provided an absolute path to a hand-authored session transcript (Escape hatch B) → treat as `SESSION_PATH`, proceed to Stage 1 with the general-structure inspector.
- If the user provided nothing OR provided a Cursor conversation ID → produce this `refusal-report.md`:

  ```
  # /mechanize refusal — Cursor session not compileable today

  Cursor does not expose a session transcript to disk in a documented, plugin-readable form
  (see this SKILL's "Known blocker" section for citations). The Cardinal Cursor plugin
  observes sessions via live hooks and does not persist a compileable transcript.

  What we can do today: nothing automated for this session.

  What you can do:

  1. If `CARDINAL_CURSOR_DEBUG_PAYLOADS=1` was set for the session you want to compile,
     debug payloads for it live under ~/.cursor/cardinal/telemetry/debug/. Those are not
     yet consumed by this SKILL; a future revision may synthesize a compileable session
     from them. File the ask against the mechanize skill if this matters to you.

  2. If you already have a hand-authored transcript in general JSON/JSONL shape (turns
     with role + tool calls + tool responses), pass its absolute path as an argument to
     /mechanize and this SKILL will apply a general-structure inspector.

  3. If you have shell access to the machine where the session ran under Claude Code,
     Codex, or Gemini instead, run /mechanize there — those adapters have first-class
     transcript readers.
  ```

  Do NOT proceed to Stage 1. Do NOT guess at a Cursor transcript path.

**Output location default (when Stage 1 is entered via Escape hatch B):** `./mechanize-out/<basename-of-session-file>/` under the current working directory. If the CWD is not writable, fall back to `~/mechanize-out/<basename>/`. Tell the user where you're writing.

## Then, before anything else — read the spec

Read `sentinels.md` §§ 8, 9, 10, 11, 12, 13, 14, 14a, 28, 28.1, 29, 32, 37, 47, 52 (co-located in this directory), and `FINDINGS.md` in full. The complete reading list with rationale is at the top of `CORE.md`. Do NOT skip this — even if you are about to produce a refusal report, the reader of that report may want to know what a Sentinel would have been.

## Stage 1 — Read and segment (Escape hatch B: general-structure inspector)

Enter this stage ONLY when the user provided an absolute path to a hand-authored transcript. Otherwise you already produced the refusal report and stopped.

Read the file with the following procedure:

1. **Determine the file's shape.** JSONL, single JSON array, or single JSON object.
2. **Identify the message container.** Look for the field that holds the conversation history — typically `messages`, `history`, `turns`, or `content`.
3. **Classify each turn.** Look for a `role` field with values like `user`, `assistant`, `tool`. For each turn: extract text content, tool calls (name + input), tool results (result payload paired with a call by ordering or by an explicit call_id if present).
4. **Extract attachments.** Note any base64-encoded inline data or file references. **Do not decode.**

**If the file's shape doesn't match this description**, stop and produce a `refusal-report.md` explaining what shape you found and asking the user to reshape the transcript. Do NOT invent tool calls that aren't there.

Produce a mental model of:
- **Objective**: first substantive user text.
- **Tool calls**: ordered list with their ordinal, name, input, and paired result.
- **Attachments**: any found; kind + mime + size only.
- **Conclusion**: last substantive assistant text.

## Stage 1.5 — Spill-to-disk pairs

Cursor has no documented transcript-level spill marker. For hand-authored transcripts, this stage is expected to be a no-op unless the author explicitly recorded a spill marker. If encountered, apply CORE.md Stage 1.5 semantics.

## Stage 2 addendum — shell-shaped tools in Cursor

Cursor's tool inventory in hand-authored transcripts is unbounded (whatever the author transcribed). If a tool call's name suggests a shell (`bash`, `shell`, `run_shell_command`, `exec`), apply CORE.md Stage 2's synthetic-capability-ID rule (`bash.<argv[0]>`). Otherwise treat by name.

## Stage 4.5 addendum — attachment vocabulary in Cursor

No documented vocabulary. In a hand-authored transcript, whatever the author used counts — apply CORE.md Stage 4.5's Q1–Q4 chooser conservatively; default to Q3 `requires-manual-input` when in doubt.

## Stage 5.5 addendum — cold-subagent mechanism in Cursor

**No cold-subagent mechanism is exposed to this SKILL.** Cursor's subagent hook (`subagentStop`) only fires from a live session and cannot be invoked programmatically from a compilation run.

**Fallback: inline ratification.** Perform CORE.md Stage 5.5's checklist yourself in a fresh reasoning pass and flag the degradation loudly in `rationale.md`:

> `Unresolved: Stage 5.5 ran inline because Cursor exposes no cold-subagent mechanism to this SKILL. The verdict below is weaker than a cold read would produce; a reviewer should treat R1–R6 as PASS-with-caveat rather than PASS.`

Use CORE.md Stage 5.5's checklist and verdict format verbatim.

## Now continue with CORE.md

If you reached this point, you entered via Escape hatch B and have a segmented mental model. Continue at **CORE.md Stage 2** and follow through Stage 7 with the Cursor addenda above.

Do NOT skip any of Stages 2 through 7. Do NOT hallucinate rules that aren't in CORE.md.

## Success criterion

See CORE.md's "Success criterion" section. For Cursor-compiled Sentinels, the rationale MUST include the Stage 5.5 degradation caveat and any Stage 1 general-structure caveats — the reader must understand this compilation ran without a first-party transcript reader and without a cold ratifier. **A refusal report is a first-class success outcome** for this adapter today; the honest failure is more valuable than a plausible-looking but ungrounded Sentinel.
