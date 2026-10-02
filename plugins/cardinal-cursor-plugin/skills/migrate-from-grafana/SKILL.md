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

Cursor's `cardinal-connect` grants an MCP key and a control-plane token with
`dashboards:write alerts:write telemetry:query`, so with it the whole migration runs
without a login token, for any org the user belongs to, and `--orgs` lists them.
A connection made before the plugin requested that token (or with
`--minimal-scopes`) lacks it: `--orgs` exits 4 — reconnect as in step 2 below with
`--rotate`.

The connection is optional: if the user doesn't want it, take CORE.md's
`.env.cardinal` route. If they do and `--orgs` exits 3 (not connected) or 4:

1. Find the connect script — `CONNECT=$(command -v cardinal-connect || find ~/.cursor . \
   -path '*/scripts/cardinal-connect' 2>/dev/null | head -1)`. If there is none, use
   the `.env.cardinal` route.
2. `rm -f ~/.cursor/cardinal-pending.json`, then start
   `python3 "$CONNECT" dashboards:write alerts:write telemetry:query` (exit 3) or,
   after the user agrees, `python3 "$CONNECT" --rotate dashboards:write alerts:write telemetry:query`
   (exit 4; it replaces the current keys) so it doesn't block you (in the background
   if your shell tool supports it, else
   `nohup python3 "$CONNECT" ... > cardinal-connect.log 2>&1 &`) — it waits up to 10
   minutes for approval. Add `--host <their Cardinal URL>` for a self-hosted Cardinal.
3. Within a few seconds it writes `~/.cursor/cardinal-pending.json`; read
   `verification_uri` from it (retry a few times, 1 s apart) and show it: "To connect
   Cursor to Cardinal, open this link, log in, pick an org, and click **Approve**:
   `<verification_uri>`". Mention that approving also sends Cursor's usage
   telemetry to that Cardinal org, and that `cardinal-disconnect` undoes it.
4. Wait for it to finish, then run `--orgs` again and continue with CORE.md step 0b.
   On failure, show its error verbatim; for "already connected", ask before
   re-running with `--rotate`. If it reports the scopes as not granted, the Cardinal
   server doesn't offer them yet: take the `.env.cardinal` route for the writes.

`cardinal-status` lists the granted scopes under Actions.

## Report

If step 0 connected them just now: tell them to restart Cursor to get the
Cardinal MCP tools in chat; `cardinal-disconnect` undoes the connection.
