# migrate-from-grafana — shared core

**This file is the shared core of the migrate-from-grafana skill.** It is copied
verbatim, with `scripts/` and `references/`, into each adapter's
`skills/migrate-from-grafana/` by `build/sync_skills.py`. Do not edit the copies —
edit the canonical files under `common/migrate-from-grafana/` and re-run the sync.

The adapter's `SKILL.md` (read it first) supplies what differs per agent:

- **`$SCRIPTS`** — how to locate this skill's `scripts/` directory.
- **`CARDINAL_AGENT_HOME`** — the agent's home dir (`~/.claude`, `~/.codex`, …),
  exported before running any script, so the scripts read that agent's
  `cardinal-connect` state.
- **Connect** — how to run the agent's `cardinal-connect` (step 0a), and which
  Cardinal scopes it can grant.
- **Report** — what to tell the user about restarting the agent.

Moves **dashboards and alert rules** from a Grafana instance into a Cardinal org.
It does not move historical telemetry — Cardinal only shows data that is sent to it,
so the services must already be shipping OTLP to Cardinal (the migration is about
the *views and rules*, which is why metric names get checked against Cardinal's
catalog before anything is converted).

**Gate: data must already be flowing to Cardinal.** Before anything else — before
asking for Grafana credentials — step 0 checks that the target org is receiving
data. If it is, carry on with the migration as written. If it isn't, pause and send
the user to set up data onboarding first (step 0c says how, for Cardinal SaaS and
self-hosted), wait for them to say it's done, re-check, and continue in the same
session once data shows up.

Migration is optional. If the user has no Grafana to migrate from (a fresh start on
Cardinal), say there's nothing to migrate and stop — Cardinal's own dashboard
authoring and `manage_alert_rules` MCP tool are the right path for them.

## What you need from the user

| Item | Why | Notes |
|---|---|---|
| Cardinal connection (`cardinal-connect`) | lists their orgs; "is Cardinal receiving data?" check; switches alert rules on/off | Step 0 starts it for the user if they aren't connected; they only approve an `app.cardinalhq.io` link in the browser. Where the agent's connect can grant `dashboards:write alerts:write telemetry:query` (step 0 asks for them — see SKILL.md), its token also **reads the catalog, creates and updates dashboards and alert rules, and runs validation** — every script uses it when no login token is set, for any org the user belongs to. |
| Which Cardinal org | where everything goes | **Always ask** (step 0) — many users have several orgs, and the connected org is not necessarily the target. |
| Cardinal login token (fallback) | only when the agent's connect isn't available or can't grant the three scopes (SKILL.md says which), or is connected without them (older Cardinal) | `CARDINAL_TOKEN` in `.env.cardinal`. Carries the user's org role: **Member** (or Owner) can create dashboards and alert rules. **Lives only ~5 minutes** — see below. Alternative that doesn't expire: an org API key with `admin:all` scope (`CARDINAL_API_KEY`; only a Cardinal superadmin can mint one). |
| Grafana URL + service account token | read dashboards, alert rules, datasources | **Viewer** role is enough. Administration → Users and access → Service accounts → Add → Viewer → Add token (`glsa_…`). |
| Which dashboards/alerts | scope | a folder, a tag, specific dashboard UIDs, or "everything" |
| Alert rules on or off | whether migrated rules evaluate immediately | ask at the dry run (step 4) |

Credentials are secrets: have the user put them in env files (below) instead of
pasting them into chat. Create the files for them with empty values and `chmod 600`,
but **open in an editor (`open -e <file>` on macOS) only a file the user actually has
to fill in**. Normally that is just `.env.grafana-migrate` (`GRAFANA_URL`,
`GRAFANA_TOKEN`). `.env.cardinal` is yours to fill (`CARDINAL_ORG_ID` from step 0), so
don't open it — unless a script exits asking for `CARDINAL_TOKEN` (the fallback
below), and only then. Never echo token values
back — when checking a file, mask them (e.g. `sed -E 's/(TOKEN|KEY)=(.{6}).*/\1=\2…/'`).
Never print the agent's `settings.json` / `cardinal*.json` files (under
`$CARDINAL_AGENT_HOME`) either; the scripts read what they need from them.

