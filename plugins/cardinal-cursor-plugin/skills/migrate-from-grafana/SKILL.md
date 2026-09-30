---
name: cardinal-migrate-from-grafana
description: Migrate Grafana dashboards and alert rules into Cardinal (Maestro dashboards + lakerunner alert rules). Use whenever someone wants to move, copy, import, port or switch their Grafana (Cloud, OSS or Enterprise) dashboards or alerts to Cardinal / CardinalHQ / lakerunner / Maestro, bring existing Grafana monitoring over when adopting Cardinal, or check what of their Grafana setup would carry over — even if they only say "move my grafana stuff to cardinal". Not for teams starting fresh on Cardinal with nothing in Grafana, and not for moving raw telemetry data.
---

# Cardinal Migrate from Grafana

This SKILL.md is the **Cursor-specific** part of the skill: where the scripts
live, how to connect, and what to say about restarting. The workflow itself — what
to ask for, credentials, steps 0–6 — lives in `CORE.md`, co-located in this
directory. **Read `CORE.md` in full before starting**, then use the sections below
wherever it points to SKILL.md.

## Scripts and agent home

Locate the scripts once and reuse `$SCRIPTS` in every step. Export the agent home
so the scripts read Cursor's connect state:

```bash
export CARDINAL_AGENT_HOME=~/.cursor
SCRIPTS=$(dirname "$(find ~/.cursor . -name convert.py \
  -path '*migrate-from-grafana/scripts*' 2>/dev/null | head -1)")
[ -f "$SCRIPTS/convert.py" ] || { echo "migrate-from-grafana scripts not found"; exit 1; }
```

The connect state is in `~/.cursor/cardinal.json` and `~/.cursor/cardinal-secrets.json` —
never print either.

## Connect (step 0a)

Cursor's `cardinal-connect` grants an MCP key, but **not** the control-plane token
Claude Code's connect mints with `dashboards:write alerts:write telemetry:query`.
What that means for CORE.md:

- **`--orgs` can't list orgs** — it exits 4 even when connected. Ask the user for the
  org ID instead (CORE.md step 0b) and write it into `.env.cardinal`.
- **`--check`** works through the MCP key for the connected org only; for another
  org it exits 6 and needs the login token.
- **Writing dashboards and alert rules always needs the login token** (or an
  `admin:all` API key) in `.env.cardinal` — ask for it right before step 2, as
  CORE.md describes. Ignore CORE.md's `--rotate <scopes>` advice; it doesn't apply here.
- **Switching alert rules on/off** goes through the MCP key, so it works for the
  connected org.

The connection is optional: if the user doesn't want it, take CORE.md's
`.env.cardinal` route. If they do and `--orgs` exits 3 (not connected):

1. Find the connect script — `CONNECT=$(command -v cardinal-connect || find ~/.cursor . \
   -path '*/scripts/cardinal-connect' 2>/dev/null | head -1)`. If there is none, use
   the `.env.cardinal` route.
2. `rm -f ~/.cursor/cardinal-pending.json`, then start `python3 "$CONNECT"` so it
   doesn't block you (in the background if your shell tool supports it, else
   `nohup python3 "$CONNECT" > cardinal-connect.log 2>&1 &`) — it waits up to 10
   minutes for approval. Add `--host <their Cardinal URL>` for a self-hosted Cardinal.
3. Within a few seconds it writes `~/.cursor/cardinal-pending.json`; read
   `verification_uri` from it (retry a few times, 1 s apart) and show it: "To connect
   Cursor to Cardinal, open this link, log in, pick an org, and click **Approve**:
   `<verification_uri>`". Mention that approving also sends Cursor's usage
   telemetry to that Cardinal org, and that `cardinal-disconnect` undoes it.
4. Wait for it to finish, then continue with CORE.md step 0b. On failure, show its
   error verbatim; for "already connected", ask before re-running with `--rotate`.

## Report

If step 0 connected them just now: tell them to restart Cursor to get the
Cardinal MCP tools in chat; `cardinal-disconnect` undoes the connection.
