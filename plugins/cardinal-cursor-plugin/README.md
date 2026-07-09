# Cardinal Cursor plugin

Connect Cursor to Cardinal telemetry and the unified MCP endpoint in one browser-approved consent.

This is a Cursor-native port of the command surface shared by the [Claude Code plugin](https://github.com/cardinalhq/cardinal-claude-plugin) and [Codex plugin](https://github.com/cardinalhq/cardinal-codex-plugin):

| Script | What it does |
| --- | --- |
| `cardinal-connect` | Runs Cardinal's device-code flow, mints ingest and MCP keys, writes managed `~/.cursor/mcp.json` + `~/.cursor/hooks.json`, and (with `--project`) additionally writes `.cursor/mcp.json` + `.cursor/hooks.json` at the repo root for cloud-agent coverage. |
| `cardinal-status` | Shows the recorded Cardinal workspace and probes the configured ingest and MCP endpoints. |
| `cardinal-disconnect` | Best-effort revokes Cardinal keys, removes managed Cursor config entries at both user and project locations, and deletes local state. |

## Telemetry scope

Cursor does not expose Claude Code's native OpenTelemetry emitter. This plugin emits Cardinal-compatible telemetry from Cursor hooks, sending the same Lakerunner event contract used by the Claude and Codex plugins where Cursor exposes equivalent data (see `docs/specs/cursor-parity.md` at the repository root for the full parity map):

- `cardinal.git_state` from the active Git checkout on `beforeSubmitPrompt`, including initiative classification from the branch name (worktree noise stripped) and slash-command detection.
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

State lives under `~/.cursor/cardinal/` (telemetry progress cursors, plan stamp, limits verdicts + notify staging); `cardinal-disconnect` removes it.

## Cloud agents

Cursor cloud agents do **not** load `~/.cursor/hooks.json`. They only load `.cursor/hooks.json` at the repo root, plus team/enterprise hooks distributed centrally. To send Cardinal telemetry from cloud-agent runs:

```bash
cd path/to/your/repo
python3 /path/to/plugins/cardinal-cursor-plugin/scripts/cardinal-connect --project
```

This additionally writes `.cursor/mcp.json` and `.cursor/hooks.json` at your repo root. Commit them so cloud agents pick them up. Note that `sessionStart`, `beforeSubmitPrompt`, and `stop` do not fire in Cursor cloud agents (per Cursor's docs), so the initiative-convention prompt and the spend gate do not run there. Tool-level telemetry (`postToolUse`, `subagentStop`, `preCompact`) does.

## Install locally

This repository is a local Cursor plugin directory. Clone it, then run `cardinal-connect`:

```bash
python3 plugins/cardinal-cursor-plugin/scripts/cardinal-connect
```

The connect script prints a Cardinal approval URL, waits for approval, and writes:

| File | What gets written |
| --- | --- |
| `~/.cursor/mcp.json` | A managed `mcpServers.cardinal` entry with the Cardinal MCP URL and API-key header. Tagged `cardinalManaged: true`. |
| `~/.cursor/hooks.json` | Managed Cardinal hook entries for `sessionStart`, `beforeSubmitPrompt`, `postToolUse`, `preCompact`, `stop`, `subagentStop`, `afterAgentResponse`, `afterAgentThought`. Each entry's `command` string embeds the marker `cardinal-cursor-plugin` for disconnect identification. |
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
python3 scripts/cardinal-status
python3 scripts/cardinal-disconnect
python3 scripts/cardinal-disconnect --force
```

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