```
# .env.grafana-migrate
GRAFANA_URL=https://<stack>.grafana.net
GRAFANA_TOKEN=

# .env.cardinal
CARDINAL_ORG_ID=<the org chosen in step 0 — fill this in yourself>
CARDINAL_TOKEN=
CARDINAL_API_KEY=
# only when not using cardinal-connect:
# CARDINAL_URL=https://app.cardinalhq.io
```

**Skip the login token when step 0 connected with all three scopes** — `cardinal-status`
lists them under Actions, and the scripts print `using the cardinal-connect token`.
Only fall back to it when a script exits asking for `CARDINAL_TOKEN`.

**The login token lives about 5 minutes**, and a migration takes longer, so expect to
ask for a fresh one several times. How to copy it so it isn't cut off (the Headers
pane in dev tools truncates long values): sign in to Cardinal, switch to the target
org, reload, open dev tools → Network, click Dashboards, **right-click** any
`/api/orgs/...` request → **Copy → Copy as cURL**, paste that anywhere and take
everything after `Bearer ` up to the closing quote. If the header says `CardinalDemo`
instead of `Bearer`, they're in Cardinal's public demo, which is read-only — they need
a real login.

Every script checks the token before sending anything and **exits 5** when it is
expired or cut off (it prints the reason and how long a valid one has left). On exit
5: say which step you're on, ask the user to save a fresh token and reply, then re-run
the same command — every step is safe to re-run. Ask for the token right before
step 2, and once they're in, keep moving between steps without waiting on the user.

Keep all migration files in one working directory (e.g. `./grafana-migration/`).
The scripts ship with this skill and need only Python 3.9+ (standard library).
Locate them once as `$SCRIPTS` (SKILL.md shows how) and reuse it in every step.

**Never modify the scripts while running a migration** — nothing under `$SCRIPTS`
(or any other copy of this skill) gets edited, patched or rewritten, even when a
script looks buggy. Running the migration and changing its code are separate jobs.
If a script fails or misbehaves: stop that item, show the error verbatim, explain
the likely cause, and offer the user options (a manual workaround in Cardinal's UI,
skipping the item, or reporting the bug to the skill maintainers). The only files
you change are the migration's own working files (`.env.*`, `mapping.json`, and
what the scripts write under `export/`, `catalog/`, `plan/`).

## Workflow

### 0. Connect, pick the org, confirm Cardinal is receiving data

Do this first, before asking for anything Grafana-side — it is the data-flowing gate
above (0c).

