# Cardinal Cursor plugin

Connect Cursor to Cardinal telemetry and the unified MCP endpoint in one browser-approved consent.

This is a Cursor-native port of the command surface shared by the [Claude Code plugin](https://github.com/cardinalhq/cardinal-claude-plugin) and [Codex plugin](https://github.com/cardinalhq/cardinal-codex-plugin):

| Script | What it does |
| --- | --- |
| `cardinal-connect` | Runs Cardinal's device-code flow, mints ingest and MCP keys, writes managed `~/.cursor/mcp.json` + `~/.cursor/hooks.json`, and (with `--project`) additionally writes `.cursor/mcp.json` + `.cursor/hooks.json` at the repo root for cloud-agent coverage. |
| `cardinal-status` | Shows the recorded Cardinal workspace and probes the configured ingest and MCP endpoints. |
| `cardinal-disconnect` | Best-effort revokes Cardinal keys, removes managed Cursor config entries at both user and project locations, and deletes local state. |
| `cardinal-decision` | Opt-in decision capture: `on` / `off` / `status` / `record` (emits `cardinal.decision`). See [Decision capture](#decision-capture-opt-in). |
| `cardinal-connect --owner-input on\|off` | Your consent to recording the prompts you type as owner input of the session's Investigation. Doesn't connect. See [Owner input](#owner-input-opt-in-consent). |

Skills (`skills/`): `cardinal-connect`, `cardinal-disconnect`, `cardinal-status`, `cardinal-decision` (status only), `cardinal-storyboard`, `canvas`, `cardinal-install-site`, `migrate-from-grafana`, `optimize-toolkit`. Subagent (`agents/`): `brainstorm`, see [Skills and the brainstorm subagent](#skills-and-the-brainstorm-subagent).

## Telemetry scope

Cursor does not expose Claude Code's native OpenTelemetry emitter. This plugin emits Cardinal-compatible telemetry from Cursor hooks, sending the same Lakerunner event contract used by the Claude and Codex plugins where Cursor exposes equivalent data (see `docs/specs/cursor-parity.md` at the repository root for the full parity map):

- `cardinal.git_state` from the active Git checkout on `beforeSubmitPrompt`, including initiative classification from the branch name (worktree noise stripped), slash-command detection, and the branch's PR as `cardinal_pr_number` / `cardinal_pr_url` when `gh pr view` resolves one. The lookup is cached per repo + branch (10 min for a hit, 2 min for a miss), skipped on protected branches, and capped at 1.5s on a cache miss because the hook is synchronous. If there's no PR, no `gh`, or `gh` isn't authenticated, the keys are left out and the record is still sent.
- `cardinal.turn_tool` + `tool_result` from `postToolUse` payloads, with MCP-qualified `tool_name` on `turn_tool` and Bash-verb `bash_class` classification.
- `cardinal.subagent_usage` from `subagentStop` payload keys (`subagent_type`, `status`, `task` / `description`, `duration_ms`, `message_count`, `tool_call_count`, `loop_count`).
- `cardinal.turn_thought` from `afterAgentThought` — duration and text length only (never the model's thinking text itself, which is potentially large and sensitive).
- `cardinal.turn_response` from `afterAgentResponse` — text length only (never the response text itself).
- `cardinal.plan_usage` (context-window slice) from `preCompact` — `context_tokens`, `context_window_size`, `context_usage_percent`, `trigger`, `messages_to_compact`, `is_first_compaction`. This is a context-usage slice on the `plan_usage` event name; downstream disambiguates from per-model-call plan_usage on the presence of `plan.compact_trigger`.
- Every emitted OTLP resource is stamped with `cursor.model`, `cursor.model_id`, `cursor.model_params`, and `cursor.version` from the hook payload's base fields when Cursor provides them, so downstream slicing by model and Cursor build works without inspecting each event.

Cursor product-side gap — per-model-call `cardinal.turn_usage` / `cardinal.api_request`:

- These events require input/output/cached token counts on every model call. **Cursor's hook surface and transcript format do not expose per-model-call token counts**, so no plugin-side implementation can produce them. This is a Cursor-side product gap, not a docs gap or a plugin blocker. Cursor staff confirmed on the forum that the transcript is JSONL of user/assistant messages with no usage records ([cursor forum #157311](https://forum.cursor.com/t/accessing-the-full-agent-transcript-in-cursor/157311)). `CARDINAL_CURSOR_DEBUG_PAYLOADS=1` still writes raw hook payloads under `~/.cursor/cardinal/telemetry/debug/` for post-hoc verification and future schema evolution.

## Session context & spend limits

Parity features with the Claude and Codex plugins, driven by the same server-side contract:

- **`sessionStart` context** — every session in a git repo receives the Cardinal initiative branch-naming convention as hook context, plus the session's current spend-budget standing when your Cardinal backend has agent spend limits enabled.
- **Spend-limits gate** — on every prompt the hook reads the locally cached limits verdict (file I/O only, never network on the critical path):
    - `block` stops the turn via Cursor's documented `{continue: false, user_message}` output.
    - `warn` / `notify` **do NOT surface on `beforeSubmitPrompt`** — Cursor's schema has no `additional_context` slot on that hook. Instead, the plugin stages the standing message and surfaces it via `postToolUse.additional_context` on the first tool call of the next turn. This is a documented divergence from the Claude/Codex plugins (see `docs/specs/cursor-parity.md` Divergence E).
    - Set `CARDINAL_CURSOR_STRICT_WARN=1` to escalate warn-band verdicts to hard blocks. Warns then use the block channel and become inline `user_message` copy.

Verdicts refresh in the background after each prompt's telemetry post. Everything fails open.

State lives under `~/.cursor/cardinal/` (telemetry progress cursors, plan stamp, limits verdicts + notify staging, decision ledger + PR cache); `cardinal-disconnect` removes it.

## Decision capture (opt-in)

Records the choices an agent makes while it works. Each one becomes a `cardinal.decision` event tagged with your email, the Cursor conversation id, repo, branch, head sha, PR, anchors, and code clusters. The event and the on/off semantics match the Claude plugin (see `docs/specs/decision-telemetry.md` at the repository root). The recording path is different because of Cursor's sandbox.

**You run these in your own terminal** (not the agent):

```bash
python3 scripts/cardinal-decision on        # off by default
python3 scripts/cardinal-decision status [--session <conversation-id>]
python3 scripts/cardinal-decision off
```

`CARDINAL_DECISIONS=1` / `0` in the environment Cursor's hooks run with overrides `on` / `off`.

**The agent runs `cardinal-decision record`, and a hook records it.** Since Cursor 3.6, the default Auto-review mode runs agent shell commands in a sandbox: writes are limited to the workspace and `/tmp`, and network access is denied by default ([run modes](https://cursor.com/docs/agent/security/run-modes), [sandbox reference](https://cursor.com/docs/reference/sandbox)). So the flow is split:

1. The agent runs `python3 scripts/cardinal-decision record --choice "..." [--question ...] [--why ...] [--alt ...] [--anchor path[::Symbol]] [--by user] [--follows|--refines|--supersedes <id>]`. The command only validates the arguments, prints one `cardinal-decision-record:v1 {json}` line, and exits 0. It never reads or writes `~/.cursor`, never runs `gh`, and never opens a network connection, so it succeeds in the sandbox and can't misreport whether capture is on.
2. Cursor fires `postToolUse` for that shell call with `tool_input.command` and `tool_output` (a JSON string with `stdout`), as documented in the [hooks docs](https://cursor.com/docs/agent/hooks). The Cardinal hook looks for the marker line, but only when the command itself invoked `cardinal-decision`.
3. The hook applies the on/off gate. If capture is on, it re-validates the decision, derives the final id against the session ledger, resolves anchors and code clusters, resolves the branch's PR with `gh`, writes the ledger under `~/.cursor/cardinal/decisions/`, and emits the event using the hooks' ingest connection. It then reports the recorded id, or why nothing was recorded, back to the agent as `additional_context`.

The hook is the only thing that writes or emits a decision. In Run Everything mode (unsandboxed) or on Windows, the CLI still has no side effects, so nothing is sent twice.

**Where the decision comes from.** The hook doesn't trust the marker line's contents. It splits `tool_input.command` into shell commands, handling quoting, `;` / `&&` / `|` / newlines, backslash-newline continuations, `VAR=x` and `env` prefixes, and interpreter flags such as `python3 -u`. For each command that really runs `cardinal-decision record`, it parses the arguments with the same parser the CLI uses (`hooks/_decision_cli.py`). The marker line only confirms that run: a decision is accepted when the output contains a marker equal to the one derived from the arguments. So a marker echoed, printed, grepped, or cat'ed from saved output is ignored wherever it appears in the command. If it happens to match the real call, it's harmless. If the command runs `record` but nothing can be recorded (unparseable command, invalid arguments, no matching confirmation, capture off, or an error), the agent is told the decision was **not** recorded and why. If you aren't connected, decisions are only saved to the local ledger.

**What runs where.** The `postToolUse` hook does only local work: parsing, the on/off gate, validation, id assignment, the ledger write, and the reply. Every network send for that tool call happens in a detached background process (its own session, stdio on `/dev/null`), handed off through an atomically written `0600` spool file under `~/.cursor/cardinal/spool/`. That covers the `cardinal.turn_tool` / `tool_result` records, the code-cluster tree listing, the `gh`-based PR lookup, and the `cardinal.decision` POST. With ingest stalled, the hook still returns in well under a second. Hooks and their background process run outside the agent sandbox (the existing hooks already write under `~/.cursor/cardinal/` and POST to ingest from there), so `git` and `gh` **still run unsandboxed**, with your `gh` credentials. If the background process dies, the decision stays in the local ledger but its event is never sent.

**After upgrading, re-run `python3 scripts/cardinal-connect`** (no `--rotate` needed: on an existing connection it refreshes the user-level hook entries and the `cardinal-behavior` MCP entry in place, with no new key). That includes the 15s `postToolUse` timeout; older installs have 5s. Project files (`--project`) still need `--rotate --project`.

Known limits of the background process: several tool calls finishing together on a branch whose PR isn't cached yet can each start their own `gh pr view`, and their concurrent writes to the shared PR cache (`prs.json`, last writer wins) can drop another branch's cached entry. The only cost is an extra `gh` call later. An argument that is only a shell operator (for example `--why ";"`) is read as a separator, so that decision is reported as not recorded.

Unverified, and worth knowing:

- **Can the sandboxed agent run the CLI at all?** The CLI lives wherever you cloned this plugin, which is usually outside the workspace. Cursor's docs don't say whether sandboxed commands can read outside the workspace by default. [Run modes](https://cursor.com/docs/agent/security/run-modes) says a sandboxed command *"can work in your project, but it cannot freely read protected files, write outside approved paths, or contact arbitrary network destinations"*. The [sandbox.json reference](https://cursor.com/docs/reference/sandbox) offers `additionalReadonlyPaths` (*"Extra paths the agent can read"*) and says only that *"SSL certificate paths and ~/.ssh are always readable."* If Cursor blocks the read, it *"will indicate when a command runs outside the sandbox and ask for your approval"*. The session instructions tell the agent to ask for that, since `record` changes nothing. To avoid the prompt, add the plugin directory to `additionalReadonlyPaths` in `~/.cursor/sandbox.json`.
- **Does `postToolUse` fire for sandboxed Shell calls?** The [hooks docs](https://cursor.com/docs/agent/hooks) don't say either way. `postToolUseFailure` exists for failed calls, and `record` exits 0, so `postToolUse` is the expected event, but this hasn't been checked in a real sandboxed session.
- **Subagents** may carry a different `conversation_id` from the parent chat. Their decisions then go to a separate ledger, and earlier decisions from the parent won't be listed as known ids.

**How the agent is told to record decisions.** When capture is on, the `sessionStart` hook adds the recording instructions and the session's decisions so far to its `additional_context`. Cursor documents that field as *"Additional context to add to the conversation's initial system context"* ([hooks docs](https://cursor.com/docs/agent/hooks)). The instructions tell the agent the command is sandbox-safe, that the hook reports the id, and that `on` / `off` / `status` are for the user. Limits of that surface:

- **The instructions arrive once, at session start.** `beforeSubmitPrompt` can't add context: its output schema is `{continue, user_message}` only. So unlike Claude's per-prompt `UserPromptSubmit` injection, the list of earlier decisions isn't refreshed on each turn. The `postToolUse` reply after each `record` gives the agent the id it needs for later links.
- **Turning capture on doesn't affect sessions that are already open.** Start a new chat to pick it up.
- **User Rules are not used.** They live only in Cursor Settings (*Customize → Rules*), and there's no file on disk the plugin could write ([rules docs](https://cursor.com/docs/context/rules)). Project rules (`.cursor/rules/*.mdc`) would have to be committed to each repo.

## Investigation sessions

Each Cursor conversation is bound to a Cardinal Investigation, the live record behind its storyboard. The shared logic lives in `cardinal_core.investigation_agent` / `investigation_turn` (the same as Codex and Gemini). This adapter only maps it onto Cursor's hook channels.

Cursor gives a hook three ways to put text in front of the model: `sessionStart` `additional_context` (*"Additional context to add to the conversation's initial system context"*), and `postToolUse` / `postToolUseFailure` `additional_context` (*"Extra context injected into the conversation after the tool result"*) ([hooks docs](https://cursor.com/docs/agent/hooks)). `beforeSubmitPrompt` answers `{continue, user_message}` only, `afterFileEdit` documents no output, and `stop`'s `followup_message` is *"automatically submit[ted] … as the next user message"*. So anything produced outside those boundaries is **staged** (`~/.cardinal/investigations/sessions/<conversation>.staged.json`, 0600, at most 20 items) and delivered at the next one.

| Hook | What it does |
| --- | --- |
| `sessionStart` (10 s) | Bootstraps the Investigation (one request), starts the detached poller (`hooks/investigation-poller.py`), and adds the session guidance: the labeled storyboard and investigation-timeline links, the `cardinal-storyboard investigation … --session <id>` commands (ack, post, checkpoint), and the supervision line when the server offers it. Also anything staged for this conversation, the connect hint (unconnected machine) and the stray-key warning (an MCP key without a URL). |
| `postToolUse`, `postToolUseFailure` | The tool boundary: staged items, then the poller's inbox (advisory events), then, if nothing else was delivered, one re-surfaced accepted challenge or directive. A payload that names a subagent (`agent_id` / `subagent_id`; Cursor documents none on tool hooks) gets nothing and consumes nothing. Local only. |
| `stop` (10 s) | The turn's end: the inbox, one bounded sync check when the poller is stale, an open directive or the owner-decision reminders, the owner-input outbox flush, and the owner-input turn boundary. What it would deliver is **staged**. It prints nothing, ever: no `followup_message`. |
| `beforeSubmitPrompt` | Owner input (below) and the discovery re-look. The limits gate runs first and keeps priority: a blocked prompt is not captured. |

`CARDINAL_STORYBOARD_SESSION_START=0` turns off the bootstrap and the session text. `CARDINAL_INVESTIGATION_POLLER=0` turns off the poller. The agent runs `scripts/cardinal-storyboard investigation …` in its own shell. **In Cursor's default Auto-review mode that shell has no network** ([run modes](https://cursor.com/docs/agent/security/run-modes), [sandbox reference](https://cursor.com/docs/reference/sandbox)), so every investigation command that talks to Cardinal (ack, post, checkpoint, `events --follow`) needs the user's approval to run outside the sandbox. The session guidance says so. The MCP tools (`storyboard__read_investigation_events` / `storyboard__post_investigation_event`) need no approval when no local evidence has to be promoted.

## Owner input (opt-in consent)

With owner input on, the prompts you type are recorded to the session's Investigation as owner input (`owner_input.recorded`). Credentials are scrubbed and the text is capped at 32 KiB. Only you and the grantees you authorize can read them. That's how a supervisor can check what you told this agent.

Claude Code, Codex and Gemini CLI tell you this per session, in a user-visible message, before the first capture. **Cursor has no such channel**: `beforeSubmitPrompt`'s `user_message` is shown only *"when the prompt is blocked"*, and Cursor's other hook outputs are model context ([hooks docs](https://cursor.com/docs/agent/hooks)). So on Cursor, nothing is captured until you consent once, in your own terminal:

```bash
python3 scripts/cardinal-connect --owner-input on    # prints the full text, asks you to type yes
python3 scripts/cardinal-connect --owner-input off
```

The consent is stored in `~/.cursor/cardinal/owner-input-consent.json` (0600), versioned by the sha256 of the text you saw: if Cardinal changes the text, you are asked again. `on` refuses to run without an interactive terminal, so the agent's shell can't consent for you. Even then, a session records prompts only when its Investigation's server turns owner input on and the session authored the Investigation (a joined one never does). `cardinal-status` shows the state. `cardinal-disconnect` removes `~/.cursor/cardinal/`, consent included.

**Kill switch**, whatever the consent says: `CARDINAL_OWNER_INPUT=0` (or `false` / `off` / `no`) in the environment Cursor's hooks run with, or `{"cursor": "off"}` in `~/.cardinal/owner-input.json`. The file is for GUI launches, which don't see your shell's environment. With either set, nothing is captured and queued prompts are deleted instead of sent.

How it is captured: `beforeSubmitPrompt` calls `cardinal_core.owner_input.capture(..., detached=True)`. The turn is numbered under the session lock and a detached process posts it, so the prompt never waits on the network, and nothing ever blocks it or prints. A payload naming a subagent is refused. Cursor fires `beforeSubmitPrompt` once per prompt, so the same prompt typed twice is recorded twice. Anything still queued is flushed at `stop`.

## Evidence capture (storyboards)

The `postToolUse` and `postToolUseFailure` hooks record every tool call (Cursor's own tools such as `Shell` and `read_file`, any MCP server's `mcp__<server>__<tool>`, and any tool Cursor adds later) in the local evidence spool shared with the other adapters: `~/.cardinal/evidence/<conversation_id>/ev_<id>.json` (directories 0700, files 0600). The shared pipeline (`cardinal_core.evidence_capture`) decides without naming any tool: a call that touches something sensitive (a `.env` or key file, `~/.aws`, `printenv`, `gh auth token`, a credentialed URL or header) is kept only as a *withheld* stub; everything else is scrubbed (the gateway's receipt scrub plus plain-text `key=value` rules), capped at 256 KiB and removed after 14 days. The hook hands the agent `[evidence:ev_…]` via `additional_context` (the first capture of a conversation also explains how to cite it). Entries name the client `cursor/<cursor_version>`. Nothing leaves the machine: `scripts/cardinal-evidence promote --storyboard sb_… ev_…` uploads only what a storyboard cites, with the connection's MCP key. Cursor's agent sandbox denies network by default, so promote has to run outside it (approve it, or run it in your own terminal). Cardinal's own `cardinal` server is skipped (its gateway mints witnessed receipts). Opt out with `CARDINAL_EVIDENCE_CAPTURE=0`, `scripts/cardinal-evidence off` or the flag file `~/.cardinal/evidence/disabled`. Capture is local file work and fails open.

`postToolUseFailure` is registered by `cardinal-connect` from this version on: re-run `cardinal-connect --rotate` (or `--project` for cloud agents) to pick it up. Its payload fields are not yet verified against a live Cursor build; the hook reads the error from `error` / `error_message` / `errorMessage` and falls back to `tool_output`, and `CARDINAL_CURSOR_DEBUG_PAYLOADS=1` dumps the real payload.

## Storyboard associations

Where a storyboard is written from (repo, branch, PR, HEAD, the files this
conversation edited) is recorded on each act as its `context`, and
storyboards that may relate to the checkout are shown at session start.
Shared logic: `cardinal_core.storyboard_agent`.

- **sessionStart** (the existing hook): when Cardinal MCP is connected,
  `additional_context` names this conversation's id (for
  `storyboard__create`, `storyboard__add_act` and `storyboard__find`
  `session_id`) and `scripts/cardinal-storyboard context --bare --session-id
  <id>`, which prints the `context` object itself to pass (without `--bare`
  it is wrapped in `{"context": {…}}`), plus the storyboards that may
  relate to this branch, PR or commit (at most 3, 2 KB, framed as data; gh
  cache only, a 2 s network deadline, `X-Cardinal-Client: cursor/<plugin
  version>`). Off: `CARDINAL_STORYBOARD_DISCOVERY=0` (the storyboards) or
  `CARDINAL_STORYBOARD_SESSION_START=0` (all of it).
- **Edited files**: `afterFileEdit` (registered by `cardinal-connect` from
  this version on; re-run it) records the edited `file_path` for
  `context.paths`, then runs the **edit lookup**: on the first edit in a
  directory this session, one bounded request (1.5 s) for the storyboards
  about that file or a PR that last changed it. `afterFileEdit` has no
  output, so the block is staged and surfaces at the next `postToolUse`.
- **Discovery re-look**: on each prompt, when the branch or HEAD moved since
  the conversation's last look, the related storyboards are looked up again
  in the detached background process and staged for the next `postToolUse`.
- **Automatic context: not supported by Cursor's hook system; SessionStart +
  CLI used.** `beforeMCPExecution` answers with a permission
  (allow / deny / ask) and cannot rewrite the call's input, so nothing is
  registered for it.

No Cursor payload has been captured for these hooks (no Cursor build was
available): the `afterFileEdit` shape (`file_path` absolute, `edits`) and the
`beforeMCPExecution` limits are from Cursor's hooks documentation and are
unverified. `CARDINAL_CURSOR_DEBUG_PAYLOADS=1` dumps the real payloads.

## Storyboard authoring

Shared logic: `cardinal_core.storyboard_authoring`, the same as the other agents.

- **Preview render.** After Cardinal's `storyboard__preview`, a second
  `postToolUse` entry (`--role authoring`, matcher
  `.*storyboard__(preview|publish).*`, **timeout 150 s**) renders every scene and
  reveal step locally with Chromium (`skills/storyboard/scripts/render_preview.py
  --runtime cursor`, PNGs under `~/.cursor/cardinal/storyboards/<sb>/r<rev>/`),
  then the cover and link-preview mock, and returns the PNG list as
  `additional_context`. Cursor documents a per-entry `timeout` in seconds with no
  stated maximum, and `matcher` on `postToolUse` (*"MCP tools using the
  `MCP:<tool_name>` format"*) ([hooks docs](https://cursor.com/docs/agent/hooks)).
  The 15 s on the main entry is the plugin's own choice, not a Cursor limit. The
  renderer stops at 120 s and the hook answers by about 141 s, so no detached
  render is needed. Cursor keeps the last response of a field when several hooks
  answer (same page), so for these two tools the main entry stays silent and
  the authoring entry carries the boundary's deliveries.
- **Hero upload.** After `storyboard__publish`, the same entry uploads the hero
  scene's render (one PUT, 7 s). Off: `CARDINAL_STORYBOARD_HERO=0`.
- **Evidence token.** After `storyboard__create` / `add_act` / `preview`, the
  main entry stores the storyboard's evidence token under
  `~/.cardinal/evidence/<conversation>/token.json`.
- **Which server ran the tool.** Cursor's `postToolUse` names no MCP server
  ([cursor/cookbook#50](https://github.com/cursor/cookbook/issues/50), reproduced
  on CLI 2026.09.28). A tool named `mcp__cardinal__<tool>` names its server. For
  any other spelling (Cursor's matcher uses `MCP:<tool>`), the call counts as
  Cardinal's only when `afterMCPExecution` (which documents `mcp_server_name`)
  recorded the same tool and input for the `cardinal` entry
  (`~/.cursor/cardinal/mcp-calls/<conversation>.json`, 0600, 10 minutes). The
  hook waits up to 1.5 s for that record. Another server's tool of the same name
  is never taken for Cardinal's: the renderer and the token store act with the
  Cardinal key. Which spelling Cursor really sends is unverified.

After upgrading, re-run `python3 scripts/cardinal-connect` (no key is
minted; `--rotate --project` for project files) to register the new entries:
`afterMCPExecution`, the authoring `postToolUse` entry, and the 10 s
`sessionStart`.

## Skills and the brainstorm subagent

`skills/connect`, `disconnect` and `status` drive the scripts above. Each one
tells the agent that in Auto-review mode the script needs approval to run
outside the sandbox, because it touches the network and `~/.cursor`.
`skills/decision` only reports status: `on` / `off` are the user's, in their own
terminal. `skills/canvas` is the Canvas guide (core shared with the other agents,
synced from `common/canvas/`).

`agents/brainstorm.md` is the same divergent-ideas subagent as the Claude
plugin's, with `model: inherit` and `readonly: true`. Cursor plugins ship
subagents from `agents/` (*"Place them in the `agents/` directory"*,
[plugins reference](https://cursor.com/docs/reference/plugins)), and the manifest
lists `"agents": "./agents/"`. Subagent frontmatter (`name`, `description`,
`model`, `readonly`) follows [subagents](https://cursor.com/docs/agent/subagents).
If this directory isn't loaded as a Cursor plugin, copy the file to
`~/.cursor/agents/` (user subagents) to use it.

## Checks preview (opt-in)

Checks are reusable, evidence-backed decisions over telemetry (a contract the
user approves, approved teaching examples, per-trace results with evidence),
built on the `cardinal-behavior` MCP server. The preview is **off by
default**; turn it on or off in your own terminal:

```bash
python3 scripts/cardinal-checks on       # install the checks skill; start a new Cursor session
python3 scripts/cardinal-checks off      # remove it
python3 scripts/cardinal-checks status   # the setting, the installed skill, accepted checks
```

- **`cardinal-behavior` MCP server.** `cardinal-connect` registers it
  as a `cardinalManaged` `mcpServers.cardinal-behavior` entry in `~/.cursor/mcp.json` (not in a `--project` `.cursor/mcp.json`: a cloud agent has no local plugin to run; an unmanaged entry of that name is left alone), launched as `scripts/cardinal-behavior --runtime cursor`;
  `cardinal-disconnect` removes it. A connection made before the Checks
  preview has none: re-running `python3 scripts/cardinal-connect` (no
  `--rotate`) adds it without a new key, and `cardinal-checks on` / `status`
  print that command while it is missing. It is a local stdio server that Cursor
  starts outside its sandbox, so it can write the Checks ledger and read
  range runs that a sandboxed agent cannot. It reads this runtime's
  connection (`~/.cursor/cardinal.json` `mcp_url` + `cardinal-secrets.json`
  `mcp_api_key`); `CARDINAL_MCP_URL` with `CARDINAL_MCP_API_KEY` in its
  environment overrides them (a URL without its key does not). No key is
  written into the MCP registration. Its draft ledger and `enabled.json` are
  private (0600 files in 0700 directories).
- **The skill** (`cardinal-checks`, from `checks/skill/`: this plugin's
  SKILL.md over the shared `common/checks/CORE.md`) is installed into
  this plugin's `skills/checks/` (`plugin.json` `"skills": "./skills/"`) by `on` and removed by `off` (and by `cardinal-disconnect`). The SessionStart hook
  reconciles that copy with the setting and, while the preview is on and
  Cardinal is connected, adds the routing rule (answer with the query tools
  first; a check only for per-trace judgments). Off: no text at all.
- **Labels and reuse** go through MCP tools (`set_check_example`,
  `approve_check_example`, `remove_check_example`, `get_check_draft`,
  `lock_check_examples`, `list_accepted_checks`), never shell commands. The
  ledger is `~/.cursor/cardinal/checks/drafts/`; disconnect removes it.
- **Range runs** (`execute_behavior_range`) run in a detached worker; the
  agent follows one by polling `get_behavior_range` (each call waits up to
  30 s for new events, and a completed run's summary comes back in it).
  Claude Code listens with its Monitor tool instead; Cursor has no
  equivalent, so it polls.

Restart Cursor after `cardinal-connect` so it starts the new MCP server. Not
checked against a live Cursor (not installed where this was written); the
fixtures are `tests/test_cursor_checks.py`.

## Limits on Cursor

What Cursor can and can't do next to the Claude, Codex and Gemini CLI plugins,
feature by feature, with a citation for every limit: [`docs/parity.md`](../../docs/parity.md).

Each of these is a property of Cursor, not of the plugin:

- **Checks preview: a change takes effect at the next session; no skill
  reload or deny.** Claude Code's gate returns `reloadSkills` and denies its
  Skill tool while the preview is off. Cursor's `sessionStart` output is
  `additional_context` (and `env`), with no skill-reload field, and no hook
  event names a skill being used, so there is nothing to deny
  ([hooks docs](https://cursor.com/docs/agent/hooks)). `cardinal-checks on` /
  `off` apply to sessions started afterwards.

- **No per-session owner-input disclosure.** Cursor has no user-visible,
  non-blocking hook message (see [Owner input](#owner-input-opt-in-consent)).
  The compensation is the connect-time, versioned consent.
- **Advisories that arrive after the turn's last tool call wait** for the
  next tool call or the next session's `sessionStart`. `stop` can't deliver
  them: `followup_message` is submitted as the next *user* message, so advisory
  text would turn into owner input and start a turn by itself
  ([hooks docs](https://cursor.com/docs/agent/hooks)). `beforeSubmitPrompt` has
  no context output. The plugin never uses `followup_message`.
- **Another hook's `followup_message` may be recorded as owner input.** It is
  unverified whether a `followup_message` from another hook's `stop` fires
  `beforeSubmitPrompt`. If it does, that text reaches the owner-input capture
  like a typed prompt and could be recorded (with your consent on), since the
  payload does not say where the prompt came from.
- **No storyboard discovery at a subagent's start.** `subagentStart` answers
  `{permission, user_message}` only (`"ask"` counts as `"deny"`), with no
  `additional_context`, so a subagent can't be handed the discovery block when
  it starts ([hooks docs](https://cursor.com/docs/agent/hooks)). Staged items
  still reach it at its first `postToolUse`.
- **A subagent's tool call may get the session's advisories.** The tool-boundary
  delivery skips a payload that names a subagent (`agent_id` / `subagent_id`),
  but Cursor documents no subagent id on its tool hooks
  ([hooks docs](https://cursor.com/docs/agent/hooks)). If a subagent's
  `postToolUse` carries the parent conversation's id, the advisories can reach
  the subagent instead of the main agent. Not checked on a live Cursor.
- **No automatic context stamping on storyboard tools.** `beforeMCPExecution`
  answers with a permission only and can't rewrite the call's input; the agent
  passes `cardinal-storyboard context` (see [Storyboard associations](#storyboard-associations)).
- **Cloud agents** don't run `sessionStart` (*"Deferred while cloud agents can
  still start in a read-only environment"*) or the MCP hooks
  ([hooks docs](https://cursor.com/docs/agent/hooks)). There, no Investigation is
  bootstrapped, no guidance is given, the poller doesn't start, and a Cardinal
  tool named `MCP:<tool>` can't be identified, so no preview render or token
  store happens for it. Staged items still surface at `postToolUse`.
- **Context delivery is reported broken in some Cursor builds.** Cursor staff
  confirmed that `postToolUse` `additional_context` isn't delivered to the
  model: for non-MCP tools the response is discarded, and for MCP tools only
  `updated_mcp_tool_output` is used
  ([forum #156157](https://forum.cursor.com/t/cursor-hooks-additional-context-not-injected-in-agent-context-in-posttooluse/156157),
  [#155689](https://forum.cursor.com/t/native-posttooluse-hooks-accept-and-log-additional-context-successfully-but-the-injected-context-is-not-surfaced-to-the-model/155689),
  [#158168](https://forum.cursor.com/t/posttooluse-hooks-additional-context-not-injected-into-agent-model-context/158168)).
  `sessionStart` `additional_context` can be dropped by a race with composer
  creation
  ([forum #158452](https://forum.cursor.com/t/sessionstart-hook-additional-context-is-never-injected-into-agents-initial-system-context/158452)).
  No fix had been announced as of these threads. Every Cursor context channel
  this plugin uses (evidence ids, limits notices, decisions, investigation
  deliveries, preview PNG lists) depends on them. The canvas and storyboard
  skills tell the agent to render by hand when no preview report arrives.

## Cloud agents

Cursor cloud agents do **not** load `~/.cursor/hooks.json`. They only load `.cursor/hooks.json` at the repo root, plus team/enterprise hooks distributed centrally. To send Cardinal telemetry from cloud-agent runs:

```bash
cd path/to/your/repo
python3 /path/to/plugins/cardinal-cursor-plugin/scripts/cardinal-connect --project
```

This additionally writes `.cursor/mcp.json` and `.cursor/hooks.json` at your repo root. Commit them so cloud agents pick them up. Cursor's [hooks docs](https://cursor.com/docs/agent/hooks) (checked 2026-09-14) list `beforeSubmitPrompt`, `postToolUse`, `subagentStop`, `preCompact`, `afterAgentResponse`, `afterAgentThought`, and `stop` as supported in cloud agents. `sessionStart` is not supported: it is *"Deferred while cloud agents can still start in a read-only environment."* Cloud agents also start with read-only exploratory turns, and no hooks run during those.

What that means for Cardinal in cloud agents:

- **Initiative-convention prompt, budget standing, and decision-capture instructions:** not delivered, because they all ride on `sessionStart`.
- **Investigation sessions:** not bootstrapped (that is `sessionStart` too). See [Limits on Cursor](#limits-on-cursor).
- **Decision capture:** the agent is never told to record decisions. If it runs `cardinal-decision record` anyway, the recorder (`postToolUse`) does run in cloud agents, but only if the CLI and hook exist in the VM at the paths your hook config uses. The on/off switch and ledger it checks live under the VM's `~/.cursor/cardinal/`, not your laptop's, and `gh` there is usually unauthenticated, so the PR will be missing.
- **Spend-limits gate:** runs on `beforeSubmitPrompt`, so it can block or warn in cloud agents too.
- **`git_state` PR linkage:** `beforeSubmitPrompt` runs once hooks are active, but `gh` in the cloud VM is usually missing or not authenticated, so expect the PR keys to be absent there. Server-side branch → PR joins still apply.
- **Tool-level telemetry** (`postToolUse`, `subagentStop`, `preCompact`, `afterAgent*`): runs normally.

Note that `cardinal-connect --project` writes the absolute path of your local hook script into `.cursor/hooks.json`. That path has to exist inside the cloud VM for any of these hooks to run there.

## Install locally

This repository is a local Cursor plugin directory. Clone it, then run `cardinal-connect`:

```bash
python3 plugins/cardinal-cursor-plugin/scripts/cardinal-connect
```

The connect script prints a Cardinal approval URL, waits for approval, and writes:

| File | What gets written |
| --- | --- |
| `~/.cursor/mcp.json` | A managed `mcpServers.cardinal` entry with the Cardinal MCP URL and API-key header. Tagged `cardinalManaged: true`. |
| `~/.cursor/hooks.json` | Managed Cardinal hook entries for `sessionStart`, `beforeSubmitPrompt`, `postToolUse` (two entries: the main one, and the storyboard authoring one with a matcher and a 150 s timeout), `postToolUseFailure`, `afterFileEdit`, `afterMCPExecution`, `preCompact`, `stop`, `subagentStop`, `afterAgentResponse`, `afterAgentThought`. Each entry's `command` string embeds the marker `cardinal-cursor-plugin` for disconnect identification. |
| `.cursor/mcp.json` + `.cursor/hooks.json` at repo root | Same content as the user-level files (only with `--project`). |
| `~/.cursor/cardinal.json` | Non-secret state: org/user metadata, endpoint URLs, key ids, key prefixes, and config locations. |
| `~/.cursor/cardinal-secrets.json` | Local plaintext ingest/MCP keys needed by hooks and status probes; written mode `0600`. |

Restart Cursor after connecting so it reloads MCP and hook config.

## Scripts

```bash
python3 scripts/cardinal-connect
python3 scripts/cardinal-connect --host https://app.cardinalhq.io
python3 scripts/cardinal-connect --rotate
python3 scripts/cardinal-connect --telemetry-only
python3 scripts/cardinal-connect --project
python3 scripts/cardinal-connect --dry-run
python3 scripts/cardinal-connect --owner-input on|off
python3 scripts/cardinal-status
python3 scripts/cardinal-storyboard context --session-id <conversation id>
python3 scripts/cardinal-storyboard investigation link --session <conversation id>
python3 scripts/cardinal-disconnect
python3 scripts/cardinal-disconnect --force
python3 scripts/cardinal-checks on|off|status
python3 scripts/cardinal-wake enable|disable|status|run --dry-run|resume <session>
```

## Owner-woken sessions

`python3 scripts/cardinal-wake` (macOS; a per-user launchd agent) tells you
about owner decisions you post on an Investigation page addressed to a
session on this machine, and resumes a stopped one in your own terminal
when you ask.

```bash
python3 scripts/cardinal-wake enable                     # install the per-user launchd agent (macOS)
python3 scripts/cardinal-wake status                     # the setting, recent notifications, resume commands
python3 scripts/cardinal-wake run --dry-run              # what the next pass would do; notifies nothing
python3 scripts/cardinal-wake resume <session>           # resume it here (a unique 8+ hex prefix is enough)
python3 scripts/cardinal-wake resume <session> --print   # show the command and its directory only
python3 scripts/cardinal-wake disable                    # remove the launchd agent
```

**What reaches you.** Only an owner decision you post from your signed-in
Cardinal web session, addressed to a session on this machine, counts; never
anything an agent posts. The waker reads it straight from Cardinal with the
Cardinal key of the plugin whose session it is (this plugin's: `~/.cursor/cardinal.json` `mcp_url` + `cardinal-secrets.json`
`mcp_api_key`), never from
local files. Each decision is one macOS notification naming the runtime,
the session (first 8 characters of its id) and the Investigation (its id and
host), never the decision's text; at most 3 per session and 10 in all per
rolling day (the rest follow later). A decision posted without "Resume this
session if it has stopped", or older than 7 days, is notified the same way.

**The notification.** For a session whose window is still open it says to
switch to that window. For a stopped session it ends with
`Resume: ~/.cardinal/wake/lib/cardinal-wake resume <first 8 of the id>`:
run that in your terminal, or install
[terminal-notifier](https://github.com/julienXX/terminal-notifier)
(`brew install terminal-notifier`) and click the notification: Terminal
opens and runs it (through a private `~/.cardinal/wake/resume/<id>.command`).

**`resume` (experimental for Cursor).** The waker recognizes a Cursor
session by its records under `~/.cursor/projects/*/agent-transcripts/` or
`~/.cursor/chats/*/<id>/` (best effort; otherwise the notification says the
runtime is unknown). `resume` takes the chat's working directory only from
those records (never from the Investigation binding) and runs, there,
`cursor-agent --resume <session> "<wake prompt>"`. Only a binary named
`cursor-agent` is used (never `agent`, an unrelated tool on some machines),
and this resume has not been verified against Cursor yet. When
`cursor-agent` is not installed, or the records do not say where the chat
ran, it tells you to reopen the chat in Cursor (its project, then the
Agent panel's history) instead.
The wake prompt names the decision's number and Investigation (never its
text) and is listed as a machine prompt, so the resumed turn is never
recorded as owner input. The resumed session also receives the decision
through this plugin's investigation hooks, as any session does.
`resume --print` shows the command and its directory without running them;
`status` lists candidate sessions with their resume commands.

**Nothing is ever started unattended.** The waker only notifies; you resume,
in your own terminal, with your own settings and permissions, and you see
everything the session does ([`docs/specs/resume-and-wake.md`](../../docs/specs/resume-and-wake.md) v4). `enable --launch`, from earlier
versions, is refused with a one-line message. `enable` copies this plugin's
`cardinal_core` and a small runner into `~/.cardinal/wake/lib/` (every enable
copies them again, so a plugin upgrade never leaves the agent pointing at a
removed version) and runs it every 45 s with a Python that resolves outside
your home directory, your repositories, temp dirs and version-manager shims,
in isolated mode (`-I -S`), with a system-only PATH. `disable` removes the
launchd agent.

After updating the plugin, run `cardinal-wake enable` again: it refreshes the
stable copy in `~/.cardinal/wake/lib/` (until then `status` says the
installed runner is out of date, and notifications ask for it instead of
naming the resume command).

## Known Cursor issues

The plugin exercises hooks that currently have open (auto-closed) bug reports on the Cursor forum. None of these are the plugin's bug; they'd be fixed upstream:

- Blocked messages (`beforeSubmitPrompt` returns `continue: false`) still land in later LLM context ([forum #153318](https://forum.cursor.com/t/blocked-messages-beforesubmitprompt-hook-returns-continue-false-are-still-included-in-later-llm-context-history/153318)).
- Double-popup when `continue: false` + `user_message` ([forum #150091](https://forum.cursor.com/t/double-popup-issue-with-beforesubmitprompt-hook/150091)).
- `beforeShellExecution` `allow` / `ask` permissions ignored — only `deny` respected ([forum #144244](https://forum.cursor.com/t/beforeshellexecution-hook-permissions-allow-ask-ignored-allow-list-takes-precedence/144244)). The Cardinal plugin does not use the shell-execution permission channel.

## Requirements

- Cursor with hooks (v1) and MCP server config support.
- Python 3.11+.
- A Cardinal account.

## License

Apache 2.0. See [LICENSE](./LICENSE).

## Repository associations across a session

Repository provenance is shared across Claude Code, Codex, Cursor and Gemini.
Starting in a parent workspace does not prevent later repository attachment:
explicit tool working directories, file arguments and successful edits add
repository labels locally. The background poller submits them to Cardinal.
The original checkout context remains unchanged; later repos are additive
`written_from` provenance, never automatically asserted as the storyboard's
subject. Labels contain canonical repository names, never local paths.

To backfill an existing session from its saved edited-file ledger, run:

```sh
cardinal-storyboard investigation repos --session SESSION_ID
```

Supply `--cwd /path/to/checkout` (repeatable) to attach additional checkouts.
This command uses the session's existing investigation and storyboard. It does
not publish or share anything. Automatic collection respects
`CARDINAL_STORYBOARD_CONTEXT=0`, and only an author's own session may sync.
At most 32 repositories are retained. Shell command text is not parsed: use
explicit tool working directories or the CLI for shell-only checkout changes.
Historical repair requires a retained local edit ledger or explicit checkouts.

Multiple labels and browser grouping require Cardinal's
`repository_provenance` capability (the companion Conductor migration/server
change). Older servers continue to accept the existing context request, and
the CLI reports when full multi-repo support is unavailable. Every adapter's
next patch release includes this same shared implementation; the parity matrix
and packaged-artifact tests guard against adapter drift.
