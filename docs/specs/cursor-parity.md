# Cursor-plugin parity — spec & plan of action

Status: **ready for implementation** (validated 2026-07-09; revised
after review 2026-07-09) · Target plugin: **`cardinal-cursor-plugin`**
v0.1.0 · Sources of truth: `cardinal-claude-plugin` v0.12.2
(`~/workspace/cardinal-claude-plugin`) and this repo's
`cardinal-codex-plugin` v0.5.2. Recent Codex enrichments the Cursor
plugin must mirror at v0.1.0: `user_turn_seq` (session-cumulative turn
counter alongside per-turn `turn_seq`), MCP-qualified `turn_tool.tool_name`
(server prefix preserved for MCP tool calls), `bash_class` (heuristic
classification of shell tool calls), and best-effort `subagent_description`
on `subagent_usage`. See `docs/specs/subagent-telemetry-enrichment.md`
for field-level detail.

## Goal

Ship a third editor plugin — `cardinal-cursor-plugin` — that produces the
same Cardinal telemetry contract, initiative classification, and
spend-limits behaviour as the Claude Code and Codex plugins, using
Cursor's native hooks and MCP config. Feature parity where the Cursor
surface allows; documented accepted asymmetries where it does not.

## Verified facts (2026-07-09)

Verified against Cursor's public docs
(https://cursor.com/docs/hooks) and community reports before writing this
plan. Every claim here is re-verified by an independent validation pass
attached below.

1. **Cursor hooks cover the events we need.** The `hooks.json` v1 surface
   exposes `sessionStart`, `sessionEnd`, `beforeSubmitPrompt`, `stop`,
   `preToolUse`, `postToolUse`, `postToolUseFailure`, `preCompact`,
   `subagentStart`, `subagentStop`, `beforeShellExecution`,
   `afterShellExecution`, `beforeMCPExecution`, `afterMCPExecution`,
   `beforeReadFile`, `afterFileEdit`, `afterAgentResponse`,
   `afterAgentThought`, plus Tab / workspace hooks
   (`beforeTabFileRead`, `afterTabFileEdit`, `workspaceOpen`) we do not
   consume. The bolded subset is the full set the two existing plugins
   depend on; the wider surface is called out because
   `afterAgentResponse` / `afterAgentThought` may resolve gap D — see
   P0 spike below.
2. **Cursor supports MCP.** Cardinal's MCP endpoint installs into
   `~/.cursor/mcp.json` (user) or `.cursor/mcp.json` (project) — same
   shape as the Codex `~/.codex/config.toml` write.
3. **`transcript_path` is exposed on agent hooks as `string | null`.**
   The docs define it as the *"path to the main conversation transcript
   file (null if transcripts disabled)"*; app-lifecycle hooks omit it
   entirely. The transcript **format and contents are not documented** —
   community reports (forum thread linked below) describe JSONL with
   user messages, assistant text, and tool-call inputs but not outputs,
   and no per-model-call token / rate-limit records. Treat all of that
   as an assumption to confirm in the P0 spike, not a fact. This gates
   one part of telemetry parity — see gap D below.
4. **`beforeSubmitPrompt` is a blocking hook** — outputting
   `{ "continue": false, "user_message": "…" }` prevents submission and
   surfaces the message to the user. This is the substrate the
   spend-limits gate rides on.
5. **`sessionStart` supports context injection AND env propagation** via
   `{ "additional_context": "…", "env": { … } }` in its stdout.
   `additional_context` is the substrate for the initiative-convention
   prompt + budget standing; `env` sets session-scoped environment
   variables visible to every subsequent hook and is a candidate native
   channel for propagating the verdict standing between events instead
   of the file-based ack/verdict layout the Codex plugin uses (see P3).
6. **Config priority is Enterprise → Team → Project → User, but hooks are
   additive**: *"All matching hooks from every source run; when responses
   conflict, higher-priority sources take precedence during merge."* So
   installing at user level does not get silently replaced by a project
   `.cursor/hooks.json` — both run.

## Gap inventory (Claude / Codex plugins → Cursor plugin v0.1.0)

The Cursor port inherits the union of Claude and Codex gaps against the
Lakerunner event contract. Anything the Codex plugin already resolved as
"portable to any editor" (worktree stripping, tag-form command detection,
`turn_seq` reset semantics, `TARGET_KEYS` extraction) is copied verbatim
from `cardinal-codex-telemetry.py`; only Cursor-specific gaps are
enumerated below.

### Missing features (must ship)

| # | Feature | Claude / Codex implementation | Cursor status |
|---|---------|-------------------------------|---------------|
| A | Spend-limits gate | `_limits_common.py` + `UserPromptSubmit` sync gate (block / warn / notify with band hysteresis + override file); async verdict refresh; budget standing at SessionStart | Absent — needs a Cursor-native `beforeSubmitPrompt` gate that emits `{continue: false, user_message}` for block. **Notify/warn cannot use `additional_context` on `beforeSubmitPrompt` — that field is not part of its output schema.** See divergence E for the two-tier resolution. |
| B | Initiative-convention prompt at SessionStart | `initiative-convention.py` (Claude) / `handle_session_start` (Codex) emit branch-naming convention + budget standing as `additional_context` | Absent — port to `sessionStart` output `additional_context` |
| C | MCP config write | Claude: `~/.claude/settings.json`; Codex: `~/.codex/config.toml` managed block | Absent — port to `~/.cursor/mcp.json` managed block (JSON, so use the Claude JSON-file managed-block strategy, not the Codex TOML BEGIN/END strategy) |

### Divergences inside shared events

| # | Divergence | Claude / Codex behaviour | Cursor v0.1.0 target | Resolution |
|---|-----------|--------------------------|-----------------------|------------|
| D | Per-model-call token usage | Claude: native OTel `api_request` events; Codex: `token_count` transcript records | Cursor transcript is documented to include neither. `api_request` / `cardinal.turn_usage` / `cardinal.plan_state` / `cardinal.plan_usage` have no obvious source | **Undecided until we inspect a live transcript.** Best case: undocumented but present → parse as Codex does. Worst case: absent → accepted asymmetry (`api_request`/plan telemetry not emitted on Cursor). Decide during P1 spike below. |
| E | Notify / warn band delivery | Claude sends `systemMessage`; Codex sends the same via `hookSpecificOutput`; both allow inline messages on non-blocking turns | Cursor `beforeSubmitPrompt` has NO `additional_context` output and no allow-with-message path — its documented output schema is only `{continue, user_message}`, and `user_message` renders only when `continue: false` | **Three-tier resolution.** *Notify* has no clean surface on the submit path; instead, deliver the notify context on the **next** turn boundary via `sessionStart.additional_context` (session-wide standing) and — if per-turn refresh matters — via `postToolUse.additional_context` piggybacked on the first tool call of the turn (documented `additional_context` output). *Warn*: if the user opts into strict mode (`CARDINAL_CURSOR_STRICT_WARN=1`), escalate to *block*; otherwise degrade to *notify*. *Block*: `{continue: false, user_message: <server text>}`. Document all three modes. |
| F | `tool_result` source | Claude: native tool events; Codex: transcript function_call_output records | Cursor transcript excludes tool outputs by design. `postToolUse` payload includes the tool result | Emit `tool_result` + `cardinal.turn_tool` from `postToolUse` payloads (never from transcript). |
| G | SubagentStop payload fidelity | Claude: rich payload; Codex: **P5 deferred** (payload shape never observed in the wild — env-gated debug dump under `CARDINAL_CODEX_DEBUG_PAYLOADS`) | Cursor documents `subagentStop` payload keys verbatim as `subagent_type`, `status`, `task`, `description`, `summary`, `duration_ms`, `message_count`, `tool_call_count`, `loop_count`, `modified_files`, `agent_transcript_path` (no top-level `subagent_id` on stop — use the `subagentStart` payload's `subagent_id` if we need to correlate) | **Cursor is strictly better here**: emit `cardinal.subagent_usage` directly from these documented keys without a capture phase. Correlation to `subagentStart` (which does carry `subagent_id`, `parent_conversation_id`, `tool_call_id`, `subagent_model`, `is_parallel_worker`, `git_branch`) is via `agent_transcript_path` + `task`. |
| H | Cloud-agent scope | Claude cloud agents run all hooks; Codex has no cloud-agent surface | Cursor cloud agents (a) skip `sessionStart`, `beforeSubmitPrompt`, `stop`; (b) **only load `.cursor/hooks.json` at repository root, plus team/enterprise hooks — NOT `~/.cursor/hooks.json`** (docs verbatim: *"User-level hooks (`~/.cursor/hooks.json`) are not available in cloud agents. Cloud agent VMs don't have access to your local home directory configuration."*) | Two-mode install in `cardinal-connect`. **Default (user mode):** writes `~/.cursor/hooks.json` + `~/.cursor/mcp.json`; cloud-agent runs are unsupported (documented). **`--project` mode:** additionally writes `.cursor/hooks.json` + `.cursor/mcp.json` at repo root, with managed-block markers so `cardinal-disconnect --project` can strip cleanly. Cloud-agent telemetry (postToolUse / subagentStop / preCompact) works only when the plugin is installed in `--project` mode on the repo the cloud agent runs against. Initiative classification from branch name still applies downstream. |
| I | Blocked-prompt UX bugs | Claude clears blocked prompts from history | Cursor community forum reports (Jun–Jul 2026): blocked messages still land in later LLM context; double-popup when `continue:false` + `user_message` | Not our bug to fix; document in the plugin README as a known Cursor-side issue with links to the forum threads. If Cursor patches these, this row drops. |

### Accepted asymmetries (non-goals)

- **OAuth plan cache** — Anthropic-subscription concepts, already excluded
  from the Codex plugin. Unchanged for Cursor.
- **`cost_usd` for non-OpenAI Cursor models** — the Codex plugin's
  `MODEL_PRICING_USD_PER_M` table covers OpenAI SKUs. Cursor sessions
  running Claude / Gemini / etc. will not have inline cost until we extend
  the pricing table (out of scope for v0.1.0; tracked as follow-up).
- **`beforeShellExecution` `allow`/`ask` permissions** — Cursor forum bug
  #144244: only `deny` is respected. We do not build any feature that
  depends on the allow path.

## Plan of action

- **P0 — telemetry-source spike (2 days).** Before writing any hook code:
  connect a throwaway Cursor session, run 5–10 prompts, and inspect
  *three* candidate sources for per-model-call token / rate-limit
  records — do not stop at the first that comes up empty:
  1. `cat "$transcript_path"` and grep for `usage`, `tokens`,
     `input_tokens`, `rate_limits`, `plan`, `token_count`.
  2. Log the raw stdin JSON of `afterAgentResponse` and
     `afterAgentThought` hooks (the docs are silent on their payload
     shape; they are the most likely native carrier of per-turn usage).
  3. Log the raw stdin JSON of `postToolUse` for aggregation potential.
  Decision matrix: any of the three carrying token totals → parity path
  for `api_request` / `cardinal.turn_usage` opens. All three empty →
  accept D as an asymmetry and document the gap in the README.
- **P1 — repo bootstrap.** Create a new repo
  `cardinal-cursor-plugin` (peer of `cardinal-codex-plugin`,
  `cardinal-claude-plugin`) with the same layout:
  `plugins/cardinal-cursor-plugin/{hooks,scripts,skills,tests}`. Copy
  `_limits_common.py` and `_plugin_version.py` verbatim (adjust
  `CODEX_DIR` → `CURSOR_DIR = ~/.cursor`, `~/.codex/*` paths →
  `~/.cursor/*`). Copy the pure-logic helpers from
  `cardinal-codex-telemetry.py`: worktree stripping, initiative
  classification, command detection, pricing table, `emit_records`.
- **P2 — connect flow.** Port `scripts/cardinal-connect` with these
  deltas:
  - MCP write target is `~/.cursor/mcp.json` (JSON, not TOML). Use the
    Claude JSON managed-block strategy: read → merge → write, with a
    `cardinalManaged: true` marker on the `mcpServers.cardinal` entry so
    disconnect can safely delete it.
  - Hooks target is `~/.cursor/hooks.json` (JSON schema v1); with
    `--project`, additionally `.cursor/hooks.json` at repo root (for
    cloud-agent coverage per divergence H). Managed entries in both
    files: `sessionStart`, `beforeSubmitPrompt`, `postToolUse`,
    `preCompact`, `subagentStop`, `stop`. All entries carry an
    object-level `cardinalManaged: true` field so disconnect can target
    only ours.
  - State layout: `~/.cursor/cardinal.json`,
    `~/.cursor/cardinal-secrets.json` (mode 0600), progress cursors and
    verdicts under `~/.cursor/cardinal/{telemetry,limits}/`.
  - **Evaluate `sessionStart.env` as an alternative to the file-based
    ack layout** — Cursor's native env-propagation lets `sessionStart`
    stamp the current verdict standing into a `CARDINAL_VERDICT_*` env
    var read by `beforeSubmitPrompt` on every turn, potentially
    replacing `<session>.ack.json`. Decide during P3; keep the
    file-based layout as fallback for cross-session state (the env
    channel is per-conversation).
- **P3 — telemetry hook.** New
  `hooks/cardinal-cursor-telemetry.py` mirroring the Codex hook's
  event-multiplexer layout:
  - `handle_session_start` → emits `additional_context` (convention +
    budget standing); performs the forced verdict fetch.
  - `handle_before_submit_prompt` → sync gate: **block** (`continue:
    false` + `user_message`), or pass-through (`continue: true` with
    no message) after stamping the pending notify/warn context into a
    `CARDINAL_NOTIFY_MSG` env-var-style ack file for the next
    `postToolUse` to surface. Then git_state OTLP post; then async
    verdict refresh.
  - `handle_post_tool_use` → emits `tool_result` + `cardinal.turn_tool`
    (using `TARGET_KEYS` fallback, MCP-qualified tool names, and
    `bash_class`); additionally emits `additional_context` carrying the
    pending notify/warn message when one is staged (once per turn).
  - `handle_pre_compact` → matches the Codex `PreCompact` semantics
    (currently a no-op there; kept for future turn-boundary needs).
  - `handle_subagent_stop` → emits `cardinal.subagent_usage` from the
    documented payload keys (`subagent_type`, `status`, `task`,
    `description`, `summary`, `duration_ms`, `message_count`,
    `tool_call_count`, `loop_count`, `modified_files`,
    `agent_transcript_path`), populating `subagent_description` from
    `task` / `description`.
  - `handle_stop` → resume-cursor JSONL sweep (contingent on P0
    outcome); at minimum emits any `api_request` / `turn_usage` we can
    extract, else no-op.
- **P4 — tests + release.** Extend `tests/test_cardinal_plugin.py`
  (mirror the Claude / Codex fixtures for worktree stripping, command
  detection, gate hysteresis, SessionStart context). Add Cursor-specific
  tests for JSON managed-block round-trip and the degraded warn-band
  path. Ship v0.1.0.
- **P5 (deferred) — non-OpenAI pricing.** Once Cursor sessions running
  Claude/Gemini become common in customer telemetry, extend
  `MODEL_PRICING_USD_PER_M` with a `provider` axis.

### Keeping the repos in lockstep

Three repos now; the guard is unchanged: shared pure logic (initiative
resolution, command detection, gate hysteresis) is duplicated by design
but pinned by identical test fixtures across all three repos. When one
repo changes the contract, its fixture diff is the prompt to mirror the
other two.

## Live verification checklist (post-implementation)

To be filled after P4:

- [ ] SessionStart entry in `~/.cursor/hooks.json` (connect covered by
      tests; live install registered).
- [ ] `sessionStart` handler emits convention prompt + real budget
      standing and warm-writes the verdict file.
- [ ] Worktree stripping in prod on a Cursor session:
      `feat/worktree-fix-…-cursor-parity-verify` → correct
      `initiative_name` / `type`.
- [ ] `plan_state` / `plan_usage` throttling parity — or, if gap D
      forced an asymmetry, this row is deleted with a README note.
- [ ] Block verdict → `beforeSubmitPrompt` blocked, override file
      downgraded the block and the turn completed.
- [ ] `postToolUse` → `tool_result` events land in prod
      `agent_session_events`.
- [ ] `subagentStop` payload → `cardinal.subagent_usage` land in prod.
- [ ] Cloud-agent smoke: preToolUse/postToolUse telemetry lands from a
      cloud-agent run; sessionStart / gate correctly do **not** fire.

## Independent validation (fresh eyes)

Every Cursor-specific claim in "Verified facts" and "Divergences" was
re-checked by an independent subagent on 2026-07-09 doing fresh web
searches. Verdict: **SPEC READY** after the fixes below were folded in.

| Claim | Section | Verdict | Note |
|-------|---------|---------|------|
| Hook event list | Verified fact 1 | CONFIRMED w/ omissions | Widened to name `sessionEnd`, `beforeReadFile`, `afterFileEdit`, `afterAgentResponse`, `afterAgentThought` and Tab / workspace hooks (spec now flags them explicitly, esp. for P0). |
| MCP config paths | Verified fact 2 | CONFIRMED | `~/.cursor/mcp.json` (user), `.cursor/mcp.json` (project). |
| `transcript_path` contents | Verified fact 3 | CONFIRMED | Docs silent on per-model-call token counts and rate-limit records → gap D stands. |
| `beforeSubmitPrompt` output | Verified fact 4 | CONFIRMED | Field is `continue` (not `permission`); `continue:false` blocks and shows `user_message`. |
| `sessionStart` output | Verified fact 5 | CONFIRMED w/ omission | Spec updated to name `env` alongside `additional_context`; P3 now evaluates `env` as an alt to the ack file. |
| Priority + additive | Verified fact 6 | CONFIRMED verbatim | Enterprise → Team → Project → User, all sources run. |
| No allow-with-message | Divergence E | CONFIRMED | Justifies the two-tier warn resolution. |
| `subagentStop` payload keys | Divergence G | **REFUTED — spec fixed** | Docs list `subagent_type`, `status`, `task`, `description`, `summary`, `duration_ms`, `message_count`, `tool_call_count`, `loop_count`, `modified_files`, `agent_transcript_path`. Spec previously wrongly listed `subagent_id` at top level and omitted `task`/`description`/`summary`. Corrected; `subagent_id` correctly attributed to `subagentStart`. |
| Cloud-agent skipped hooks | Divergence H | CONFIRMED | Also skipped: `sessionEnd`, MCP hooks, `afterAgentResponse` / `afterAgentThought`, Tab / workspace — documented. |
| Forum bug 10a (blocked-in-history) | Divergence I | Confirmed report; Discourse auto-closed; no resolution observed in thread | forum #153318 |
| Forum bug 10b (double-popup) | Divergence I | Confirmed report; Discourse auto-closed; no resolution observed in thread | forum #150091 |
| Forum bug 10c (shell allow/ask) | Divergence I | Confirmed report; Discourse auto-closed; no resolution observed in thread | forum #144244 |

Enrichments folded in from the validation pass:

- `afterAgentResponse` / `afterAgentThought` added to the P0 spike as
  additional candidate carriers of per-model-call token usage.
- `sessionStart.env` added to P3 as an evaluated alternative to the
  file-based ack layout.
- Verified fact 1 now names the wider event surface so future work
  doesn't rediscover it.

## Second-pass review (2026-07-09)

A follow-up review caught six additional issues after the first
validation pass:

- **P1 — `additional_context` on `beforeSubmitPrompt`.** Original draft
  planned to use it for notify/warn; verified against docs, it is only
  supported on `sessionStart`, `postToolUse`, and `preCompact`.
  Divergence E rewritten to a three-tier resolution: block via
  `user_message`; notify/warn delivered on the *next* turn boundary via
  `postToolUse.additional_context` (or `sessionStart.additional_context`
  at session start), with an opt-in `CARDINAL_CURSOR_STRICT_WARN=1`
  escalation.
- **P1 — cloud-agent install path.** Docs verbatim: user-level
  `~/.cursor/hooks.json` is not loaded by cloud agents. Divergence H
  rewritten to require a `cardinal-connect --project` mode that
  additionally installs at `.cursor/hooks.json` + `.cursor/mcp.json` at
  repo root for cloud-agent coverage.
- **P1 — `preToolUse` vs `preCompact` typo.** Fixed in P2: managed
  hooks list now names `preCompact` directly.
- **P2 — claude source-of-truth version.** Bumped from v0.11.x to
  v0.12.2; recent Codex enrichments (`user_turn_seq`, MCP-qualified
  `turn_tool.tool_name`, `bash_class`, `subagent_description`) named
  explicitly as v0.1.0 requirements.
- **P2 — transcript guarantees.** Verified fact 3 softened: docs only
  guarantee `transcript_path: string | null` on agent hooks; JSONL
  format and content shape are community-reported, not documented, and
  are treated as P0-spike assumptions.
- **P3 — forum-bug wording.** Verdicts rewritten to *"Confirmed report;
  Discourse auto-closed; no resolution observed in thread."*