**a. Connected?** Run `python3 $SCRIPTS/cardinal_catalog.py --orgs`. It lists the
user's Cardinal orgs and marks the connected one. If it exits 3 (not connected) or 4
(needs renewing, or this agent's connect has no org-listing token), follow SKILL.md's
**Connect** section — connect for the user rather than making them run it separately.

The migration scripts read the new connection straight from disk, so there is no
need to restart the agent now. The connect script's own "restart" advice only
concerns the Cardinal MCP tools in chat — pass it on in the report.

**b. Which org?** Show the `--orgs` list and **ask which org to migrate into**, even
when there is only one (then just confirm it). Never assume the connected org. Write
the chosen org's id into `.env.cardinal` as `CARDINAL_ORG_ID` when you create it; all
scripts use it. When `--orgs` can't list them (SKILL.md says for which agents), ask
for the org ID instead — it's in the `/api/orgs/<org-id>/...` request URL in Cardinal.

**c. Is it receiving data?**

```bash
python3 $SCRIPTS/cardinal_catalog.py --env-file .env.cardinal --check [--instance <slug>]
```

"Receiving data" means a collector (OpenTelemetry Collector, Alloy, any OTLP
SDK/exporter) sent a metric in the last 15 minutes. Two things don't count, and the
script ignores them: metric names stored from an earlier send (Cardinal keeps them
after the source stops), and the agent's own usage telemetry, which connecting
switches on (series with an `agent_runtime` label). So a freshly connected org with
nothing else sending fails the check, as it should.

It needs the `telemetry:query` scope (with it, it works for any of the user's orgs,
no login token) or the login token. Connected without it: exit 4 — reconnect with
`--rotate` and the three scopes (SKILL.md's **Connect**); for an org that isn't the
connected one it exits 6 — reconnect the same way, or run it again right after the
user adds the login token.

**Several data lakes** (exit 7): the script lists each one and whether it is receiving
data. **Ask the user which data lake their collector sends to** — never pick one
yourself, not even the only one with data: another lake's data may belong to
someone else (a demo, another team), and the migrated dashboards would show it.
Then re-run `--check --instance <their choice>` and use the same `--instance` in
step 2.

- **Receiving data** (`Cardinal is receiving data on …`): continue to step 1.
- **Not receiving data** (exit 1: no data lake, no metrics, or nothing current from a
  collector): **pause the migration** — migrated dashboards would all be empty. Don't
  get around it by checking a data lake the user didn't choose. The user's services need to send telemetry
  to Cardinal first; that is a data-onboarding step, not a migration one. What to tell
  them depends on where their Cardinal runs:
  - **Cardinal SaaS** (the hostname of the connection's host / `CARDINAL_URL` is
    `app.cardinalhq.io`, whatever the scheme or path; it's the default): ask them to follow the setup steps at
    <https://docs.cardinalhq.io/data-lake/instrumentation#get-an-api-key> — get an API
    key and point their OpenTelemetry collector / SDKs at Cardinal.
  - **Self-hosted Cardinal** (connected with `--host <their URL>`, or the hostname is
    anything other than `app.cardinalhq.io`): ask them to set up their site and data lake with the Cardinal
    plugin's **install-site** skill (`/cardinal:install-site` in Claude Code,
    `cardinal-install-site` in Codex, Cursor and Gemini CLI), then send telemetry to it.
  - Not sure which one they're on: ask.

  Then **wait in this session**: ask them to reply once it's set up (data can take a
  few minutes to show up after the first send). Don't ask for Grafana credentials or
  export anything meanwhile. When they reply, re-run the `--check` above:
  - Receiving data now: say so in one line and continue to step 1. Steps 0a/0b are
    done and the org stays the same.
  - Still no data: say so, and help them work out why before asking them to try again.
    Is the collector/SDK running? Is it pointed at the right endpoint with the API key
    for **this** org? Self-hosted: did install-site finish with the data lake up? Then
    wait and re-check again.
  - They'd rather do it later: end here. Tell them to re-run this migration once data
    is flowing; step 0 will confirm it.

If the user can't or won't connect: ask for the org ID (it's in the
`/api/orgs/<org-id>/...` request URL) and `CARDINAL_URL`, put both in `.env.cardinal`
with the token, and run the `--check` above.

### 1. Export from Grafana

```bash
python3 $SCRIPTS/grafana_export.py --env-file .env.grafana-migrate --out export \
    [--folder "Team X"] [--tag prod] [--uid <dash-uid>]
```

Writes `export/dashboards/*.json`, `export/alerts.json`, `export/datasources.json`.
Show the user the list of dashboards and the alert count and confirm the scope
before continuing — migrating the wrong folder is the most common mistake. If alert
export fails (older Grafana, or no permission), continue with dashboards and say so.

### 2. Read Cardinal's catalog and build the name mapping

```bash
python3 $SCRIPTS/cardinal_catalog.py --env-file .env.cardinal --export export --out catalog \
    [--instance <slug>]
```

Pass the data lake the user chose in step 0 as `--instance` (with several data lakes
and no `--instance` it exits 7, as in step 0). The alert rules are created on this
data lake too.

This lists Cardinal's real metric and label names and writes
`catalog/mapping.suggested.json`: for every metric the Grafana queries use, the
Cardinal name it most likely corresponds to (Prometheus-style names in Grafana
carry `_total` / unit suffixes that lakerunner may not). It also measures how this
Cardinal instance answers the queries the converter will write, instead of assuming:
`histogram_quantile` per histogram (`histogram_quantile_ok`), how it counts requests
from a histogram (`histogram_rate_mode`) and whether `or vector(0)` works
(`supports_or_vector`).

Then **review the mapping** — this is where migrations go wrong silently, because a
wrong name produces a dashboard that renders but shows "No data":

- Look at every entry in `_review`. For `no Cardinal metric matched`, check
  `similar_in_cardinal` and `catalog/metrics.json`; set the right name, or `null` if
  the metric genuinely isn't sent to Cardinal (those panels/rules get skipped and
  reported, which is better than an empty panel).
- For renamed metrics, sanity-check they're the same thing (same service, same unit).
- If a metric is missing only because the service isn't sending to Cardinal yet,
  tell the user — that's a data-onboarding gap, not a migration problem.
- Copy the reviewed result to `mapping.json` (drop the `_review` key).

Also check:
- `native_histograms`: Cardinal stores these histograms under the base name only (no
  `_bucket/_sum/_count`). There `rate()`/`sum()` read the sum of observed values, not
  the request count; the converter uses `histogram_count`, `histogram_avg` and
  `histogram_quantile` instead (`references/translation-rules.md`).
- `histogram_rate_mode`: `per_second` is Prometheus semantics. `per_minute` means
  lakerunner counts per 60s rollup: request-rate panels divide by 60 and are right at
  1-minute resolution or finer but overstated in zoomed-out views. Tell the user.
- `histogram_quantile_ok`: `false` for a histogram means Cardinal gives no usable
  percentile for it (e.g. negative values when observations are 0): its p50/p95/p99
  panels show the average and are retitled "avg". Tell the user which ones.
- **Filters are never removed for you.** `_review` entries with a `suggested_drop`
  are filters that match nothing in Cardinal: a label that isn't on that signal
  (`"cluster"`), or a value not seen there in the last 7 days (`"env=qa"`). As written,
  those queries show no data, and validation reports them as `missing`. Values seen
  rarely (an error status when nothing failed lately) are normal: leave those alone.
  For each one, in this order:
  1. The same thing under another name in Cardinal (e.g. `k8s_cluster_name`)? Map it
     in the mapping's `labels`. That keeps the query as narrow as in Grafana.
  2. Otherwise, **ask the user** whether to widen those queries. Removing a filter can
     pull in other environments, services or status codes (a log panel can start
     showing every service's logs). Only on a yes, add the `suggested_drop` value to
     `drop_labels` (metric queries) or `drop_log_labels` (log queries). `"label"`
     removes every `=`/`=~` filter on it, `"label=value"` only that one. Negative
     filters (`!=`, `!~`) always stay.
  Even then, alert rules that would lose a filter are **not migrated** (they would
  fire on data the Grafana rule ignores), and a log query whose stream selector would
  end up empty is skipped rather than reading every service's logs.

If the catalog comes back empty, the org isn't receiving data yet; stop and handle it
as in step 0c (SaaS: the API-key setup doc; self-hosted: install-site).

### 3. Convert (offline)

```bash
python3 $SCRIPTS/convert.py --export export --mapping mapping.json --out plan
```

Produces `plan/dashboards/*.json` (Cardinal dashboard specs), `plan/alerts.json`
(lakerunner rule specs) and `plan/report.json` (every panel and rule marked
`migrated`, `adapted` or `skipped`, with the reason). See
`references/translation-rules.md` for exactly what gets converted, adapted or
dropped — read it when a result looks surprising or the user asks why something
changed.

Summarise the report for the user before writing anything: counts per status, and
every **adapted** item whose meaning changed, especially:
- percentile panels showing the **average** (retitled "avg"), and percentiles Cardinal
  estimates from its sketch (bucket resolution, so they can differ from Grafana's);
- request rates divided by 60 (`per_minute` mode): right at 1-minute resolution only;
- removed filters (wider queries), and the alert rules skipped because of them;
- ad-hoc filters added to every query, and variables whose "All" got an extra matcher
  to stay as narrow as in Grafana;
- Grafana transformations, hidden series and repeated panels/rows that don't carry
  over (the panel can show more than in Grafana);
- `or vector(0)` panels that show no data instead of 0.
Alert rules get the same translations, and their names are kept: say when a rule that
says "p95" now compares the average. List every **skipped** item with its reason.

### 4. Dry run

```bash
python3 $SCRIPTS/cardinal_apply.py --env-file .env.cardinal --plan plan --catalog catalog
```

Shows every dashboard and alert rule that would be created or updated (same-name
objects are updated in place, so re-running is safe). Migrated objects keep their
Grafana names — **don't add a name prefix** by default. Only if the dry run shows an
*update* of something that isn't from an earlier run of this migration (it would be
overwritten), ask the user whether to overwrite it or use `--name-prefix "<prefix>"`
(then on every apply/verify call below).

Before step 5, get an explicit go-ahead, and in the same question ask whether the
alert rules should be created **enabled** (they start evaluating immediately, as in
Grafana) or **disabled** (created and validated, switched on later in Cardinal's
Alerts page). Rules that were paused in Grafana are always created disabled. It
writes into the user's Cardinal org, so don't start without the answer.

### 5. Migrate and validate, one item at a time

The user should watch the migration happen item by item: migrate dashboard 1,
validate dashboard 1, migrate dashboard 2, validate dashboard 2, … then the alert
rules the same way. Each step is its own command, so each shows up in the session
as it happens. Get the order first:

```bash
python3 $SCRIPTS/cardinal_apply.py --plan plan --list
```

Then, for each dashboard `i` of `N` in that order (`<uid>` from the list):

1. Say one line: **Dashboard i/N — "<name>": migrating…**
2. Migrate it (command description, where the agent shows one: `Migrate dashboard i/N: <name>`):
   ```bash
   python3 $SCRIPTS/cardinal_apply.py --env-file .env.cardinal --plan plan --catalog catalog --apply --dashboard <uid>
   ```
3. Say one line: **Dashboard i/N — "<name>": validating…**
4. Validate it (command description: `Validate dashboard i/N: <name>`):
   ```bash
   python3 $SCRIPTS/cardinal_verify.py --env-file .env.cardinal --plan plan --catalog catalog --dashboard <uid> \
       --grafana-env .env.grafana-migrate --export export --mapping mapping.json
   ```
   It checks the dashboard exists in Cardinal, runs every panel query (variables
   set to "All") and **compares the values with the Grafana panels they came from**
   over the last 15 minutes. A query that returns data can still count the wrong
   thing; this catches it. Ends with `RESULT: PASS | WARN | FAIL`. A panel that
   disagrees with Grafana (`differs`), lacks series Grafana has (`missing`) or has
   series Grafana doesn't (`extra`, e.g. a removed filter let other sources in) makes
   it WARN. No data where Grafana has none either is fine.
5. Say one line with the outcome and the link, e.g.
   `✓ PASS — 8/8 panels have data, 8/8 match Grafana → <link>` or
   `⚠ WARN — 8/8 have data, "Requests / sec" is ×0.14 of Grafana → <link>`,
   then go straight on to the next dashboard.

Then the alert rules, the same way, with `--alert <number>` (from `--list`) and
"Alert rule i/N" in the lines and descriptions — adding `--disable-alerts` to the
apply command if the user chose disabled:

```bash
python3 $SCRIPTS/cardinal_apply.py --env-file .env.cardinal --plan plan --catalog catalog --apply --alert <n> [--disable-alerts]
python3 $SCRIPTS/cardinal_verify.py --env-file .env.cardinal --plan plan --catalog catalog --alert <n> \
    --grafana-env .env.grafana-migrate --export export --mapping mapping.json
```

Validation reads the rule back from Cardinal: it must exist, be enabled/disabled as
chosen, and its query must return data. It also compares the rule's query with the
Grafana rule's over the last 15 minutes: a wider query returns *more* data, so a rule
reading other series (`extra` / `differs`) is WARN, not PASS. Switching a rule off goes through the
`cardinal-connect` MCP key (a plain REST update is accepted but doesn't take effect),
so when the target org isn't the connected one the apply line says the switch
failed — tell the user to switch those rules off in Cardinal's Alerts page.

Keep the between-step lines to one line each; save explanations for the report.
Rules for the loop:

- **WARN / validation FAIL:** note it and keep going. Fix these after the loop (below).
- **Exit 5 (token expired or cut off):** ask for a fresh token, then re-run the same
  command and carry on from that item. Expected several times per migration.
- **Apply error (403/422):** stop the loop — it will fail for every remaining item.
  403 on dashboards: the token can't write (Viewer role, or an API key without
  `admin:all`). 403 on alerts: the user is a Viewer (or the Cardinal host predates Member-level alert writes, where only Owners can). `insufficient_scope`: the connect token lacks a scope this step needs — reconnect with `--rotate dashboards:write alerts:write telemetry:query`. 422 *"Alerting is not
  connected for this integration (status='disabled')"*: alerting is switched off on
  that data lake. The error lists the org's other data lakes: if one of them holds the
  same data (check with the user), continue the alerts there with `--instance <slug>`
  on both apply and verify; otherwise an org admin has to switch alerting on, and the
  alerts wait. Report these messages verbatim and don't retry blindly.

After the loop, for items with problems: a query **ERROR** is a translation problem —
fix the mapping or query, re-run `convert.py`, then migrate + validate just that item
again (it updates in place). **No data** while Grafana has data usually means a wrong
metric/label mapping or data that isn't flowing to Cardinal; say which. **`differs`**
means the query measures something else (wrong function or metric) or the data
differs; **`extra`** usually means a removed filter widened the query. Never call a
dashboard correct on "returns data" alone when the comparison disagrees.

Note: Cardinal's query API is not the Prometheus HTTP API. Queries go to
`POST /api/lakerunner/<instance>/query/{metrics|logs}/query` with `{q, s, e, step}`
(times in ms, as strings) and stream back as SSE; `cardinal_catalog.NativeQuery`
wraps this if you need to test a query by hand.

### 6. Report

Give the user a short migration report, built from `plan/applied.json`,
`plan/verify.json` and `plan/report.json`:

- Dashboards: name → Cardinal link (`{CARDINAL_URL}/dashboards/{id}`), panels migrated/adapted/skipped
- Alerts: name → created/updated, enabled or disabled, which data lake, and any meaning changes
- Everything skipped, and why
- Validation result per item (PASS / WARN / FAIL), the panels that returned no data, and
  every panel whose values don't match Grafana (from `grafana_comparison` in verify.json)
- Follow-ups: rotate/delete the Grafana token if it was created for this; alert
  notification routing (Grafana contact points / policies do **not** carry over —
  the user sets up Cardinal notification groups, then attaches them to the rules)
- If step 0 connected them just now: what SKILL.md's **Report** section says about
  restarting the agent to get the Cardinal MCP tools in chat, and how to disconnect

Offer to write the report into a file (`migration-report.md`) in the working directory.
